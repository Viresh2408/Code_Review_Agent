"""
AST parsing utilities using tree-sitter.
"""

from __future__ import annotations

import re

import structlog
from agents.schemas import ChangedFile
from tree_sitter_languages import get_parser

logger = structlog.get_logger(__name__)

# Mapping from file extensions or languages to tree-sitter language identifiers
LANGUAGE_MAP = {
    "py": "python",
    "python": "python",
    "js": "javascript",
    "javascript": "javascript",
    "ts": "typescript",
    "typescript": "typescript",
    "jsx": "javascript",
    "tsx": "typescript",
}


def parse_changed_lines(diff_hunk: str) -> list[int]:
    """
    Parse a unified diff hunk and return the list of 1-indexed changed line numbers
    in the new (post-image) file.
    """
    changed_lines = []
    lines = diff_hunk.splitlines()
    if not lines:
        return []

    # Match hunk header: @@ -old_start,old_len +new_start,new_len @@
    header_match = re.match(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@", lines[0])
    if not header_match:
        return []

    new_line_start = int(header_match.group(1))
    current_line = new_line_start

    for line in lines[1:]:
        if line.startswith("+"):
            changed_lines.append(current_line)
            current_line += 1
        elif line.startswith("-"):
            # Removed in new file. The location in the new file is current_line.
            changed_lines.append(current_line)
            continue
        else:
            # Context line (starts with space or empty)
            current_line += 1

    return changed_lines


def find_deepest_node(node, line: int):
    """
    Find the deepest node in the AST that covers the specified 0-indexed line.
    """
    if not (node.start_point[0] <= line <= node.end_point[0]):
        return None

    # Check children
    for child in node.children:
        res = find_deepest_node(child, line)
        if res is not None:
            return res
    return node


def get_enclosing_definition(node):
    """
    Walk up the parent chain of the node to find the enclosing function or class definition.
    """
    def_types = {
        # Python definitions
        "function_definition",
        "class_definition",
        # JS/TS definitions
        "function_declaration",
        "class_declaration",
        "method_definition",
        "arrow_function",
        "generator_function_declaration",
        "function_expression",
        "interface_declaration",
        "enum_declaration",
    }

    current = node
    while current is not None:
        if current.type in def_types:
            return current
        current = current.parent
    return None


def get_node_name(node, source_bytes: bytes) -> str:
    """
    Extract the name of a function or class definition node.
    """
    # Try using named child with field 'name'
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node.text.decode("utf-8", errors="ignore")

    # Fallback: search children for identifier type
    for child in node.children:
        if child.type in ("identifier", "property_identifier", "type_identifier"):
            return child.text.decode("utf-8", errors="ignore")

    return "<anonymous>"


def get_node_signature(node, source_bytes: bytes) -> str:
    """
    Extract the signature of a definition node (up to the start of its body).
    """
    body_node = node.child_by_field_name("body") or node.child_by_field_name("block")
    if body_node is not None:
        # Get everything from node start to body start
        start_byte = node.start_byte
        body_start_byte = body_node.start_byte
        sig_bytes = source_bytes[start_byte:body_start_byte]
        sig_text = sig_bytes.decode("utf-8", errors="ignore").strip()
        # Remove trailing colons or opening brackets if they are captured
        if sig_text.endswith(":"):
            sig_text = sig_text[:-1].strip()
        return sig_text

    # Fallback: return the first line of the node
    node_text = node.text.decode("utf-8", errors="ignore")
    first_line = node_text.splitlines()[0] if node_text else ""
    return first_line.strip()


def get_body_preview(node, source_bytes: bytes, max_lines: int = 5) -> str:
    """
    Get a truncated preview of the body/block of a definition node.
    """
    body_node = node.child_by_field_name("body") or node.child_by_field_name("block")
    if body_node is None:
        return ""

    body_text = body_node.text.decode("utf-8", errors="ignore")
    lines = body_text.splitlines()

    # Clean up empty lines or comments at the start of block if any
    if lines:
        preview_lines = lines[:max_lines]
        preview = "\n".join(preview_lines)
        if len(lines) > max_lines:
            preview += "\n    ... (truncated)"
        return preview

    return ""


def generate_ast_summary(changed_definitions: list, source_bytes: bytes) -> str:
    """
    Generate a markdown summary of changed classes and functions.
    """
    if not changed_definitions:
        return "No functions or classes were changed in this file."

    summary_parts = []
    for node in changed_definitions:
        name = get_node_name(node, source_bytes)
        sig = get_node_signature(node, source_bytes)
        body = get_body_preview(node, source_bytes)

        node_type = (
            "Function/Method"
            if "function" in node.type or "method" in node.type or "arrow" in node.type
            else "Class/Interface"
        )

        part = f"### {node_type}: `{name}`\n"
        part += f"**Signature:**\n```python\n{sig}\n```\n"
        if body:
            part += f"**Body Preview:**\n```python\n{body}\n```\n"
        summary_parts.append(part)

    return "\n".join(summary_parts)


def get_changed_definitions(root_node, diff_hunks: list[str]) -> list:
    """
    Given a root tree-sitter node and unified diff hunks, identify the
    enclosing class or function definition nodes that contain the changed lines.
    """
    changed_lines = []
    for hunk in diff_hunks:
        changed_lines.extend(parse_changed_lines(hunk))

    seen_definitions = set()
    changed_definitions = []

    for line in changed_lines:
        ts_line = line - 1  # tree-sitter uses 0-indexed line numbers
        deepest_node = find_deepest_node(root_node, ts_line)
        if deepest_node is not None:
            enclosing = get_enclosing_definition(deepest_node)
            if enclosing is not None:
                key = (enclosing.start_byte, enclosing.end_byte)
                if key not in seen_definitions:
                    seen_definitions.add(key)
                    changed_definitions.append(enclosing)
    return changed_definitions


def parse_and_summarize_file(
    file_path: str,
    language: str,
    source_content: str,
    diff_hunks: list[str],
) -> ChangedFile:
    """
    Parse source code into AST, map changed diff line ranges to definitions,
    and return a structured ChangedFile Pydantic model.
    """
    mapped_lang = LANGUAGE_MAP.get(language.lower(), LANGUAGE_MAP.get(file_path.split(".")[-1], ""))

    if not mapped_lang or not source_content:
        return ChangedFile(
            path=file_path,
            language=language,
            diff_hunks=diff_hunks,
            ast_summary="AST parsing not supported for this file type or empty file.",
            blast_radius=[],
        )

    try:
        parser = get_parser(mapped_lang)
        source_bytes = source_content.encode("utf-8")
        tree = parser.parse(source_bytes)
        root_node = tree.root_node

        # Get definition nodes covering changed lines
        changed_definitions = get_changed_definitions(root_node, diff_hunks)

        # Generate the formatted AST summary
        ast_summary = generate_ast_summary(changed_definitions, source_bytes)

    except Exception as exc:
        logger.error("ast_parsing_failed", file_path=file_path, error=str(exc))
        ast_summary = f"Error parsing AST: {exc}"

    return ChangedFile(
        path=file_path,
        language=language,
        diff_hunks=diff_hunks,
        ast_summary=ast_summary,
        blast_radius=[],
    )
