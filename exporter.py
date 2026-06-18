"""
Session exporters: Obsidian-flavoured zip of linked Markdown files, or a single
flat Markdown document with depth-based heading nesting.

Pure stdlib. No FastAPI imports — these are plain functions over the session
dict shape produced by SessionManager.
"""

import hashlib
import io
import re
import zipfile
from datetime import datetime


# ---------- Helpers ----------

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 50) -> str:
    """Lowercase, hyphen-separated, ASCII-only slug. Empty input -> 'untitled'."""
    if not text:
        return "untitled"
    s = text.lower().strip()
    s = _SLUG_STRIP.sub("-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "untitled"


def _short_hash(node_id: str, n: int = 6) -> str:
    """Stable short hash from a node ID — for filename disambiguation."""
    return hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:n]


def _summary(node: dict, max_len: int = 60) -> str:
    """A short human-readable summary of what this node is about."""
    prompt = (node.get("prompt_text") or "").strip()
    if prompt:
        return prompt[:max_len] + ("…" if len(prompt) > max_len else "")
    hl = (node.get("highlighted_text") or "").strip()
    if hl:
        return f'"{hl[:max_len]}"' + ("…" if len(hl) > max_len else "")
    return "(untitled node)"


def _filename_for(node: dict) -> str:
    """Generate a stable filename for a node. Format: {mode}-{slug}-{hash}.md"""
    mode = node.get("prompt_mode", "node") or "node"
    # Prefer the prompt_text; fall back to highlighted_text; else generic
    seed = node.get("prompt_text") or node.get("highlighted_text") or "node"
    slug = slugify(seed, max_len=40)
    return f"{mode}-{slug}-{_short_hash(node['id'])}.md"


def _format_date(iso: str) -> str:
    """Return a friendlier 'YYYY-MM-DD HH:MM' or the raw string on parse failure."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso


def _yaml_escape(value) -> str:
    """Quote a value for safe inclusion in YAML frontmatter."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        # Inline list of strings
        items = ", ".join(_yaml_escape(v) for v in value)
        return f"[{items}]"
    s = str(value)
    # Always double-quote strings; escape backslashes and double quotes.
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def _frontmatter(fields: dict) -> str:
    lines = ["---"]
    for k, v in fields.items():
        lines.append(f"{k}: {_yaml_escape(v)}")
    lines.append("---")
    return "\n".join(lines)


# ---------- Graph traversal ----------

def _build_indexes(session: dict):
    """Pre-compute lookups used during export.

    Returns:
        nodes: dict[node_id -> node_dict]
        edges: list[edge_dict]
        highlights: dict[highlight_id -> highlight_dict]
        children_of: dict[parent_id -> list[child_id]]   (ordered by created_at)
        roots: list[node_id]                              (nodes with no parent)
        filenames: dict[node_id -> str]
    """
    nodes = session.get("nodes", {}) or {}
    edges = session.get("edges", []) or []
    highlights = session.get("highlights", {}) or {}

    children_of: dict[str, list[str]] = {}
    for nid, n in nodes.items():
        pid = n.get("parent_id")
        if pid:
            children_of.setdefault(pid, []).append(nid)

    # Sort children deterministically by created_at, falling back to id
    for pid, kids in children_of.items():
        kids.sort(key=lambda cid: (nodes[cid].get("created_at", ""), cid))

    roots = sorted(
        [nid for nid, n in nodes.items() if not n.get("parent_id")],
        key=lambda nid: (nodes[nid].get("created_at", ""), nid),
    )

    filenames = {nid: _filename_for(n) for nid, n in nodes.items()}
    # Collision guard: if two nodes hashed to the same filename (astronomically unlikely
    # at 6 hex chars, but cheap to defend), append a counter.
    seen: dict[str, int] = {}
    for nid, fname in list(filenames.items()):
        if fname in seen.values():
            count = sum(1 for v in seen.values() if v == fname) + 1
            stem = fname[:-3]  # strip .md
            filenames[nid] = f"{stem}-{count}.md"
        seen[nid] = fname

    return nodes, edges, highlights, children_of, roots, filenames


# ---------- Wikilink substitution ----------

def _inject_wikilinks(
    body: str,
    node_id: str,
    edges: list,
    highlights: dict,
    filenames: dict,
) -> str:
    """Replace the first occurrence of each outgoing highlight's text with a
    wikilink to the corresponding child node file.

    Strategy: collect (text, target_filename) pairs from outgoing edges, sort by
    text length descending so longer highlights win over shorter overlapping
    ones, then replace each at its first occurrence using a non-overlapping
    forward pass.
    """
    pairs = []
    for edge in edges:
        if edge.get("source_node_id") != node_id:
            continue
        hl = highlights.get(edge.get("source_highlight_id") or "")
        target_fname = filenames.get(edge.get("target_node_id") or "")
        if not hl or not target_fname:
            continue
        text = (hl.get("text") or "").strip()
        if not text:
            continue
        pairs.append((text, target_fname))

    if not pairs:
        return body

    # Longest first so a longer highlight isn't broken by an earlier shorter one.
    pairs.sort(key=lambda p: -len(p[0]))

    # Track replaced character ranges to avoid double-wrapping overlaps.
    replaced_ranges: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        for s, e in replaced_ranges:
            if start < e and end > s:
                return True
        return False

    out = body
    for text, target_fname in pairs:
        # Search the *current* string for the text; bail if not found.
        idx = out.find(text)
        while idx != -1 and _overlaps(idx, idx + len(text)):
            idx = out.find(text, idx + 1)
        if idx == -1:
            continue
        stem = target_fname[:-3] if target_fname.endswith(".md") else target_fname
        # Escape pipe in highlight text for the wikilink alias.
        alias = text.replace("|", "\\|").replace("]]", "] ]")
        wikilink = f"[[{stem}|{alias}]]"
        out = out[:idx] + wikilink + out[idx + len(text):]
        # Record the new range. Note the offsets shift for all subsequent matches;
        # since we restart with .find() each loop, we only need to track ranges in
        # the *current* `out`. Update existing recorded ranges past idx.
        shift = len(wikilink) - len(text)
        replaced_ranges = [
            (s + shift if s >= idx else s, e + shift if e > idx else e)
            for s, e in replaced_ranges
        ]
        replaced_ranges.append((idx, idx + len(wikilink)))

    return out


# ---------- Per-node Markdown body ----------

def _render_node_md(
    node: dict,
    session: dict,
    nodes: dict,
    edges: list,
    highlights: dict,
    filenames: dict,
    children_of: dict,
) -> str:
    """Build the full markdown content for one node file."""
    nid = node["id"]
    parent_id = node.get("parent_id")
    child_ids = children_of.get(nid, [])

    fm = {
        "id": nid,
        "session": session.get("id", ""),
        "session_name": session.get("name", ""),
        "mode": node.get("prompt_mode", "initial"),
        "created_at": node.get("created_at", ""),
        "parent": filenames.get(parent_id, "")[:-3] if parent_id else None,
        "children": [filenames[c][:-3] for c in child_ids if c in filenames],
        "highlighted_text": node.get("highlighted_text") or None,
    }
    frontmatter = _frontmatter(fm)

    # Title
    title = _summary(node, max_len=120)

    # Context block
    ctx_lines = []
    prompt_text = (node.get("prompt_text") or "").strip()
    hl_text = (node.get("highlighted_text") or "").strip()
    if prompt_text:
        ctx_lines.append(f"> **Prompt:** {prompt_text}")
    if hl_text:
        ctx_lines.append(f"> **Highlighted:** {hl_text}")
    if node.get("prompt_mode") and node["prompt_mode"] != "initial":
        ctx_lines.append(f"> **Mode:** {node['prompt_mode']}")
    context_block = "\n".join(ctx_lines)

    # Response with wikilinks
    response = node.get("response_text") or ""
    response = _inject_wikilinks(response, nid, edges, highlights, filenames)
    if not response.strip():
        response = "_(no response yet)_"

    # Navigation footer
    nav_lines = []
    if parent_id and parent_id in filenames:
        parent_stem = filenames[parent_id][:-3]
        nav_lines.append(f"← Parent: [[{parent_stem}]]")
    if child_ids:
        child_links = ", ".join(
            f"[[{filenames[c][:-3]}]]" for c in child_ids if c in filenames
        )
        nav_lines.append(f"→ Children: {child_links}")
    nav_lines.append("⌂ Session: [[_index|" + (session.get("name") or "Session") + "]]")
    nav = "\n".join(nav_lines)

    parts = [frontmatter, "", f"# {title}", ""]
    if context_block:
        parts += [context_block, ""]
    parts += [response, "", "---", "", nav, ""]
    return "\n".join(parts)


# ---------- Index file ----------

def _render_index_md(
    session: dict,
    nodes: dict,
    roots: list,
    children_of: dict,
    filenames: dict,
) -> str:
    fm = {
        "id": session.get("id", ""),
        "name": session.get("name", ""),
        "created_at": session.get("created_at", ""),
        "updated_at": session.get("updated_at", ""),
        "node_count": len(nodes),
    }
    out = [_frontmatter(fm), "", f"# {session.get('name') or 'Session'}", ""]
    out.append(f"- **Created:** {_format_date(session.get('created_at', ''))}")
    out.append(f"- **Updated:** {_format_date(session.get('updated_at', ''))}")
    out.append(f"- **Nodes:** {len(nodes)}")
    out.append("")
    out.append("## Graph")
    out.append("")

    if not roots:
        out.append("_(empty session)_")
    else:
        for root in roots:
            _emit_tree(out, root, 0, nodes, children_of, filenames)

    out.append("")
    return "\n".join(out)


def _emit_tree(out, nid, depth, nodes, children_of, filenames):
    indent = "  " * depth
    node = nodes.get(nid)
    if not node:
        return
    stem = filenames[nid][:-3]
    label = _summary(node, max_len=80).replace("|", "\\|")
    out.append(f"{indent}- [[{stem}|{label}]]")
    for child_id in children_of.get(nid, []):
        _emit_tree(out, child_id, depth + 1, nodes, children_of, filenames)


# ---------- Public exporters ----------

def export_obsidian(session: dict) -> bytes:
    """Export a session as a zip of linked markdown files (Obsidian-flavoured).

    Structure:
      {session_slug}/
        _index.md
        {mode}-{slug}-{hash}.md   (one per node)
    """
    nodes, edges, highlights, children_of, roots, filenames = _build_indexes(session)

    session_slug = slugify(session.get("name", "")) or "session"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Index
        index_md = _render_index_md(session, nodes, roots, children_of, filenames)
        zf.writestr(f"{session_slug}/_index.md", index_md)

        # One file per node
        for nid, node in nodes.items():
            content = _render_node_md(
                node, session, nodes, edges, highlights, filenames, children_of
            )
            zf.writestr(f"{session_slug}/{filenames[nid]}", content)

    return buf.getvalue()


def export_single_markdown(session: dict) -> str:
    """Export a session as one Markdown document with depth-based H-nesting.

    Roots become H1, their children H2, etc. Headings clip at H6 (anything deeper
    just gets indented bullet headers).
    """
    nodes, edges, highlights, children_of, roots, filenames = _build_indexes(session)

    out = []
    name = session.get("name") or "Session"
    out.append(f"# {name}")
    out.append("")
    out.append(f"_Exported from Learning Tool on {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    out.append("")
    out.append(f"- **Created:** {_format_date(session.get('created_at', ''))}")
    out.append(f"- **Updated:** {_format_date(session.get('updated_at', ''))}")
    out.append(f"- **Nodes:** {len(nodes)}")
    out.append("")
    out.append("---")
    out.append("")

    if not roots:
        out.append("_(empty session)_")
        return "\n".join(out)

    for root in roots:
        _emit_node_flat(out, root, depth=1, nodes=nodes, edges=edges,
                        highlights=highlights, children_of=children_of)

    return "\n".join(out)


def _emit_node_flat(out, nid, depth, nodes, edges, highlights, children_of):
    node = nodes.get(nid)
    if not node:
        return
    # Heading: H1..H6, then fall back to bold for deeper levels
    h_level = min(depth + 1, 6)  # root nodes at depth 1 → H2
    prefix = "#" * h_level
    title = _summary(node, max_len=120)
    mode = node.get("prompt_mode", "initial")
    mode_tag = f" `[{mode}]`" if mode and mode != "initial" else ""
    out.append(f"{prefix} {title}{mode_tag}")
    out.append("")

    prompt_text = (node.get("prompt_text") or "").strip()
    hl_text = (node.get("highlighted_text") or "").strip()
    if prompt_text and mode == "initial":
        # For initial nodes the title already shows the prompt; skip duplication.
        pass
    elif prompt_text:
        out.append(f"> **Prompt:** {prompt_text}")
        out.append("")
    if hl_text:
        out.append(f"> **Highlighted:** {hl_text}")
        out.append("")

    response = (node.get("response_text") or "").strip()
    if response:
        out.append(response)
    else:
        out.append("_(no response yet)_")
    out.append("")

    for child_id in children_of.get(nid, []):
        _emit_node_flat(out, child_id, depth + 1, nodes, edges, highlights, children_of)
