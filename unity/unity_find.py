#!/usr/bin/env python3
"""
Stateless Unity asset lookup tool — zero-index, ripgrep-backed.

Commands:
  guid      <path>              Print guid of an asset.
  path      <guid>              Print asset path for a guid.
  refs      <path|guid>         Find all assets that reference this asset.
  deps      <path>              List outgoing references of this asset.
  instances <ClassName>         Find all assets whose m_Script is this class.

Output is grouped and human-readable (designed for AI agents to consume
without follow-up reads). Use --json for machine output.

Paths can be absolute or relative to the project root (parent of Assets/).
The script auto-locates the project root from its own location.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

GUID_RE = re.compile(r"\bguid:\s*([a-f0-9]{32})")
META_GUID_RE = re.compile(r"^guid:\s*([a-f0-9]{32})", re.MULTILINE)
# Structural cross-asset reference. Unity uses this exact shape for every
# pointer from one asset to another; anything else that looks like "guid: X"
# (e.g. inside AudioMixer snapshots) is an internal identifier, not a ref.
ASSET_REF_RE = re.compile(
    r"\{fileID:\s*(-?\d+),\s*guid:\s*([a-f0-9]{32}),\s*type:\s*\d+\}"
)
M_SCRIPT_RE = re.compile(
    r"m_Script:\s*\{fileID:\s*-?\d+,\s*guid:\s*([a-f0-9]{32}),\s*type:\s*\d+\}"
)
M_NAME_RE = re.compile(r"^\s*m_Name:\s*(.+?)\s*$", re.MULTILINE)
NULL_GUID = "0" * 32
BUILTIN_GUID_PREFIX = "0" * 16  # Unity's built-in assets (default sprites, lights, materials)

SCAN_ROOTS = ("Assets", "Packages")
ASSET_EXTS = (".asset", ".prefab", ".unity", ".mat", ".controller", ".anim",
              ".playable", ".mixer", ".preset", ".spriteatlas", ".lighting",
              ".shadergraph", ".shadersubgraph", ".guiskin", ".physicmaterial",
              ".physicsmaterial2d", ".cubemap", ".overridecontroller",
              ".inputactions", ".terrainlayer")


def project_root() -> Path:
    # script lives at <root>/tools/unity_find.py
    return Path(__file__).resolve().parent.parent


ROOT = project_root()


def run_rg(args: list[str]) -> list[str]:
    """Run ripgrep, return stdout lines. Non-zero exit with no matches is fine."""
    try:
        r = subprocess.run(
            ["rg", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        sys.exit("error: ripgrep (rg) not found in PATH")
    if r.returncode not in (0, 1):
        sys.exit(f"rg failed: {r.stderr.strip()}")
    return [ln for ln in r.stdout.splitlines() if ln]


def norm_path(p: str) -> Path:
    """Resolve user input to path relative to project root."""
    raw = Path(p)
    if raw.is_absolute():
        try:
            return raw.relative_to(ROOT)
        except ValueError:
            return raw
    # if cwd differs from root, try both
    if (ROOT / raw).exists():
        return raw
    cwd_abs = (Path.cwd() / raw).resolve()
    try:
        return cwd_abs.relative_to(ROOT)
    except ValueError:
        return raw


def meta_for(asset_path: Path) -> Path:
    return Path(str(asset_path) + ".meta")


def read_guid_of(asset_path: Path) -> str | None:
    meta = ROOT / meta_for(asset_path)
    if not meta.exists():
        return None
    m = META_GUID_RE.search(meta.read_text(encoding="utf-8", errors="ignore"))
    return m.group(1) if m else None


def guid_to_path(guid: str) -> Path | None:
    """Find .meta with this guid, return the asset path (without .meta)."""
    lines = run_rg([
        "-l", "-F", f"guid: {guid}",
        "-g", "*.meta", *SCAN_ROOTS,
    ])
    if not lines:
        return None
    meta_path = lines[0]
    if not meta_path.endswith(".meta"):
        return None
    return Path(meta_path[: -len(".meta")])


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".unity": "Scene",
        ".prefab": "Prefab",
        ".mat": "Material",
        ".controller": "AnimatorController",
        ".overridecontroller": "AnimatorOverride",
        ".anim": "Animation",
        ".asset": "Asset",  # refined below via m_Script
        ".cs": "Script",
        ".shader": "Shader",
        ".shadergraph": "ShaderGraph",
        ".png": "Texture",
        ".jpg": "Texture",
        ".tga": "Texture",
        ".psd": "Texture",
        ".wav": "Audio",
        ".mp3": "Audio",
        ".ogg": "Audio",
        ".fbx": "Model",
        ".ttf": "Font",
        ".otf": "Font",
    }.get(ext, ext.lstrip(".") or "Unknown")


def resolve_script_guid(script_guid: str) -> str | None:
    """guid of a .cs.meta → class name (filename stem)."""
    if script_guid == NULL_GUID:
        return "<missing script>"
    lines = run_rg([
        "-l", "-F", f"guid: {script_guid}",
        "-g", "*.cs.meta", *SCAN_ROOTS,
    ])
    if not lines:
        return None
    return Path(lines[0]).name.removesuffix(".cs.meta")


def script_class_of(asset_path: Path) -> str | None:
    """For .asset/.prefab files — find first m_Script guid and resolve to class name."""
    full = ROOT / asset_path
    if not full.exists():
        return None
    try:
        # only first 8KB — m_Script is near the top of a ScriptableObject
        with open(full, "rb") as f:
            head = f.read(8192).decode("utf-8", errors="ignore")
    except OSError:
        return None
    m = M_SCRIPT_RE.search(head)
    if not m:
        return None
    return resolve_script_guid(m.group(1))


def read_m_name(asset_path: Path) -> str | None:
    full = ROOT / asset_path
    if not full.exists():
        return None
    try:
        with open(full, "rb") as f:
            head = f.read(16384).decode("utf-8", errors="ignore")
    except OSError:
        return None
    m = M_NAME_RE.search(head)
    if not m:
        return None
    name = m.group(1).strip()
    return name or None


def display_name(asset_path: Path) -> str:
    ext = asset_path.suffix.lower()
    if ext in (".asset", ".prefab"):
        n = read_m_name(asset_path)
        if n:
            return n
    return asset_path.stem


def count_refs_in_file(file_path: str, guid: str) -> int:
    lines = run_rg([
        "-c", "--no-filename", "-F", f"guid: {guid}",
        "--", file_path,
    ])
    if not lines:
        return 0
    try:
        return int(lines[0])
    except ValueError:
        return 1


def count_refs_by_sprite(file_path: str, guid: str, id_to_name: dict[int, str]) -> dict[str, int]:
    """For texture refs in a file, bucket by sub-sprite name (via internalID)."""
    lines = run_rg([
        "-F", f"guid: {guid}",
        "--no-filename",
        "--", file_path,
    ])
    pattern = re.compile(
        r"\{fileID:\s*(-?\d+),\s*guid:\s*" + re.escape(guid)
    )
    counts: dict[str, int] = defaultdict(int)
    for ln in lines:
        for m in pattern.finditer(ln):
            fid = int(m.group(1))
            name = id_to_name.get(fid) or f"<fid:{fid}>"
            counts[name] += 1
    return dict(counts)


def locate_in_file(file_path: str, guid: str, limit: int = 20) -> list[tuple[int, str]]:
    """Return [(line_no, snippet)] where guid occurs in file."""
    lines = run_rg([
        "-n", "--no-filename", "-F", f"guid: {guid}",
        "--", file_path,
    ])
    out: list[tuple[int, str]] = []
    for ln in lines[:limit]:
        # format: "42:  - guid: abc..."
        head, _, rest = ln.partition(":")
        try:
            n = int(head)
        except ValueError:
            continue
        snippet = rest.strip()
        if len(snippet) > 100:
            snippet = snippet[:97] + "..."
        out.append((n, snippet))
    return out


def find_referers(
    guid: str,
    self_path: Path | None,
    with_locations: bool = False,
    sprite_table: dict[int, str] | None = None,
) -> list[dict]:
    """Return list of {path, type, name, class, count, locations?, sprites?}."""
    lines = run_rg([
        "-l", "-F", f"guid: {guid}",
        "-g", "!*.meta",
        *SCAN_ROOTS,
    ])
    results = []
    for rel in lines:
        p = Path(rel)
        if self_path is not None and p == self_path:
            continue
        entry = {
            "path": rel,
            "type": classify(p),
            "name": display_name(p),
            "class": None,
            "count": count_refs_in_file(rel, guid),
        }
        if entry["type"] == "Asset":
            cls = script_class_of(p)
            if cls:
                entry["class"] = cls
                entry["type"] = cls
        if with_locations:
            entry["locations"] = locate_in_file(rel, guid)
        if sprite_table:
            entry["sprites"] = count_refs_by_sprite(rel, guid, sprite_table)
        results.append(entry)
    return results


def group_and_print_refs(guid: str, target: Path | None, results: list[dict]):
    if target is not None:
        name = display_name(target)
        header_type = classify(target)
        if header_type == "Asset":
            cls = script_class_of(target)
            if cls:
                header_type = cls
        print(f"{header_type}: {name}  [{target}]")
    print(f"guid: {guid}")
    print(f"referenced by {len(results)} file(s):")
    if not results:
        return
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        groups[r["type"]].append(r)
    # stable order: Scene, Prefab, then sorted class-types, then rest
    priority = ["Scene", "Prefab", "Material", "AnimatorController"]
    order = [t for t in priority if t in groups]
    order += sorted(t for t in groups if t not in priority)
    for t in order:
        items = sorted(groups[t], key=lambda r: r["path"])
        print(f"\n[{t}]  ({len(items)})")
        for r in items:
            count_str = f"  ×{r['count']}" if r["count"] > 1 else ""
            sprites = r.get("sprites") or {}
            inline_sprite_str = ""
            expanded_sprites: list[tuple[str, int]] = []
            if sprites:
                parts = sorted(sprites.items(), key=lambda kv: (-kv[1], kv[0]))
                if len(parts) <= 5:
                    inline_sprite_str = "  (" + ", ".join(
                        f"{n}×{c}" if c > 1 else n for n, c in parts
                    ) + ")"
                else:
                    expanded_sprites = parts
            print(f"  {r['name']:<40} {r['path']}{count_str}{inline_sprite_str}")
            for n, c in expanded_sprites:
                marker = f"×{c}" if c > 1 else "×1"
                print(f"      - {n:<36} {marker}")
            for ln, snip in r.get("locations", []):
                print(f"      L{ln}: {snip}")


def cmd_guid(args):
    path = norm_path(args.target)
    g = read_guid_of(path)
    if not g:
        sys.exit(f"no .meta for {path}")
    print(g)


def cmd_path(args):
    p = guid_to_path(args.guid)
    if not p:
        sys.exit(f"no asset found for guid {args.guid}")
    print(p)


def cmd_refs(args):
    tgt = args.target
    if re.fullmatch(r"[a-f0-9]{32}", tgt):
        guid = tgt
        target_path = guid_to_path(guid)
    else:
        target_path = norm_path(tgt)
        guid = read_guid_of(target_path)
        if not guid:
            sys.exit(f"no .meta for {target_path}")
    sprite_table: dict[int, str] | None = None
    if target_path is not None and target_path.suffix.lower() in (".png", ".jpg", ".psd", ".tga"):
        st = sprite_table_for_texture(target_path)
        if st:
            sprite_table = st
    results = find_referers(
        guid, target_path,
        with_locations=args.locations,
        sprite_table=sprite_table,
    )
    if args.json:
        print(json.dumps({
            "guid": guid,
            "target": str(target_path) if target_path else None,
            "referers": results,
        }, indent=2))
        return
    group_and_print_refs(guid, target_path, results)


def cmd_deps(args):
    path = norm_path(args.target)
    full = ROOT / path
    if not full.exists():
        sys.exit(f"not found: {path}")
    text = full.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    self_guid = read_guid_of(path)
    guids: dict[str, int] = defaultdict(int)
    file_ids: dict[str, set[int]] = defaultdict(set)  # guid → set of sub-sprite fileIDs
    locations: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for m in ASSET_REF_RE.finditer(text):
        fid = int(m.group(1))
        g = m.group(2)
        if g == NULL_GUID or g == self_guid:
            continue
        if g.startswith(BUILTIN_GUID_PREFIX):
            continue
        guids[g] += 1
        file_ids[g].add(fid)
        if args.locations:
            line_no = text.count("\n", 0, m.start()) + 1
            if line_no - 1 < len(lines):
                snippet = lines[line_no - 1].strip()
                if len(snippet) > 100:
                    snippet = snippet[:97] + "..."
                locations[g].append((line_no, snippet))
    print(f"{display_name(path)}  [{path}]")
    print(f"outgoing refs: {len(guids)} unique guid(s)")
    if not guids:
        return
    resolved = []
    for g, cnt in guids.items():
        p = guid_to_path(g)
        if p is None:
            resolved.append((None, None, g, cnt))
            continue
        t = classify(p)
        if t == "Asset":
            cls = script_class_of(p)
            if cls:
                t = cls
        resolved.append((p, t, g, cnt))

    # for texture refs, resolve which sub-sprites were used
    sprite_suffix: dict[str, str] = {}  # guid → trailing "(sprite1, sprite2)" string
    for p, t, g, _ in resolved:
        if p is None or t != "Texture":
            continue
        id_to_name = sprite_table_for_texture(p)
        if not id_to_name:
            continue
        used = file_ids.get(g, set())
        names = [id_to_name[fid] for fid in used if fid in id_to_name]
        if names:
            sprite_suffix[g] = "  (" + ", ".join(sorted(set(names))) + ")"
    # resolved with path first, grouped by type
    known = [r for r in resolved if r[0] is not None]
    unknown = [r for r in resolved if r[0] is None]
    by_type: dict[str, list] = defaultdict(list)
    for r in known:
        by_type[r[1]].append(r)
    for t in sorted(by_type):
        items = sorted(by_type[t], key=lambda r: str(r[0]))
        print(f"\n[{t}]  ({len(items)})")
        for p, _, g, cnt in items:
            count_str = f"  ×{cnt}" if cnt > 1 else ""
            suffix = sprite_suffix.get(g, "")
            print(f"  {display_name(p):<40} {p}{count_str}{suffix}")
            for ln, snip in locations.get(g, [])[:20]:
                print(f"      L{ln}: {snip}")
    if unknown:
        print(f"\n[unresolved]  ({len(unknown)})")
        for _, _, g, cnt in unknown:
            count_str = f"  ×{cnt}" if cnt > 1 else ""
            print(f"  {g}{count_str}")
            for ln, snip in locations.get(g, [])[:20]:
                print(f"      L{ln}: {snip}")


def cmd_instances(args):
    cls = args.class_name
    # find <Class>.cs.meta
    hits = run_rg([
        "--files",
        "-g", f"**/{cls}.cs.meta",
        *SCAN_ROOTS,
    ])
    if not hits:
        sys.exit(f"no .cs.meta found for class {cls}")
    if len(hits) > 1:
        print(f"warning: multiple .cs.meta for {cls}:", file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
    cs_meta = Path(hits[0])
    guid = META_GUID_RE.search(
        (ROOT / cs_meta).read_text(encoding="utf-8", errors="ignore")
    ).group(1)
    results = find_referers(guid, None)
    # filter to assets only that actually use m_Script with this guid (noisy otherwise: string references in cs files etc)
    # easier: rg already filtered to !*.meta; .cs files likely won't contain "guid: X" — skip them
    results = [r for r in results if not r["path"].endswith(".cs")]
    print(f"class: {cls}  [{cs_meta.with_suffix('').with_suffix('')}]")
    print(f"script guid: {guid}")
    print(f"used in {len(results)} asset(s):")
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        groups[r["type"]].append(r)
    for t in sorted(groups):
        items = sorted(groups[t], key=lambda r: r["path"])
        print(f"\n[{t}]  ({len(items)})")
        for r in items:
            count_str = f"  ×{r['count']}" if r["count"] > 1 else ""
            print(f"  {r['name']:<40} {r['path']}{count_str}")


NAME_FILEID_HEADER_RE = re.compile(r"^(\s*)nameFileIdTable:\s*$", re.MULTILINE)
NAME_FILEID_ENTRY_RE = re.compile(r"^(\s+)([^:\n]+?):\s*(-?\d+)\s*$")
SPRITE_MODE_RE = re.compile(r"^\s*spriteMode:\s*(\d+)\s*$", re.MULTILINE)

# Unity local fileIDs for main sub-assets follow the formula classID * 100000.
# classID is a Unity engine constant (same across versions/projects/platforms)
# baked into the binary YAML format — e.g. MonoScript=115 → 11500000 (which is
# why every `m_Script` ref you see in YAML has that exact fileID).
# For a Single-mode texture, the sole Sprite sub-asset lives under classID(213)
# * 100000 = 21300000. Multiple-mode textures assign per-sprite random
# internalIDs instead, because there are many sprites and 21300000 can only
# name one of them.
SPRITE_CLASS_ID = 213
MAIN_SPRITE_FILEID = SPRITE_CLASS_ID * 100000


def parse_sprite_mode(meta_path: Path) -> int | None:
    """Return spriteMode from a texture .meta: 1=Single, 2=Multiple, 0=None. None if unknown."""
    full = ROOT / meta_path
    if not full.exists():
        return None
    try:
        text = full.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = SPRITE_MODE_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def sprite_mode_label(mode: int | None) -> str:
    return {0: "None", 1: "Single", 2: "Multiple", 3: "Polygon"}.get(mode or -1, "?")


def parse_name_fileid_table(meta_path: Path) -> dict[str, int]:
    """Parse TextureImporter.nameFileIdTable → {sprite_name: internalID}.

    Returns empty dict if the section is missing.
    """
    full = ROOT / meta_path
    if not full.exists():
        return {}
    try:
        text = full.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    m = NAME_FILEID_HEADER_RE.search(text)
    if not m:
        return {}
    header_indent = len(m.group(1))
    result: dict[str, int] = {}
    for line in text[m.end():].split("\n"):
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent <= header_indent:  # exited the section
            break
        mm = NAME_FILEID_ENTRY_RE.match(line)
        if not mm:
            break
        name = mm.group(2).strip()
        try:
            result[name] = int(mm.group(3))
        except ValueError:
            break
    return result


def sprite_table_for_texture(asset_path: Path) -> dict[int, str]:
    """{ref_fileID: sprite_name} for a texture asset.

    For Multiple-mode textures, keys are the internalIDs from nameFileIdTable
    (which is how Unity stores refs to each sub-sprite).
    For Single-mode textures, the only key is MAIN_SPRITE_FILEID (21300000) —
    internalID is ignored because Unity references Single sprites by the fixed ID.
    """
    meta = Path(str(asset_path) + ".meta")
    table = parse_name_fileid_table(meta)
    if not table:
        return {}
    mode = parse_sprite_mode(meta)
    if mode == 1:
        name = next(iter(table.keys()))  # Single mode has exactly one entry
        return {MAIN_SPRITE_FILEID: name}
    return {iid: name for name, iid in table.items()}


def collect_project_guids() -> dict[str, str]:
    """Scan all .meta files → {guid: asset_path (without .meta)}."""
    lines = run_rg([
        "-H", "--no-heading", "^guid: [a-f0-9]{32}",
        "-g", "*.meta", *SCAN_ROOTS,
    ])
    result: dict[str, str] = {}
    for ln in lines:
        path, _, content = ln.partition(":")
        if not path.endswith(".meta"):
            continue
        m = re.search(r"([a-f0-9]{32})", content)
        if not m:
            continue
        result[m.group(1)] = path[: -len(".meta")]
    return result


def list_components(asset_path: Path) -> tuple[list[tuple[str, int]], int]:
    """Count m_Script occurrences in file, resolve guid → class name.

    Returns (sorted_list_of_(class, count), unresolved_count).
    """
    full = ROOT / asset_path
    if not full.exists():
        sys.exit(f"not found: {asset_path}")
    text = full.read_text(encoding="utf-8", errors="ignore")
    counts: dict[str, int] = defaultdict(int)
    cache: dict[str, str | None] = {}
    unresolved = 0
    for m in M_SCRIPT_RE.finditer(text):
        g = m.group(1)
        if g not in cache:
            cache[g] = resolve_script_guid(g)
        cls = cache[g]
        if cls:
            counts[cls] += 1
        else:
            unresolved += 1
    items = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return items, unresolved


def print_components(asset_path: Path, items: list[tuple[str, int]], unresolved: int):
    t = classify(asset_path)
    if t == "Asset":
        cls = script_class_of(asset_path)
        if cls:
            t = cls
    print(f"{t}: {display_name(asset_path)}  [{asset_path}]")
    total = sum(c for _, c in items) + unresolved
    print(f"m_Script entries: {total} ({len(items)} unique class(es))")
    if not items and not unresolved:
        return
    for cls, cnt in items:
        print(f"  {cls:<40} ×{cnt}")
    if unresolved:
        print(f"  <unresolved guid>                        ×{unresolved}")


def cmd_components(args):
    path = norm_path(args.target)
    items, unresolved = list_components(path)
    print_components(path, items, unresolved)


def cmd_summary(args):
    # same logic, different naming — nothing to customize yet
    cmd_components(args)


RESOURCES_LOAD_RE = re.compile(
    r'Resources\.Load(All|Async)?\s*<[^>]+>\s*\(\s*\$?"([^"{]*)(["{])'
)


def collect_resources_load_exact_paths() -> set[str]:
    """Exact asset paths from single-asset Resources.Load<T>("literal") calls.

    Excludes LoadAll (prefix scans) and interpolated / concatenated strings —
    those only tell us a folder is touched, not which assets are actually used;
    registry-based references (e.g. GameContentSpec in strict mode) cover that.
    """
    lines = run_rg([r"Resources\.Load", "-g", "*.cs", "Assets/Scripts"])
    exact: set[str] = set()
    for ln in lines:
        for m in RESOURCES_LOAD_RE.finditer(ln):
            variant, lit, terminator = m.group(1), m.group(2).strip(), m.group(3)
            if variant:  # LoadAll, LoadAsync
                continue
            if terminator != '"':  # interpolation cut the literal
                continue
            if lit.endswith("/") or not lit:
                continue  # folder prefix via concatenation
            exact.add(lit)
    return exact


def is_runtime_loaded(asset_path: str, exact_paths: set[str]) -> bool:
    marker = "/Resources/"
    idx = asset_path.find(marker)
    if idx < 0:
        return False
    rel = asset_path[idx + len(marker):]
    p = Path(rel)
    rel_no_ext = str(p.with_suffix("")) if p.suffix else rel
    return rel_no_ext in exact_paths


def filter_by_resources_load(candidates: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split orphan candidates into (confirmed, runtime_loaded).

    Only exact-path Resources.Load<T>("full/path") calls count. LoadAll and
    interpolated/concatenated paths are handled via the normal reference scan:
    if something is registered in a registry asset (strict mode) it isn't an
    orphan anyway, and if nothing points at it, it is a legitimate orphan even
    if the folder is iterated at runtime.
    """
    if not candidates:
        return {}, {}
    exact_paths = collect_resources_load_exact_paths()
    confirmed: dict[str, str] = {}
    loaded: dict[str, str] = {}
    for g, p in candidates.items():
        if is_runtime_loaded(p, exact_paths):
            loaded[g] = p
        else:
            confirmed[g] = p
    return confirmed, loaded


def cmd_orphans(args):
    folder = args.folder
    # collect guids of assets inside folder
    meta_files = run_rg(["--files", "-g", "*.meta", folder])
    guid_to_asset: dict[str, str] = {}
    for mf in meta_files:
        if not mf.endswith(".meta"):
            continue
        asset_rel = mf[: -len(".meta")]
        # skip folder metas — folders are not tracked as assets for orphan analysis
        if (ROOT / asset_rel).is_dir():
            continue
        try:
            txt = (ROOT / mf).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = META_GUID_RE.search(txt)
        if m:
            guid_to_asset[m.group(1)] = asset_rel
    if not guid_to_asset:
        print(f"no assets with .meta found in {folder}")
        return

    # single rg call with all patterns at once
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as tf:
        for g in guid_to_asset:
            tf.write(f"guid: {g}\n")
        patterns = tf.name
    try:
        lines = run_rg([
            "-nF", "-f", patterns,
            "-g", "!*.meta",
            *SCAN_ROOTS,
        ])
    finally:
        os.unlink(patterns)

    referenced: set[str] = set()
    for ln in lines:
        parts = ln.split(":", 2)
        if len(parts) != 3:
            continue
        path, _, content = parts
        m = GUID_RE.search(content)
        if not m:
            continue
        g = m.group(1)
        if g not in guid_to_asset:
            continue
        # ignore self-references (asset listing its own guid somewhere inside)
        if path == guid_to_asset[g]:
            continue
        # loose mode: skip GameContentSpec aggregator — it lists everything
        if not args.strict and path.endswith("GameContentSpec.asset"):
            continue
        referenced.add(g)

    raw_orphans: dict[str, str] = {
        g: p for g, p in guid_to_asset.items() if g not in referenced
    }
    if args.no_resources_check:
        confirmed, loaded = raw_orphans, {}
    else:
        confirmed, loaded = filter_by_resources_load(raw_orphans)

    mode_parts = []
    mode_parts.append("strict" if args.strict else "loose")
    if not args.no_resources_check:
        mode_parts.append("Resources.Load filter")
    mode = " + ".join(mode_parts)
    print(f"orphans in {folder}  [{mode}]")
    print(f"scanned {len(guid_to_asset)} asset(s), "
          f"{len(confirmed)} orphan(s), {len(loaded)} likely string-loaded:")

    def render(items: dict[str, str]):
        for g in sorted(items, key=lambda k: items[k]):
            p = items[g]
            p_path = Path(p)
            t = classify(p_path)
            if t == "Asset":
                cls = script_class_of(p_path)
                if cls:
                    t = cls
            print(f"  {t:<20} {display_name(p_path):<40} {p}")

    if confirmed:
        print("\n[orphans]")
        render(confirmed)
    if loaded:
        print("\n[runtime-loaded — exact Resources.Load<T>(\"path\") in Assets/Scripts/*.cs]")
        render(loaded)


def cmd_sprite(args):
    query = args.name
    query_lower = query.lower()
    # only parse meta files that actually have sprite sheets
    files = run_rg([
        "-l", "nameFileIdTable:",
        "-g", "*.png.meta", "-g", "*.jpg.meta", "-g", "*.psd.meta", "-g", "*.tga.meta",
        *SCAN_ROOTS,
    ])
    # (sprite_name, texture_path, ref_fileID, mode)
    results: list[tuple[str, str, int, int | None]] = []
    for meta_rel in files:
        if not meta_rel.endswith(".meta"):
            continue
        tex_path = meta_rel[: -len(".meta")]
        meta_path = Path(meta_rel)
        table = parse_name_fileid_table(meta_path)
        if not table:
            continue
        mode = parse_sprite_mode(meta_path)
        for name, iid in table.items():
            matches = (name == query) if args.exact else (query_lower in name.lower())
            if not matches:
                continue
            ref_id = MAIN_SPRITE_FILEID if mode == 1 else iid
            results.append((name, tex_path, ref_id, mode))
    results.sort(key=lambda r: (r[0], r[1]))
    label = "exact" if args.exact else "substring"
    print(f"{len(results)} match(es) for '{query}' [{label}]")
    if not results:
        return
    limit = None if args.all else 50
    shown = results[:limit] if limit else results
    for name, tex, ref_id, mode in shown:
        guid = read_guid_of(Path(tex)) or "?"
        mode_str = sprite_mode_label(mode)
        print(f"  {name}")
        print(f"    {tex}   [mode: {mode_str}]")
        print(f"    {{fileID: {ref_id}, guid: {guid}, type: 3}}")
    if limit and len(results) > limit:
        print(f"  ... +{len(results) - limit} more (use --all to see all)")


def cmd_sprites(args):
    tex = norm_path(args.target)
    meta = Path(str(tex) + ".meta")
    table = parse_name_fileid_table(meta)
    if not table:
        sys.exit(f"no nameFileIdTable in {meta}  (not a texture with sprites)")
    guid = read_guid_of(tex)
    if not guid:
        sys.exit(f"no .meta for {tex}")
    mode = parse_sprite_mode(meta)
    mode_str = sprite_mode_label(mode)

    # {sprite_name: ref_fileID} — what Unity uses in scene/prefab refs
    name_to_ref: dict[str, int] = {}
    if mode == 1:
        # Single: sole sprite is referenced via fixed 21300000
        only_name = next(iter(table.keys()))
        name_to_ref[only_name] = MAIN_SPRITE_FILEID
    else:
        for n, iid in table.items():
            name_to_ref[n] = iid

    ref_counts: dict[int, int] = defaultdict(int)
    if not args.no_refs:
        lines = run_rg([
            "-F", f"guid: {guid}",
            "-g", "!*.meta",
            *SCAN_ROOTS,
        ])
        file_ref_re = re.compile(
            r"\{fileID:\s*(-?\d+),\s*guid:\s*" + re.escape(guid)
        )
        for ln in lines:
            _, _, content = ln.partition(":")
            for m in file_ref_re.finditer(content):
                ref_counts[int(m.group(1))] += 1

    entries = sorted(
        name_to_ref.items(),
        key=lambda kv: (-ref_counts.get(kv[1], 0), kv[0]),
    )
    print(f"{tex}  [{len(entries)} sub-sprite(s), mode: {mode_str}]")
    print(f"guid: {guid}")
    if not args.no_refs:
        orphan = sum(1 for _, rid in entries if ref_counts.get(rid, 0) == 0)
        print(f"orphan sub-sprites: {orphan}")
    print()
    for name, rid in entries:
        if args.no_refs:
            print(f"  {name:<40}  fileID: {rid}")
        else:
            cnt = ref_counts.get(rid, 0)
            tag = "× 0 refs  ← orphan" if cnt == 0 else f"× {cnt} ref(s)"
            print(f"  {name:<40}  fileID: {rid:<22}  {tag}")


def cmd_missing(args):
    folder = args.folder
    search_roots = [folder] if folder else list(SCAN_ROOTS)
    guid_map = collect_project_guids()
    all_guids = set(guid_map.keys())
    all_guids.add(NULL_GUID)
    # Match Unity's canonical cross-asset ref form only, to avoid false positives
    # from internal identifiers (AudioMixer snapshots, etc.).
    lines = run_rg([
        "-n", r"\{fileID: -?\d+, guid: [a-f0-9]{32}, type: \d+\}",
        "-g", "!*.meta",
        *search_roots,
    ])
    broken: list[tuple[str, int, str, str]] = []
    self_guid_cache: dict[str, str | None] = {}
    def self_guid_for(path: str) -> str | None:
        if path in self_guid_cache:
            return self_guid_cache[path]
        meta = ROOT / (path + ".meta")
        g: str | None = None
        if meta.exists():
            mm = META_GUID_RE.search(meta.read_text(encoding="utf-8", errors="ignore"))
            if mm:
                g = mm.group(1)
        self_guid_cache[path] = g
        return g
    for ln in lines:
        parts = ln.split(":", 2)
        if len(parts) != 3:
            continue
        path, lineno, content = parts
        try:
            n = int(lineno)
        except ValueError:
            continue
        self_guid = self_guid_for(path)
        for m in ASSET_REF_RE.finditer(content):
            g = m.group(2)
            if g in all_guids or g == self_guid:
                continue
            if g.startswith(BUILTIN_GUID_PREFIX):
                continue  # Unity built-in asset
            snippet = content.strip()
            if len(snippet) > 100:
                snippet = snippet[:97] + "..."
            broken.append((path, n, g, snippet))
            break
    # group by missing guid (one missing asset referenced from many places)
    by_guid: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for path, n, g, snip in broken:
        by_guid[g].append((path, n, snip))
    print(f"missing references in {folder or 'project'}: "
          f"{len(by_guid)} unique guid(s), {len(broken)} occurrence(s)")
    if not broken:
        return
    # classify by Unity field name; m_Script → "script", m_Sprite → "sprite", etc.
    field_re = re.compile(r"([\w\[\]]+):\s*\{fileID")
    def classify_field(occs: list[tuple[str, int, str]]) -> str:
        tally: dict[str, int] = defaultdict(int)
        for _, _, snip in occs:
            m = field_re.search(snip)
            if m:
                tally[m.group(1)] += 1
        if not tally:
            return "?"
        top = max(tally.items(), key=lambda kv: kv[1])[0]
        mapping = {
            "m_Script": "script", "m_Sprite": "sprite", "m_Font": "font",
            "m_Texture": "texture", "m_Material": "material", "m_Mesh": "mesh",
            "m_Controller": "animator", "m_AnimatorController": "animator",
            "m_Clip": "audioClip", "m_Shader": "shader", "m_Icon": "icon",
        }
        return mapping.get(top, top)
    ordered = sorted(by_guid.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for g, occs in ordered:
        kind = classify_field(occs)
        print(f"\n[{kind}] guid: {g}  ({len(occs)} ref(s))")
        for path, n, snip in occs[:5]:
            print(f"  {path}:L{n}  {snip}")
        if len(occs) > 5:
            print(f"  ... +{len(occs) - 5} more")


def main():
    p = argparse.ArgumentParser(description="Unity stateless asset lookup")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("guid", help="print guid of an asset")
    sp.add_argument("target")
    sp.set_defaults(func=cmd_guid)

    sp = sub.add_parser("path", help="print asset path for a guid")
    sp.add_argument("guid")
    sp.set_defaults(func=cmd_path)

    sp = sub.add_parser("refs", help="find all assets referencing this asset")
    sp.add_argument("target", help="asset path or 32-char guid")
    sp.add_argument("-L", "--locations", action="store_true",
                    help="show line numbers and snippets for each match")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_refs)

    sp = sub.add_parser("deps", help="list outgoing asset references")
    sp.add_argument("target")
    sp.add_argument("-L", "--locations", action="store_true",
                    help="show line numbers and snippets for each reference")
    sp.set_defaults(func=cmd_deps)

    sp = sub.add_parser("instances", help="find all assets using a given script class")
    sp.add_argument("class_name")
    sp.set_defaults(func=cmd_instances)

    sp = sub.add_parser("components", help="list MonoBehaviour/Script classes attached to a prefab")
    sp.add_argument("target")
    sp.set_defaults(func=cmd_components)

    sp = sub.add_parser("summary", help="inventory of script classes in a scene or prefab")
    sp.add_argument("target")
    sp.set_defaults(func=cmd_summary)

    sp = sub.add_parser("orphans", help="find unreferenced assets in a folder")
    sp.add_argument("folder", help="folder to scan, e.g. Assets/Resources/Content/Tags")
    sp.add_argument("--strict", action="store_true",
                    help="do not exclude GameContentSpec aggregator from reference set")
    sp.add_argument("--no-resources-check", action="store_true",
                    help="skip the heuristic that filters string-loaded assets")
    sp.set_defaults(func=cmd_orphans)

    sp = sub.add_parser("missing", help="find references to guids that no longer exist")
    sp.add_argument("folder", nargs="?", help="optional folder to scan (default: whole project)")
    sp.set_defaults(func=cmd_missing)

    sp = sub.add_parser("sprite", help="find a sprite by name across all textures")
    sp.add_argument("name", help="sprite name (substring by default)")
    sp.add_argument("--exact", action="store_true", help="require exact name match")
    sp.add_argument("--all", action="store_true", help="show all results (default: limit 50)")
    sp.set_defaults(func=cmd_sprite)

    sp = sub.add_parser("sprites", help="list sub-sprites in a texture with reference counts")
    sp.add_argument("target", help="path to a .png/.jpg/.psd texture")
    sp.add_argument("--no-refs", action="store_true", help="skip reference counting (faster)")
    sp.set_defaults(func=cmd_sprites)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
