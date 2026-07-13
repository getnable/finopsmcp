"""
HCL source patcher — adds missing tags to Terraform resource blocks.

Algorithm for each resource:
  1. Locate `resource "type" "name" {` in the .tf source file.
  2. Find the matching closing `}` using brace counting.
  3. If a `tags = {` block exists: insert missing keys before its closing `}`.
  4. If no tags block: inject a complete `tags = { ... }` before the resource's `}`.
  5. FIXME placeholder values: "Name" tag -> resource name, others -> "FIXME-<key>".
"""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Matches `resource "type" "name" {` at the start of a line (source, not diff)
_TF_RESOURCE_SOURCE_RE = re.compile(
    r'^resource\s+"(?P<type>[^"]+)"\s+"(?P<name>[^"]+)"\s*\{',
    re.MULTILINE,
)

# Matches heredoc opening: <<EOT, <<-EOT, <<'EOT', <<-'EOT'
_HEREDOC_OPEN_RE = re.compile(r"<<-?'?([A-Z_][A-Z_0-9]*)'?")


def _contains_heredoc(text: str) -> bool:
    """Return True if text contains a HCL heredoc (<<EOT ... EOT)."""
    return bool(_HEREDOC_OPEN_RE.search(text))


# ── Brace helpers ─────────────────────────────────────────────────────────────

def _find_matching_brace(content: str, open_pos: int) -> int:
    """Return position of the `}` that closes the `{` at open_pos.

    Handles nested braces. Returns -1 if not found.
    Ignores `{` / `}` inside:
      - single-line double-quoted strings
      - heredoc blocks (<<EOT ... EOT or <<-EOT ... EOT)
    """
    depth = 0
    i = open_pos
    in_string = False
    while i < len(content):
        c = content[i]

        # Detect heredoc opening: <<[-]['"]?MARKER
        hm = _HEREDOC_OPEN_RE.match(content, i)
        if hm and not in_string:
            marker = hm.group(1)
            # Skip past the opening line
            nl = content.find("\n", hm.end())
            if nl == -1:
                break
            i = nl + 1
            # Scan lines until we find the terminator alone on a line
            term_re = re.compile(r"^[ \t]*" + re.escape(marker) + r"[ \t]*$", re.MULTILINE)
            tm = term_re.search(content, i)
            if tm:
                i = tm.end()
                # Step past the trailing newline if present
                if i < len(content) and content[i] == "\n":
                    i += 1
            else:
                # Unterminated heredoc — bail out
                return -1
            continue

        if c == '"' and (i == 0 or content[i - 1] != "\\"):
            in_string = not in_string
        elif not in_string:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


# ── Core patching logic ───────────────────────────────────────────────────────

def _fixme_value(key: str, res_name: str) -> str:
    """Return a placeholder value for a missing tag."""
    return res_name if key == "Name" else f"FIXME-{key}"


def _patch_block(block: str, res_name: str, missing_tags: list[str]) -> str:
    """Return a patched version of a resource block with missing tags added.

    block is the raw text from `resource "..." "..." {` to its closing `}`.
    Blocks containing heredocs are returned unchanged — heredoc support is
    not yet implemented and patching them blindly would corrupt the file.
    """
    if _contains_heredoc(block):
        log.warning(
            "Resource '%s' contains a heredoc — skipping tag patch to avoid corruption",
            res_name,
        )
        return block

    # Look for an existing tags = { ... } block
    tags_re = re.compile(r'^(?P<indent>\s+)tags\s*=\s*\{', re.MULTILINE)
    tags_match = tags_re.search(block)

    if tags_match:
        indent = tags_match.group("indent")
        val_indent = indent + "  "
        # Find the opening { of the tags value block
        tags_open = block.index("{", tags_match.start())
        tags_close = _find_matching_brace(block, tags_open)
        if tags_close == -1:
            log.warning("Could not find closing brace of tags block — skipping")
            return block

        # Insert before the `\n<indent>}` line so indentation is preserved
        newline_before_close = block.rfind("\n", 0, tags_close)
        insert_at = newline_before_close if newline_before_close != -1 else tags_close

        new_entries = "".join(
            f'\n{val_indent}{key} = "{_fixme_value(key, res_name)}"'
            for key in missing_tags
        )
        return block[:insert_at] + new_entries + block[insert_at:]

    else:
        # Inject a brand-new tags block before the resource's closing }
        res_open = block.index("{")
        res_close = _find_matching_brace(block, res_open)
        if res_close == -1:
            log.warning("Could not find closing brace of resource block — skipping")
            return block

        tag_lines = "\n".join(
            f'    {key} = "{_fixme_value(key, res_name)}"'
            for key in missing_tags
        )
        injection = f'\n  tags = {{\n{tag_lines}\n  }}\n'
        return block[:res_close] + injection + block[res_close:]


def _build_patched_content(original: str, violations: list[dict]) -> str:
    """Apply all violation patches to the file content and return the result."""
    # Build a lookup (type, name) -> missing_tags
    viol_map: dict[tuple[str, str], list[str]] = {
        (v["type"], v["name"]): v["missing_tags"]
        for v in violations
    }

    patched = original
    offset = 0  # track cumulative character offset from prior insertions

    for match in _TF_RESOURCE_SOURCE_RE.finditer(original):
        key = (match.group("type"), match.group("name"))
        if key not in viol_map:
            continue

        missing_tags = viol_map[key]

        # Find block boundaries in the *original* string
        open_pos = original.index("{", match.start())
        close_pos = _find_matching_brace(original, open_pos)
        if close_pos == -1:
            log.warning("Could not find block end for %s.%s — skipping", *key)
            continue

        block_start = match.start()
        block_end = close_pos + 1
        block_content = original[block_start:block_end]

        patched_block = _patch_block(block_content, key[1], missing_tags)
        if patched_block == block_content:
            continue

        adj_start = block_start + offset
        adj_end = block_end + offset
        patched = patched[:adj_start] + patched_block + patched[adj_end:]
        offset += len(patched_block) - len(block_content)

    return patched


# ── File discovery ────────────────────────────────────────────────────────────

def _locate_by_tf_dir(tf_dir: str, violations: list[dict]) -> dict[str, list[dict]]:
    """Scan all .tf files in tf_dir and map file_path -> violations that live there.

    Used when violations don't have a populated file_path (e.g. read from tfstate).
    """
    by_file: dict[str, list[dict]] = {}

    # First pass: use explicit file_path if available
    unlocated: list[dict] = []
    for v in violations:
        fp = v.get("file_path", "")
        if fp:
            by_file.setdefault(fp, []).append(v)
        else:
            unlocated.append(v)

    if not unlocated:
        return by_file

    # Second pass: scan .tf files to find remaining violations
    tf_files = list(Path(tf_dir).rglob("*.tf"))
    for tf_file in tf_files:
        try:
            content = tf_file.read_text()
        except OSError:
            continue
        file_str = str(tf_file)
        for v in unlocated:
            pattern = re.compile(
                rf'^resource\s+"{re.escape(v["type"])}"\s+"{re.escape(v["name"])}"\s*\{{',
                re.MULTILINE,
            )
            if pattern.search(content):
                by_file.setdefault(file_str, []).append(v)

    return by_file


# ── Public API ────────────────────────────────────────────────────────────────

def patch_file(file_path: str, violations: list[dict]) -> str | None:
    """Return a unified diff string for the file if changes are needed, else None."""
    original = Path(file_path).read_text()
    patched = _build_patched_content(original, violations)
    if patched == original:
        return None

    orig_lines = original.splitlines(keepends=True)
    patched_lines = patched.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines, patched_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
    )
    return "".join(diff)


def generate_all_fixes(tf_dir: str, violations: list[dict]) -> dict[str, str]:
    """Return {file_path: unified_diff} for every .tf file that needs patching."""
    by_file = _locate_by_tf_dir(tf_dir, violations)
    result: dict[str, str] = {}
    for file_path, file_violations in by_file.items():
        diff = patch_file(file_path, file_violations)
        if diff:
            result[file_path] = diff
    return result


def apply_fixes(tf_dir: str, violations: list[dict]) -> list[str]:
    """Write patched content to disk for all affected .tf files.

    Returns the list of file paths that were modified.
    """
    by_file = _locate_by_tf_dir(tf_dir, violations)
    modified: list[str] = []

    for file_path, file_violations in by_file.items():
        try:
            original = Path(file_path).read_text()
        except OSError as exc:
            log.error("Could not read %s: %s", file_path, exc)
            continue
        patched = _build_patched_content(original, file_violations)
        if patched == original:
            continue
        Path(file_path).write_text(patched)
        modified.append(file_path)
        log.info("patched %s (%d violations)", file_path, len(file_violations))

    return modified


# ── Rightsizing: instance type / class patching ───────────────────────────────

# Maps Terraform resource type -> the attribute that controls instance sizing
_INSTANCE_SIZE_ATTR: dict[str, str] = {
    "aws_instance":                      "instance_type",
    "aws_db_instance":                   "instance_class",
    "aws_rds_cluster_instance":          "instance_class",
    "aws_elasticache_cluster":           "node_type",
    "aws_elasticache_replication_group": "node_type",
    "aws_redshift_cluster":              "node_type",
}


def _patch_instance_size(block: str, attr: str, new_value: str) -> tuple[str, bool]:
    """Replace the sizing attribute value in a resource block.

    Returns (patched_block, was_changed).
    Only replaces the attribute if it already exists in the block — never injects.
    Blocks containing heredocs are returned unchanged.
    """
    if _contains_heredoc(block):
        log.warning(
            "Block contains a heredoc — skipping instance size patch for '%s'", attr
        )
        return block, False

    pattern = re.compile(
        rf'^(?P<prefix>\s+{re.escape(attr)}\s*=\s*")(?P<val>[^"]+)"',
        re.MULTILINE,
    )
    match = pattern.search(block)
    if not match:
        return block, False
    start = match.start("val")
    end = match.end("val")
    return block[:start] + new_value + block[end:], True


def find_resource_file(tf_dir: str, resource_type: str, resource_name: str) -> str | None:
    """Scan .tf files to find which file declares a specific resource.

    Returns the absolute path of the first matching file, or None.
    """
    pattern = re.compile(
        rf'^resource\s+"{re.escape(resource_type)}"\s+"{re.escape(resource_name)}"\s*\{{',
        re.MULTILINE,
    )
    for tf_file in sorted(Path(tf_dir).rglob("*.tf")):
        try:
            content = tf_file.read_text()
        except OSError:
            continue
        if pattern.search(content):
            return str(tf_file)
    return None


def _locate_sizing(
    content: str, resource_type: str, resource_name: str
) -> tuple[int, str] | None:
    """Locate the instance-sizing attribute for a resource block in HCL source.

    Returns (line_number_1based, current_value) for the literal sizing attribute
    (instance_type / instance_class / node_type) of the given resource, or None if
    the block is absent, has no size-attr mapping, or the value is not a quoted
    literal (e.g. references a variable or module output).
    """
    attr = _INSTANCE_SIZE_ATTR.get(resource_type)
    if not attr:
        return None
    for match in _TF_RESOURCE_SOURCE_RE.finditer(content):
        if match.group("type") != resource_type or match.group("name") != resource_name:
            continue
        open_pos = content.index("{", match.start())
        close_pos = _find_matching_brace(content, open_pos)
        if close_pos == -1:
            continue
        block = content[match.start():close_pos + 1]
        pat = re.compile(
            rf'^(?P<prefix>\s+{re.escape(attr)}\s*=\s*")(?P<val>[^"]+)"',
            re.MULTILINE,
        )
        m = pat.search(block)
        if not m:
            continue
        val_offset = match.start() + m.start("val")
        line_no = content.count("\n", 0, val_offset) + 1
        return line_no, m.group("val")
    return None


def find_sizing_attr_line(
    file_path: str, resource_type: str, resource_name: str
) -> tuple[int, str] | None:
    """Return (1-based line number, current value) of a resource's sizing literal,
    or None if the block or a literal sizing attribute cannot be found."""
    try:
        content = Path(file_path).read_text()
    except OSError:
        return None
    return _locate_sizing(content, resource_type, resource_name)


def extract_sizing_value(
    content: str, resource_type: str, resource_name: str
) -> str | None:
    """Return the sizing literal for a resource from in-memory HCL content, or None.
    Used to read the pre-change value from `git show <sha>^:<file>` output."""
    result = _locate_sizing(content, resource_type, resource_name)
    return result[1] if result else None


def generate_rightsizing_diff(
    file_path: str,
    resource_type: str,
    resource_name: str,
    new_value: str,
) -> str | None:
    """Return a unified diff for changing an instance type/class, or None if unchanged.

    Returns None (with a warning) if the target resource block contains a heredoc.
    Full heredoc support is not yet implemented; patching blindly would corrupt the file.
    """
    attr = _INSTANCE_SIZE_ATTR.get(resource_type)
    if not attr:
        log.warning("No size attribute mapping for resource type %s", resource_type)
        return None

    original = Path(file_path).read_text()
    patched = original
    offset = 0

    for match in _TF_RESOURCE_SOURCE_RE.finditer(original):
        if match.group("type") != resource_type or match.group("name") != resource_name:
            continue
        open_pos = original.index("{", match.start())
        close_pos = _find_matching_brace(original, open_pos)
        if close_pos == -1:
            log.warning("Could not find block end for %s.%s", resource_type, resource_name)
            continue

        block_start = match.start()
        block_end = close_pos + 1
        block = original[block_start:block_end]

        if _contains_heredoc(block):
            log.warning(
                "Resource %s.%s contains a heredoc — skipping rightsizing diff to avoid corruption",
                resource_type, resource_name,
            )
            return None

        patched_block, changed = _patch_instance_size(block, attr, new_value)
        if not changed:
            continue

        adj_start = block_start + offset
        adj_end = block_end + offset
        patched = patched[:adj_start] + patched_block + patched[adj_end:]
        offset += len(patched_block) - len(block)

    if patched == original:
        return None

    orig_lines = original.splitlines(keepends=True)
    patched_lines = patched.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines, patched_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
    )
    return "".join(diff)


def apply_rightsizing_fix(
    file_path: str,
    resource_type: str,
    resource_name: str,
    new_value: str,
) -> bool:
    """Write the patched instance type/class to disk.

    Returns True if the file was modified, False if unchanged or a heredoc was detected.
    Refuses to patch resource blocks that contain heredocs — use generate_rightsizing_diff
    to inspect the block first and apply the change manually if needed.
    """
    attr = _INSTANCE_SIZE_ATTR.get(resource_type)
    if not attr:
        return False

    original = Path(file_path).read_text()
    patched = original
    offset = 0

    for match in _TF_RESOURCE_SOURCE_RE.finditer(original):
        if match.group("type") != resource_type or match.group("name") != resource_name:
            continue
        open_pos = original.index("{", match.start())
        close_pos = _find_matching_brace(original, open_pos)
        if close_pos == -1:
            continue

        block_start = match.start()
        block_end = close_pos + 1
        block = original[block_start:block_end]

        if _contains_heredoc(block):
            log.warning(
                "Resource %s.%s contains a heredoc — refusing to apply rightsizing fix to avoid corruption",
                resource_type, resource_name,
            )
            return False

        patched_block, changed = _patch_instance_size(block, attr, new_value)
        if not changed:
            continue

        adj_start = block_start + offset
        adj_end = block_end + offset
        patched = patched[:adj_start] + patched_block + patched[adj_end:]
        offset += len(patched_block) - len(block)

    if patched == original:
        return False

    Path(file_path).write_text(patched)
    log.info("Applied rightsizing fix to %s (%s.%s -> %s)", file_path, resource_type, resource_name, new_value)
    return True
