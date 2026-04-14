#!/usr/bin/env python3
"""
unity_read.py — read-only content inspection for Unity text-serialized files.

Pairs with unity_find.py: where unity_find navigates the asset graph
(file ↔ file via guids), unity_read navigates what lives *inside* a single
Unity YAML file — GameObjects, components, fields, ScriptableObject values,
material slots, etc.

Every field in the output carries a line anchor (L<N>) so the AI agent can
copy the exact YAML line into the Edit tool as `old_string` and change it
in place. The script NEVER writes to files.

Works on:
  - .unity (scenes)                          → tree, find, inspect, path
  - .prefab (prefabs and variants)           → tree, find, inspect, path
  - .asset (ScriptableObjects, sub-assets)   → show
  - .mat (Materials)                         → show
  - other single-doc or multi-doc YAML assets → show

Commands:
  tree     <file>                     GameObject hierarchy with components
  find     <file> <name>              locate GameObjects by name (substring)
  inspect  <file> <fileID|name>       dump one GameObject's components + fields
  path     <file> <fileID>            fileID → "Parent/Child/..." hierarchy path
  show     <file>                     dump all documents of a non-GameObject file
                                      (ScriptableObject / Material / sub-assets)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("Assets", "Packages")

# --- Unity classID → class name (common subset used in scenes/prefabs) ---
UNITY_CLASSES: dict[int, str] = {
    1: "GameObject", 2: "Component", 4: "Transform",
    20: "Camera", 21: "Material", 23: "MeshRenderer",
    25: "Renderer", 27: "Texture", 28: "Texture2D", 33: "MeshFilter",
    43: "Mesh", 48: "Shader", 54: "Rigidbody",
    58: "CircleCollider2D", 60: "PolygonCollider2D", 61: "BoxCollider2D",
    64: "MeshCollider", 65: "BoxCollider", 68: "EdgeCollider2D",
    70: "CapsuleCollider2D", 81: "AudioListener", 82: "AudioSource",
    95: "Animator", 102: "TextMesh", 108: "Light", 111: "Animation",
    114: "MonoBehaviour", 115: "MonoScript", 120: "LineRenderer",
    124: "Behaviour", 135: "SphereCollider", 136: "CapsuleCollider",
    137: "SkinnedMeshRenderer", 143: "Rigidbody2D", 146: "PhysicsMaterial2D",
    152: "RenderTexture", 198: "ParticleSystem", 199: "ParticleSystemRenderer",
    205: "SortingGroup", 210: "SortingGroup", 212: "SpriteRenderer", 213: "Sprite",
    220: "LightProbeGroup", 222: "CanvasRenderer", 223: "Canvas",
    224: "RectTransform", 225: "LineRenderer", 227: "TrailRenderer",
    331: "SpriteRenderer", 362: "TilemapCollider2D", 483: "Tilemap",
    1001: "PrefabInstance", 1480428607: "LODGroup",
}

DOC_HEADER_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)( stripped)?")
FILEID_RE = re.compile(r"fileID:\s*(-?\d+)")
GUID_RE = re.compile(r"guid:\s*([a-f0-9]{32})")
ASSET_REF_RE = re.compile(
    r"\{fileID:\s*(-?\d+),\s*guid:\s*([a-f0-9]{32}),\s*type:\s*\d+\}"
)
LOCAL_REF_RE = re.compile(r"\{fileID:\s*(-?\d+)\}")
FIELD_LINE_RE = re.compile(r"^(\s*)([A-Za-z_]\w*):\s*(.*)$")

# fields hidden by default in inspect output; use --fields to see everything
HIDDEN_FIELDS = {
    "m_ObjectHideFlags", "m_CorrespondingSourceObject", "m_PrefabInstance",
    "m_PrefabAsset", "m_EditorHideFlags", "m_EditorClassIdentifier",
    "m_IsActive", "serializedVersion", "m_GameObject",
    "m_NavMeshLayer", "m_StaticEditorFlags",
    # transform internals that duplicate the useful fields
    "m_LocalEulerAnglesHint", "m_ConstrainProportionsScale",
    "m_RootOrder", "m_Father", "m_Children",
    # monobehaviour wiring
    "m_Script", "m_Enabled",
    # common UI noise
    "m_AnchorMin", "m_AnchorMax", "m_Pivot",
}


# ========================================================================
# Core parsing
# ========================================================================

@dataclass
class Doc:
    class_id: int
    file_id: int
    start: int        # line index (0-based) of the `--- !u!... &...` header
    end: int          # exclusive
    stripped: bool
    top_indent: int = 0  # indent of field lines under "ClassName:"


@dataclass
class GameObject:
    file_id: int
    name: str
    name_line: int
    component_fileids: list[int] = field(default_factory=list)
    transform_fileid: int | None = None
    stripped: bool = False


@dataclass
class Xform:  # Transform or RectTransform
    file_id: int
    gameobject_fileid: int
    parent_fileid: int
    child_fileids: list[int]
    root_order: int


@dataclass
class SceneModel:
    file_path: Path
    lines: list[str]
    docs: list[Doc]
    by_fileid: dict[int, Doc]
    gameobjects: dict[int, GameObject]         # GO fileID → GameObject
    transforms: dict[int, Xform]                # transform fileID → Xform
    go_to_transform: dict[int, int]             # GO fileID → transform fileID
    go_children: dict[int, list[int]]           # GO fileID → sorted child GO fileIDs
    roots: list[int]                            # root GO fileIDs


def parse_file(file_path: Path) -> SceneModel:
    full = ROOT / file_path
    if not full.exists():
        sys.exit(f"not found: {file_path}")
    text = full.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    docs: list[Doc] = []
    current: Doc | None = None
    for i, line in enumerate(lines):
        m = DOC_HEADER_RE.match(line)
        if m:
            if current is not None:
                current.end = i
                docs.append(current)
            current = Doc(
                class_id=int(m.group(1)),
                file_id=int(m.group(2)),
                start=i,
                end=-1,
                stripped=bool(m.group(3)),
            )
    if current is not None:
        current.end = len(lines)
        docs.append(current)

    # determine top_indent for each doc
    for doc in docs:
        for i in range(doc.start + 2, doc.end):
            ln = lines[i]
            if not ln.strip():
                continue
            stripped = ln.lstrip()
            if stripped.startswith("#"):
                continue
            doc.top_indent = len(ln) - len(stripped)
            break

    by_fileid: dict[int, Doc] = {d.file_id: d for d in docs}
    gameobjects: dict[int, GameObject] = {}
    transforms: dict[int, Xform] = {}

    for doc in docs:
        if doc.stripped:
            # skip stripped for hierarchy — they're placeholder overrides
            if doc.class_id == 1:
                gameobjects[doc.file_id] = GameObject(
                    file_id=doc.file_id,
                    name="<stripped>",
                    name_line=-1,
                    stripped=True,
                )
            continue
        if doc.class_id == 1:
            gameobjects[doc.file_id] = _parse_gameobject(lines, doc)
        elif doc.class_id in (4, 224):  # Transform, RectTransform
            t = _parse_transform(lines, doc)
            if t is not None:
                transforms[doc.file_id] = t

    go_to_transform: dict[int, int] = {}
    for tid, t in transforms.items():
        if t.gameobject_fileid in gameobjects:
            go_to_transform[t.gameobject_fileid] = tid
            gameobjects[t.gameobject_fileid].transform_fileid = tid

    # build parent → children map at GO level, ordered by RootOrder
    go_children: dict[int, list[tuple[int, int]]] = defaultdict(list)
    roots: list[tuple[int, int]] = []
    for gid, go in gameobjects.items():
        tid = go.transform_fileid
        if tid is None:
            continue
        t = transforms[tid]
        parent_gid = 0
        if t.parent_fileid != 0:
            parent_t = transforms.get(t.parent_fileid)
            if parent_t:
                parent_gid = parent_t.gameobject_fileid
        if parent_gid == 0:
            roots.append((t.root_order, gid))
        else:
            go_children[parent_gid].append((t.root_order, gid))

    sorted_children: dict[int, list[int]] = {
        p: [g for _, g in sorted(kids)] for p, kids in go_children.items()
    }
    sorted_roots = [g for _, g in sorted(roots)]

    return SceneModel(
        file_path=file_path,
        lines=lines,
        docs=docs,
        by_fileid=by_fileid,
        gameobjects=gameobjects,
        transforms=transforms,
        go_to_transform=go_to_transform,
        go_children=sorted_children,
        roots=sorted_roots,
    )


def _doc_field_line(lines: list[str], doc: Doc, name: str) -> int | None:
    """Return line index of a top-level field line `  <name>: ...` inside doc."""
    for i in range(doc.start + 2, doc.end):
        ln = lines[i]
        if not ln.strip():
            continue
        stripped = ln.lstrip()
        indent = len(ln) - len(stripped)
        if indent < doc.top_indent:
            break
        if indent != doc.top_indent:
            continue
        if stripped.startswith(name + ":"):
            return i
    return None


def _collect_fileids_in_block(lines: list[str], start: int, doc: Doc) -> list[int]:
    """Collect fileIDs from a list-valued field block starting after `start`.

    YAML list items begin with `-` at the same indent as the parent key
    (e.g. `m_Component:` and its `- component: {fileID: X}` entries both
    sit at `top_indent`). Walk forward accepting `-` lines and any deeper
    continuations; stop on a sibling field at `top_indent`.
    """
    result: list[int] = []
    for i in range(start + 1, doc.end):
        ln = lines[i]
        if not ln.strip():
            continue
        stripped = ln.lstrip()
        indent = len(ln) - len(stripped)
        if indent < doc.top_indent:
            break
        if indent == doc.top_indent and not stripped.startswith("-"):
            break  # next sibling field
        for m in FILEID_RE.finditer(ln):
            result.append(int(m.group(1)))
    return result


def _parse_gameobject(lines: list[str], doc: Doc) -> GameObject:
    name = "<unnamed>"
    name_line = -1
    comps: list[int] = []
    name_idx = _doc_field_line(lines, doc, "m_Name")
    if name_idx is not None:
        name = lines[name_idx].split(":", 1)[1].strip()
        name_line = name_idx + 1
    comp_idx = _doc_field_line(lines, doc, "m_Component")
    if comp_idx is not None:
        comps = _collect_fileids_in_block(lines, comp_idx, doc)
    return GameObject(
        file_id=doc.file_id, name=name, name_line=name_line,
        component_fileids=comps,
    )


def _parse_transform(lines: list[str], doc: Doc) -> Xform | None:
    go_idx = _doc_field_line(lines, doc, "m_GameObject")
    father_idx = _doc_field_line(lines, doc, "m_Father")
    children_idx = _doc_field_line(lines, doc, "m_Children")
    root_idx = _doc_field_line(lines, doc, "m_RootOrder")
    go_id = 0
    father_id = 0
    children: list[int] = []
    root_order = 0
    if go_idx is not None:
        m = FILEID_RE.search(lines[go_idx])
        if m:
            go_id = int(m.group(1))
    if father_idx is not None:
        m = FILEID_RE.search(lines[father_idx])
        if m:
            father_id = int(m.group(1))
    if children_idx is not None:
        children = _collect_fileids_in_block(lines, children_idx, doc)
    if root_idx is not None:
        try:
            root_order = int(lines[root_idx].split(":", 1)[1].strip())
        except ValueError:
            pass
    return Xform(
        file_id=doc.file_id,
        gameobject_fileid=go_id,
        parent_fileid=father_id,
        child_fileids=children,
        root_order=root_order,
    )


# ========================================================================
# Resolution helpers
# ========================================================================

_script_class_cache: dict[str, str | None] = {}


def run_rg(args: list[str]) -> list[str]:
    try:
        r = subprocess.run(["rg", *args], cwd=ROOT, capture_output=True,
                           text=True, check=False)
    except FileNotFoundError:
        sys.exit("error: ripgrep (rg) not found in PATH")
    if r.returncode not in (0, 1):
        sys.exit(f"rg failed: {r.stderr.strip()}")
    return [ln for ln in r.stdout.splitlines() if ln]


def resolve_script_class(script_guid: str) -> str | None:
    if script_guid in _script_class_cache:
        return _script_class_cache[script_guid]
    if set(script_guid) == {"0"}:
        _script_class_cache[script_guid] = None
        return None
    hits = run_rg([
        "-l", "-F", f"guid: {script_guid}",
        "-g", "*.cs.meta", *SCAN_ROOTS,
    ])
    if not hits:
        # fall back to Library/PackageCache for Unity built-ins (uGUI, TMP, etc.)
        cache_root = ROOT / "Library" / "PackageCache"
        if cache_root.exists():
            hits = run_rg([
                "-l", "-F", f"guid: {script_guid}",
                "-g", "*.cs.meta", "Library/PackageCache",
            ])
    result = None
    if hits:
        result = Path(hits[0]).name.removesuffix(".cs.meta")
    _script_class_cache[script_guid] = result
    return result


_guid_to_path_cache: dict[str, str | None] = {}
_name_fileid_table_cache: dict[str, dict[int, str]] = {}


def guid_to_asset_path(guid: str) -> str | None:
    if guid in _guid_to_path_cache:
        return _guid_to_path_cache[guid]
    hits = run_rg([
        "-l", "-F", f"guid: {guid}",
        "-g", "*.meta", *SCAN_ROOTS,
    ])
    result = None
    for h in hits:
        if h.endswith(".meta"):
            result = h[:-5]
            break
    _guid_to_path_cache[guid] = result
    return result


NAME_FILEID_HEADER_RE = re.compile(r"^(\s*)nameFileIdTable:\s*$", re.MULTILINE)
NAME_FILEID_ENTRY_RE = re.compile(r"^(\s+)([^:\n]+?):\s*(-?\d+)\s*$")
SPRITE_MODE_RE = re.compile(r"^\s*spriteMode:\s*(\d+)\s*$", re.MULTILINE)

# Unity local fileIDs for main sub-assets follow `classID * 100000` — classID
# is a fixed engine constant (Sprite=213, MonoScript=115, GameObject=1, etc.)
# baked into Unity's YAML format. A Single-mode texture's sole sprite lives
# under 213*100000 = 21300000. Multiple-mode textures assign random internalIDs
# per sub-sprite instead, since 21300000 can only name one.
SPRITE_CLASS_ID = 213
MAIN_SPRITE_FILEID = SPRITE_CLASS_ID * 100000


def sub_sprite_table(texture_path: str) -> dict[int, str]:
    """{ref_fileID: sprite_name} for a texture asset.

    For Multiple-mode textures, keys are the internalIDs from nameFileIdTable.
    For Single-mode textures, the sole key is MAIN_SPRITE_FILEID (21300000) —
    that's how scene/prefab YAML actually references Single sprites.
    """
    if texture_path in _name_fileid_table_cache:
        return _name_fileid_table_cache[texture_path]
    meta = ROOT / (texture_path + ".meta")
    raw: dict[int, str] = {}
    mode: int | None = None
    if meta.exists():
        text = meta.read_text(encoding="utf-8", errors="ignore")
        m = NAME_FILEID_HEADER_RE.search(text)
        if m:
            header_indent = len(m.group(1))
            for line in text[m.end():].split("\n"):
                if not line.strip():
                    continue
                stripped = line.lstrip()
                indent = len(line) - len(stripped)
                if indent <= header_indent:
                    break
                mm = NAME_FILEID_ENTRY_RE.match(line)
                if not mm:
                    break
                try:
                    raw[int(mm.group(3))] = mm.group(2).strip()
                except ValueError:
                    break
        mode_m = SPRITE_MODE_RE.search(text)
        if mode_m:
            try:
                mode = int(mode_m.group(1))
            except ValueError:
                mode = None
    if raw and mode == 1:
        # Remap Single-mode: the one entry is referenced via 21300000, not its internalID.
        only_name = next(iter(raw.values()))
        result: dict[int, str] = {MAIN_SPRITE_FILEID: only_name}
    else:
        result = raw
    _name_fileid_table_cache[texture_path] = result
    return result


def format_asset_ref(file_id: int, guid: str) -> str:
    """Human-readable asset reference: resolves guid, sub-sprite name, class."""
    if guid.startswith("0" * 16):
        return f"<built-in fileID:{file_id}>"
    path = guid_to_asset_path(guid)
    if path is None:
        return f"<missing guid:{guid}>"
    p = Path(path)
    # sub-sprite resolve
    if p.suffix.lower() in (".png", ".jpg", ".psd", ".tga"):
        table = sub_sprite_table(path)
        sprite_name = table.get(file_id)
        if sprite_name:
            return f"{sprite_name} ({path})"
        return path
    return path


def format_local_ref(model: SceneModel, file_id: int) -> str:
    """Resolve a local (same-file) fileID to its GameObject path or class."""
    if file_id == 0:
        return "<null>"
    doc = model.by_fileid.get(file_id)
    if doc is None:
        return f"<unknown fileID:{file_id}>"
    if doc.class_id == 1:  # GameObject
        go = model.gameobjects.get(file_id)
        if go:
            return f"GameObject '{go.name}'"
    if doc.class_id in (4, 224):  # Transform
        t = model.transforms.get(file_id)
        if t and t.gameobject_fileid in model.gameobjects:
            name = model.gameobjects[t.gameobject_fileid].name
            return f"Transform of '{name}'"
    cls = UNITY_CLASSES.get(doc.class_id, f"ClassID:{doc.class_id}")
    return f"{cls} [{file_id}]"


def component_class_name(model: SceneModel, comp_doc: Doc) -> str:
    """Resolve component doc to its class name. MonoBehaviour → m_Script → class."""
    cls = UNITY_CLASSES.get(comp_doc.class_id)
    if comp_doc.class_id != 114:
        return cls or f"ClassID:{comp_doc.class_id}"
    # MonoBehaviour — resolve script guid
    m_script_idx = _doc_field_line(model.lines, comp_doc, "m_Script")
    if m_script_idx is not None:
        m = GUID_RE.search(model.lines[m_script_idx])
        if m:
            resolved = resolve_script_class(m.group(1))
            if resolved:
                return resolved
    return "MonoBehaviour"


def hierarchy_path(model: SceneModel, go_id: int) -> str:
    """Compute 'Root/Child/.../Target' string for a GameObject fileID."""
    chain: list[str] = []
    cur = go_id
    visited = set()
    while cur and cur not in visited:
        visited.add(cur)
        go = model.gameobjects.get(cur)
        if go is None:
            break
        chain.append(go.name)
        tid = go.transform_fileid
        if tid is None:
            break
        t = model.transforms.get(tid)
        if t is None or t.parent_fileid == 0:
            break
        parent_t = model.transforms.get(t.parent_fileid)
        if parent_t is None:
            break
        cur = parent_t.gameobject_fileid
    return "/".join(reversed(chain))


# ========================================================================
# tree
# ========================================================================

def cmd_tree(args):
    model = parse_file(Path(args.file))
    root_ids: list[int]
    if args.root:
        target_ids = find_gameobjects(model, args.root)
        if not target_ids:
            sys.exit(f"no GameObject matching '{args.root}' in {args.file}")
        if len(target_ids) > 1:
            print(f"warning: '{args.root}' matches {len(target_ids)} GameObjects, using first", file=sys.stderr)
        root_ids = [target_ids[0]]
    else:
        root_ids = list(model.roots)

    is_huge = len(model.gameobjects) > 300 and not args.root
    if is_huge and args.depth is None:
        print(f"# {args.file} has {len(model.gameobjects)} GameObjects — showing roots only.")
        print(f"# Use --depth N for deeper levels, --root <name> for a subtree.")
        depth_cap = 0
    else:
        depth_cap = args.depth

    print(f"{args.file}  [{len(model.gameobjects)} GameObjects, {len(model.docs)} docs]")
    for rid in root_ids:
        _print_tree_node(
            model, rid, prefix="", is_last=True,
            depth=0, depth_cap=depth_cap,
            expand_components=args.expand_components,
            component_filter=args.filter,
        )


def _print_tree_node(
    model: SceneModel, go_id: int, prefix: str, is_last: bool,
    depth: int, depth_cap: int | None, expand_components: bool,
    component_filter: str | None,
):
    go = model.gameobjects.get(go_id)
    if go is None:
        return
    branch = "└─ " if is_last else "├─ "
    comps_str = ""
    comp_names: list[str] = []
    for cid in go.component_fileids:
        comp_doc = model.by_fileid.get(cid)
        if comp_doc is None:
            continue
        if comp_doc.class_id in (4, 224):
            continue  # don't list the Transform itself, it's implicit
        comp_names.append(component_class_name(model, comp_doc))

    if component_filter:
        has_match = any(component_filter in n for n in comp_names)
        if not has_match:
            # still descend to children — a matching component may live below
            kids = model.go_children.get(go_id, [])
            for i, cid in enumerate(kids):
                _print_tree_node(
                    model, cid, prefix, i == len(kids) - 1,
                    depth, depth_cap, expand_components, component_filter,
                )
            return

    suffix = ""
    if comp_names and not expand_components:
        suffix = "  (" + ", ".join(comp_names) + ")"
    stripped_mark = " [stripped]" if go.stripped else ""
    fid_mark = f"  [{go.file_id}]"
    name_anchor = f"  L{go.name_line}" if go.name_line > 0 else ""
    print(f"{prefix}{branch}{go.name}{stripped_mark}{suffix}{fid_mark}{name_anchor}")

    inner_prefix = prefix + ("   " if is_last else "│  ")
    if expand_components and comp_names:
        for cn in comp_names:
            print(f"{inner_prefix}· {cn}")

    if depth_cap is not None and depth >= depth_cap:
        return
    kids = model.go_children.get(go_id, [])
    for i, cid in enumerate(kids):
        _print_tree_node(
            model, cid, inner_prefix, i == len(kids) - 1,
            depth + 1, depth_cap, expand_components, component_filter,
        )


def find_gameobjects(model: SceneModel, query: str) -> list[int]:
    q = query.lower()
    return [gid for gid, go in model.gameobjects.items() if q in go.name.lower()]


# ========================================================================
# find
# ========================================================================

def cmd_find(args):
    model = parse_file(Path(args.file))
    matches = find_gameobjects(model, args.name)
    if not matches:
        print(f"no GameObject matching '{args.name}' in {args.file}")
        return
    print(f"{len(matches)} match(es) for '{args.name}' in {args.file}:")
    for gid in sorted(matches, key=lambda g: hierarchy_path(model, g)):
        go = model.gameobjects[gid]
        path = hierarchy_path(model, gid)
        anchor = f"  L{go.name_line}" if go.name_line > 0 else ""
        print(f"  [{gid}]  {path}{anchor}")


# ========================================================================
# inspect
# ========================================================================

def cmd_inspect(args):
    model = parse_file(Path(args.file))
    if not model.gameobjects:
        print(f"# {args.file} has no GameObjects — redirecting to `show`.", file=sys.stderr)
        args.force = False
        args.fields = getattr(args, "fields", False)
        return cmd_show(args)
    target = _resolve_target(model, args.target)
    if target is None:
        sys.exit(f"no GameObject matching '{args.target}' in {args.file}")
    go = model.gameobjects[target]
    path = hierarchy_path(model, target)
    print(f"GameObject: {go.name}  [fileID: {go.file_id}]")
    print(f"Path: {path}")
    if go.name_line > 0:
        print(f"Name anchor: {args.file}:L{go.name_line}")
    print(f"Components: {len(go.component_fileids)}")
    print()
    for cid in go.component_fileids:
        comp_doc = model.by_fileid.get(cid)
        if comp_doc is None:
            print(f"  [{cid}]  <missing>")
            continue
        cls = component_class_name(model, comp_doc)
        header_line = comp_doc.start + 1  # 1-based
        print(f"  {cls}  [fileID: {cid}]  L{header_line}")
        _print_component_fields(model, comp_doc, show_all=args.fields)
        print()


def _resolve_target(model: SceneModel, target: str) -> int | None:
    # try numeric fileID
    try:
        fid = int(target)
        if fid in model.gameobjects:
            return fid
    except ValueError:
        pass
    # fall back to substring name match
    matches = find_gameobjects(model, target)
    if not matches:
        return None
    if len(matches) > 1:
        print(f"warning: '{target}' matches {len(matches)} GameObjects, using first "
              f"({hierarchy_path(model, matches[0])})", file=sys.stderr)
    return matches[0]


def _print_component_fields(model: SceneModel, doc: Doc, show_all: bool = False):
    """Walk through doc body and print every content line with L<N> anchor.

    Preserves nested YAML structure (list items, sub-blocks) verbatim from the
    file — the printed line after `L<N>:` is exactly what sits in the file
    (stripped of original indent, with uniform re-indentation). Asset and
    local references get a `→ <resolved>` suffix.

    Hidden top-level fields (m_ObjectHideFlags, m_Script, etc.) are skipped
    together with their entire subtree unless `show_all` is True.
    """
    hide_until: int | None = None
    for i in range(doc.start + 2, doc.end):
        line = model.lines[i]
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent < doc.top_indent:
            break
        # exit hidden block when we dedent back to or past the hiding indent
        if hide_until is not None and indent <= hide_until:
            hide_until = None
        if hide_until is not None:
            continue
        # skip top-level hidden fields (and their subtrees)
        if indent == doc.top_indent and not show_all:
            m = FIELD_LINE_RE.match(line)
            if m and m.group(2) in HIDDEN_FIELDS:
                hide_until = indent
                continue
        # resolve any asset / local ref in the line; Unity sometimes wraps long
        # refs across two lines, so join forward until the closing `}` if needed.
        probe = line
        if "{fileID:" in line and "}" not in line[line.find("{fileID:"):]:
            for j in range(i + 1, min(i + 4, doc.end)):
                probe += " " + model.lines[j].strip()
                if "}" in model.lines[j]:
                    break
        resolved_suffix = ""
        m_asset = ASSET_REF_RE.search(probe)
        if m_asset:
            fid = int(m_asset.group(1))
            guid = m_asset.group(2)
            resolved_suffix = f"  → {format_asset_ref(fid, guid)}"
        else:
            m_local = LOCAL_REF_RE.search(probe)
            if m_local:
                fid = int(m_local.group(1))
                if fid != 0:
                    resolved_suffix = f"  → {format_local_ref(model, fid)}"
        rel_indent = indent - doc.top_indent
        indent_str = " " * (rel_indent + 2)
        line_no = i + 1
        print(f"  L{line_no}:{indent_str}{stripped}{resolved_suffix}")


def _format_value(model: SceneModel, raw_value: str) -> str:
    """Resolve refs in a value string into human-readable form."""
    s = raw_value.strip()
    if not s:
        return ""
    # asset ref with guid
    m = ASSET_REF_RE.search(s)
    if m:
        fid = int(m.group(1))
        guid = m.group(2)
        resolved = format_asset_ref(fid, guid)
        return f"{s}  → {resolved}"
    # local ref
    m2 = LOCAL_REF_RE.search(s)
    if m2:
        fid = int(m2.group(1))
        if fid == 0:
            return s
        resolved = format_local_ref(model, fid)
        return f"{s}  → {resolved}"
    return s


# ========================================================================
# path
# ========================================================================

def _doc_display_name(lines: list[str], doc: Doc) -> str:
    """Try to get m_Name of a document (for headers)."""
    idx = _doc_field_line(lines, doc, "m_Name")
    if idx is None:
        return ""
    return lines[idx].split(":", 1)[1].strip()


def _doc_class_label(model: SceneModel, doc: Doc) -> str:
    """Human-readable class name for any Unity YAML document."""
    if doc.class_id == 114:
        idx = _doc_field_line(model.lines, doc, "m_Script")
        if idx is not None:
            m = GUID_RE.search(model.lines[idx])
            if m:
                resolved = resolve_script_class(m.group(1))
                if resolved:
                    return resolved
        return "MonoBehaviour"
    return UNITY_CLASSES.get(doc.class_id, f"ClassID:{doc.class_id}")


def cmd_show(args):
    model = parse_file(Path(args.file))
    if model.gameobjects and not args.force:
        print(f"# {args.file} contains {len(model.gameobjects)} GameObject(s).")
        print(f"# Use `tree {args.file}` to see hierarchy, or "
              f"`inspect {args.file} <name>` for a specific GameObject.")
        print(f"# Pass --force to dump all documents anyway.")
        return
    # documents worth showing: skip Transforms/RectTransforms (no content value on their own)
    # and skip stripped documents
    shown: list[Doc] = []
    for doc in model.docs:
        if doc.stripped:
            continue
        if doc.class_id in (4, 224):
            continue  # Transform/RectTransform — only useful in hierarchy context
        shown.append(doc)
    if not shown:
        print(f"# {args.file} has no inspectable documents.")
        return
    print(f"{args.file}  [{len(shown)} document(s)]")
    for i, doc in enumerate(shown):
        cls = _doc_class_label(model, doc)
        name = _doc_display_name(model.lines, doc)
        header_line = doc.start + 1  # 1-based
        tag = f"{cls}"
        if name:
            tag += f": {name}"
        print()
        print(f"=== {tag}  [fileID: {doc.file_id}]  L{header_line} ===")
        _print_component_fields(model, doc, show_all=args.fields)


def cmd_path(args):
    model = parse_file(Path(args.file))
    try:
        fid = int(args.fileid)
    except ValueError:
        sys.exit(f"not a valid fileID: {args.fileid}")
    if fid in model.gameobjects:
        print(hierarchy_path(model, fid))
        return
    # maybe a component fileID — resolve to its GameObject
    doc = model.by_fileid.get(fid)
    if doc is None:
        sys.exit(f"fileID {fid} not found in {args.file}")
    if doc.class_id in (4, 224):
        t = model.transforms.get(fid)
        if t and t.gameobject_fileid in model.gameobjects:
            go_path = hierarchy_path(model, t.gameobject_fileid)
            print(f"{go_path}  (Transform of '{model.gameobjects[t.gameobject_fileid].name}')")
            return
    # component — find its GameObject via m_GameObject
    m_go_idx = _doc_field_line(model.lines, doc, "m_GameObject")
    if m_go_idx is not None:
        m = FILEID_RE.search(model.lines[m_go_idx])
        if m:
            go_id = int(m.group(1))
            if go_id in model.gameobjects:
                cls = component_class_name(model, doc)
                print(f"{hierarchy_path(model, go_id)}  ({cls} on '{model.gameobjects[go_id].name}')")
                return
    cls = UNITY_CLASSES.get(doc.class_id, f"ClassID:{doc.class_id}")
    print(f"<{cls} fileID:{fid}> (no GameObject owner)")


# ========================================================================
# argparse
# ========================================================================

def main():
    p = argparse.ArgumentParser(
        description="Read-only content inspection for Unity YAML files "
                    "(scenes, prefabs, ScriptableObjects, materials)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("tree", help="print GameObject hierarchy")
    sp.add_argument("file", help="path to .unity or .prefab")
    sp.add_argument("--root", help="show only subtree under GameObject with this name")
    sp.add_argument("--depth", type=int, help="limit tree depth")
    sp.add_argument("--filter", help="show only branches containing a GO with this component class")
    sp.add_argument("--expand-components", action="store_true",
                    help="print each component on its own line instead of inline")
    sp.set_defaults(func=cmd_tree)

    sp = sub.add_parser("find", help="find GameObjects by name (substring)")
    sp.add_argument("file")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("inspect", help="dump components and fields of a GameObject")
    sp.add_argument("file")
    sp.add_argument("target", help="fileID (number) or GameObject name (substring)")
    sp.add_argument("--fields", action="store_true",
                    help="show all fields including usually-hidden ones")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("path", help="fileID → hierarchy path")
    sp.add_argument("file")
    sp.add_argument("fileid", help="numeric fileID from the file")
    sp.set_defaults(func=cmd_path)

    sp = sub.add_parser("show", help="dump contents of a ScriptableObject / Material / single-doc asset")
    sp.add_argument("file", help="path to a .asset / .mat / other non-GameObject YAML asset")
    sp.add_argument("--fields", action="store_true",
                    help="show all fields including usually-hidden ones")
    sp.add_argument("--force", action="store_true",
                    help="dump documents even if the file contains GameObjects")
    sp.set_defaults(func=cmd_show)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
