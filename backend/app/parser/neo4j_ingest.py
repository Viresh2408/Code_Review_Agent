"""
Neo4j ingestion and query module.
Handles AST parsing extraction, symbol resolution, upserting nodes/relationships,
and querying function blast radius with distinct logging.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog
from neo4j import GraphDatabase
from tree_sitter_languages import get_parser

from app.config import get_settings
from app.parser.ast import (
    LANGUAGE_MAP,
    get_changed_definitions,
    get_node_name,
)

logger = structlog.get_logger(__name__)


# ── Neo4j Driver Client Singleton ─────────────────────────────────────────────

_driver = None

def get_neo4j_driver():
    """Return the cached Neo4j driver singleton."""
    global _driver
    if _driver is None:
        settings = get_settings()
        # Initialize driver. If it fails, let the caller catch and log.
        _driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password)
        )
    return _driver


def close_neo4j_driver():
    """Close the Neo4j driver singleton."""
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


# ── In-Memory AST Extraction & Walker ─────────────────────────────────────────

def extract_ast_info(source_bytes: bytes, language: str) -> dict:
    """
    Extract functions (with their nested calls), classes (with parent classes),
    and imports from raw file content using tree-sitter.
    """
    import_sources = []
    classes = []
    functions = []

    mapped_lang = LANGUAGE_MAP.get(language.lower(), "")
    if not mapped_lang:
        return {"classes": classes, "functions": functions, "imports": import_sources}

    try:
        parser = get_parser(mapped_lang)
        tree = parser.parse(source_bytes)
        root_node = tree.root_node
    except Exception as exc:
        logger.error("neo4j_ast_parse_failed", error=str(exc))
        return {"classes": classes, "functions": functions, "imports": import_sources}

    class_context = []
    function_context = []

    def visit(node):
        nonlocal class_context, function_context
        node_type = node.type

        # 1. Imports
        if mapped_lang == "python":
            if node_type == "import_statement":
                name_node = node.child_by_field_name("name")
                if name_node:
                    dotted_name = name_node.text.decode("utf-8", errors="ignore")
                    import_sources.append(dotted_name)
            elif node_type == "import_from_statement":
                module_node = node.child_by_field_name("module_name")
                if module_node:
                    dotted_name = module_node.text.decode("utf-8", errors="ignore")
                    import_sources.append(dotted_name)
        elif mapped_lang in ("javascript", "typescript"):
            if node_type == "import_statement":
                source_node = node.child_by_field_name("source")
                if source_node:
                    fragment = None
                    for child in source_node.children:
                        if child.type == "string_fragment":
                            fragment = child.text.decode("utf-8", errors="ignore")
                            break
                    if not fragment:
                        fragment = source_node.text.decode("utf-8", errors="ignore").strip("'\"")
                    import_sources.append(fragment)

        # 2. Class definition
        is_class = False
        class_name = None
        is_js_ts_class = (mapped_lang in ("javascript", "typescript") and
                          node_type in ("class_declaration", "class_expression"))
        if (mapped_lang == "python" and node_type == "class_definition") or is_js_ts_class:
            is_class = True
            class_name = get_node_name(node, source_bytes)

            inherits_from = []
            if mapped_lang == "python":
                superclasses = node.child_by_field_name("superclasses")
                if superclasses:
                    for child in superclasses.children:
                        if child.type == "identifier":
                            inherits_from.append(child.text.decode("utf-8", errors="ignore"))
            else:  # JS/TS
                heritage = None
                for child in node.children:
                    if child.type == "class_heritage":
                        heritage = child
                        break
                if heritage:
                    for child in heritage.children:
                        if child.type == "identifier":
                            inherits_from.append(child.text.decode("utf-8", errors="ignore"))

            classes.append({
                "name": class_name,
                "inherits_from": inherits_from
            })
            class_context.append(class_name)

        # 3. Function definition
        is_function = False
        func_name = None
        func_types = {
            "function_definition",            # Python
            "function_declaration",           # JS/TS
            "method_definition",              # JS/TS
            "generator_function_declaration", # JS/TS
        }

        # Check for arrow function assigned to variable in JS/TS
        is_arrow = False
        if mapped_lang in ("javascript", "typescript") and node_type == "arrow_function":
            is_arrow = True

        if node_type in func_types or is_arrow:
            is_function = True
            if is_arrow:
                parent = node.parent
                if parent and parent.type == "variable_declarator":
                    name_node = parent.child_by_field_name("name")
                    if name_node:
                        func_name = name_node.text.decode("utf-8", errors="ignore")
                if not func_name:
                    func_name = "<anonymous_arrow>"
            else:
                func_name = get_node_name(node, source_bytes)

            # Qualify with class context
            qualified_name = f"{class_context[-1]}.{func_name}" if class_context else func_name

            functions.append({
                "name": qualified_name,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "calls": []
            })
            function_context.append(qualified_name)

        # 4. Function call
        if (mapped_lang == "python" and node_type == "call") or \
           (mapped_lang in ("javascript", "typescript") and node_type == "call_expression"):
            called_name = None
            func_expr = node.child_by_field_name("function")
            if not func_expr and node.children:
                func_expr = node.children[0]

            if func_expr:
                if func_expr.type == "identifier":
                    called_name = func_expr.text.decode("utf-8", errors="ignore")
                elif func_expr.type == "attribute":  # Python obj.method
                    attr_node = func_expr.child_by_field_name("attribute")
                    if attr_node:
                        called_name = attr_node.text.decode("utf-8", errors="ignore")
                elif func_expr.type == "member_expression":  # JS/TS obj.method
                    prop_node = func_expr.child_by_field_name("property")
                    if prop_node:
                        called_name = prop_node.text.decode("utf-8", errors="ignore")

            if called_name and function_context:
                functions[-1]["calls"].append(called_name)

        # Traverse children
        for child in node.children:
            visit(child)

        # Pop context
        if is_class:
            class_context.pop()
        if is_function:
            function_context.pop()

    visit(root_node)
    return {"classes": classes, "functions": functions, "imports": import_sources}


# ── Best-Effort Import & Path Resolution ──────────────────────────────────────

def resolve_import_path(
    importing_file_path: str, import_source: str, project_root: str
) -> str | None:
    """
    Resolve import path on a best-effort basis relative to disk structure.
    Returns repo-relative path using forward slashes.
    """
    if not import_source:
        return None

    # Normalise root and check paths relative to it
    p_root = Path(project_root).resolve()

    # 1. JS/TS relative imports
    if import_source.startswith((".", "/")):
        importing_dir = os.path.dirname(importing_file_path)
        base_path = os.path.normpath(os.path.join(importing_dir, import_source))

        for ext in ("", ".js", ".ts", ".jsx", ".tsx"):
            candidate = base_path + ext
            abs_cand = p_root / candidate
            if abs_cand.is_file():
                return Path(candidate).as_posix()
            # Index check if it's a directory
            if ext == "":
                for index_ext in (".js", ".ts", ".jsx", ".tsx"):
                    index_cand = os.path.join(base_path, f"index{index_ext}")
                    if (p_root / index_cand).is_file():
                        return Path(index_cand).as_posix()

    # 2. Python imports
    else:
        parts = import_source.split(".")
        if import_source.startswith("."):
            # Count leading dots
            leading_dots = 0
            for char in import_source:
                if char == ".":
                    leading_dots += 1
                else:
                    break

            importing_dir = os.path.dirname(importing_file_path)
            curr_dir = importing_dir
            for _ in range(leading_dots - 1):
                curr_dir = os.path.dirname(curr_dir)

            remaining = parts[leading_dots:]
            base_path = os.path.normpath(os.path.join(curr_dir, *remaining))
        else:
            # Absolute Python import candidate matching
            search_prefixes = ["", "backend", "backend/app"]
            resolved = None
            for prefix in search_prefixes:
                if prefix:
                    candidate = os.path.normpath(os.path.join(prefix, *parts))
                else:
                    candidate = os.path.normpath(os.path.join(*parts))
                for ext in (".py", "/__init__.py"):
                    full_path = candidate + ext
                    if (p_root / full_path).is_file():
                        resolved = Path(full_path).as_posix()
                        break
                if resolved:
                    break
            if resolved:
                return resolved

            # Fallback path if not found
            base_path = os.path.normpath(os.path.join("backend/app", *parts))

        # Check Python file candidate
        for ext in (".py", "/__init__.py"):
            full_path = base_path + ext
            if (p_root / full_path).is_file():
                return Path(full_path).as_posix()

    return None


# ── Neo4j Ingestion Operations ────────────────────────────────────────────────

def ingest_file_to_neo4j(
    file_path: str,
    language: str,
    source_content: str,
    repo_id: str,
    project_root: str | None = None
) -> None:
    """
    Ingest the file contents, class/function definitions, calls, imports, and inheritance
    into the local Neo4j instance. Enforces idempotency via cleanups on re-runs.
    """
    if not project_root:
        project_root = str(Path(__file__).resolve().parents[3])

    source_bytes = source_content.encode("utf-8")
    info = extract_ast_info(source_bytes, language)

    driver = get_neo4j_driver()
    with driver.session() as session:
        session.execute_write(_ingest_tx, file_path, language, repo_id, info, project_root)


def _ingest_tx(
    tx, file_path: str, language: str, repo_id: str, info: dict, project_root: str
) -> None:
    # 1. Idempotency cleanup: Delete old functions/classes defined in this file to avoid stale nodes
    tx.run(
        """
        MATCH (f:File {path: $file_path, repo_id: $repo_id})
        OPTIONAL MATCH (fn:Function)-[:DEFINED_IN]->(f)
        DETACH DELETE fn
        WITH f
        OPTIONAL MATCH (c:Class {file_path: $file_path})
        DETACH DELETE c
        """,
        file_path=file_path,
        repo_id=repo_id
    )

    # 2. Merge File Node
    tx.run(
        """
        MERGE (file:File {path: $file_path, repo_id: $repo_id})
        SET file.language = $language
        """,
        file_path=file_path,
        repo_id=repo_id,
        language=language
    )

    # 3. Merge Class Nodes & Inheritance
    for cls in info["classes"]:
        class_name = cls["name"]
        tx.run(
            """
            MERGE (c:Class {name: $class_name, file_path: $file_path})
            """,
            class_name=class_name,
            file_path=file_path
        )

        for parent_name in cls["inherits_from"]:
            tx.run(
                """
                MERGE (parent:Class {name: $parent_name})
                WITH parent
                MATCH (c:Class {name: $class_name, file_path: $file_path})
                MERGE (c)-[:INHERITS_FROM]->(parent)
                """,
                class_name=class_name,
                file_path=file_path,
                parent_name=parent_name
            )

    # 4. Merge Function Definitions & link DEFINED_IN File
    for fn in info["functions"]:
        func_name = fn["name"]
        tx.run(
            """
            MERGE (fn:Function {name: $func_name, file_path: $file_path})
            SET fn.start_line = $start_line, fn.end_line = $end_line
            WITH fn
            MATCH (file:File {path: $file_path, repo_id: $repo_id})
            MERGE (fn)-[:DEFINED_IN]->(file)
            """,
            func_name=func_name,
            file_path=file_path,
            repo_id=repo_id,
            start_line=fn["start_line"],
            end_line=fn["end_line"]
        )

    # 5. Merge Imports
    resolved_imports = {}
    for imp in info["imports"]:
        resolved_path = resolve_import_path(file_path, imp, project_root)
        if resolved_path:
            resolved_imports[imp] = resolved_path
            tx.run(
                """
                MERGE (imported:File {path: $imported_path, repo_id: $repo_id})
                WITH imported
                MATCH (file:File {path: $file_path, repo_id: $repo_id})
                MERGE (file)-[:IMPORTS]->(imported)
                """,
                imported_path=resolved_path,
                file_path=file_path,
                repo_id=repo_id
            )

    # 6. Merge Calls
    # Build list of functions defined locally in this file
    local_funcs = {fn["name"] for fn in info["functions"]}

    for fn in info["functions"]:
        func_name = fn["name"]
        for called_name in fn["calls"]:
            # Best-effort resolution:
            # - If called name matches a local function, target file is current file
            # - Else check if it corresponds to a resolved import
            # - Otherwise default to "unknown"
            called_file_path = "unknown"
            if called_name in local_funcs:
                called_file_path = file_path
            else:
                # check imports matching prefix/module
                for imp_name, imp_path in resolved_imports.items():
                    if imp_name.split(".")[-1] == called_name or called_name.startswith(imp_name):
                        called_file_path = imp_path
                        break

            tx.run(
                """
                MERGE (target:Function {name: $called_name, file_path: $called_file_path})
                WITH target
                MATCH (caller:Function {name: $func_name, file_path: $file_path})
                MERGE (caller)-[:CALLS]->(target)
                """,
                called_name=called_name,
                called_file_path=called_file_path,
                func_name=func_name,
                file_path=file_path
            )


# ── Query Blast Radius & Distinct Logging ─────────────────────────────────────

def get_blast_radius(function_name: str, file_path: str, hops: int = 2) -> list[str]:
    """
    Run blast radius query on Neo4j for a function.
    Returns formatted list of calling functions/files with distinct logging categories.
    """
    try:
        driver = get_neo4j_driver()
    except Exception as exc:
        logger.error("neo4j_connection_failed_blast_radius_skipped", error=str(exc))
        return []

    query = """
    MATCH (changed:Function {name: $function_name, file_path: $file_path})
    WITH changed
    OPTIONAL MATCH path = (caller:Function)-[:CALLS*1..2]->(changed)
    RETURN caller.name AS caller_name, caller.file_path AS caller_file_path, length(path) AS hops
    """

    try:
        with driver.session() as session:
            result = session.run(query, function_name=function_name, file_path=file_path)
            records = list(result)

            if not records:
                logger.info(
                    "blast_radius_function_not_indexed",
                    function_name=function_name,
                    file_path=file_path,
                )
                return []

            # If there is exactly one row and its caller_name is None, it means the
            # function exists but has 0 callers (leaf function).
            if len(records) == 1 and records[0]["caller_name"] is None:
                logger.info(
                    "blast_radius_leaf_function",
                    function_name=function_name,
                    file_path=file_path,
                    callers_count=0
                )
                return []

            # Format callers
            callers = []
            for r in records:
                c_name = r["caller_name"]
                c_file = r["caller_file_path"]
                if c_name and c_file:
                    callers.append(f"{c_name} ({c_file})")

            logger.info(
                "blast_radius_callers_found",
                function_name=function_name,
                file_path=file_path,
                callers_count=len(callers)
            )
            return callers

    except Exception as exc:
        logger.error("neo4j_query_failed_blast_radius_empty", error=str(exc))
        return []


# ── AST changed functions identification ──────────────────────────────────────

def get_changed_functions(source_content: str, language: str, diff_hunks: list[str]) -> list[str]:
    """
    Parse new source content, locate enclosing function names covering the diff hunks,
    and return their qualified names.
    """
    mapped_lang = LANGUAGE_MAP.get(language.lower(), "")
    if not mapped_lang or not source_content:
        return []

    try:
        parser = get_parser(mapped_lang)
        source_bytes = source_content.encode("utf-8")
        tree = parser.parse(source_bytes)
        root_node = tree.root_node

        changed_defs = get_changed_definitions(root_node, diff_hunks)

        # Collect function qualified names
        # Walk parent definitions to check if within class
        func_names = []
        for node in changed_defs:
            if "function" in node.type or "method" in node.type or "arrow" in node.type:
                name = get_node_name(node, source_bytes)
                # Check enclosing class parent
                class_name = None
                parent = node.parent
                while parent is not None:
                    if parent.type in ("class_definition", "class_declaration"):
                        class_name = get_node_name(parent, source_bytes)
                        break
                    parent = parent.parent

                qualified = f"{class_name}.{name}" if class_name else name
                func_names.append(qualified)

        return func_names
    except Exception as exc:
        logger.error("failed_to_get_changed_functions", error=str(exc))
        return []
