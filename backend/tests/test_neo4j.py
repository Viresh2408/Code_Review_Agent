"""
Unit tests for the Neo4j ingestion and blast radius query module.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from app.parser.neo4j_ingest import (
    extract_ast_info,
    resolve_import_path,
    ingest_file_to_neo4j,
    get_blast_radius,
    get_changed_functions,
)


def test_python_ast_extraction():
    """Verify that Python functions, calls, classes, and imports are correctly extracted."""
    source_content = (
        "import os\n"
        "from app.db.session import get_session\n"
        "from .base import Model\n"
        "\n"
        "class User(Model):\n"
        "    def save(self):\n"
        "        db = get_session()\n"
        "        db.commit()\n"
        "\n"
        "def helper_func():\n"
        "    os.getenv('VAR')\n"
    )
    source_bytes = source_content.encode("utf-8")
    info = extract_ast_info(source_bytes, "python")

    # Verify imports
    assert "os" in info["imports"]
    assert "app.db.session" in info["imports"]
    assert ".base" in info["imports"]

    # Verify classes
    assert len(info["classes"]) == 1
    assert info["classes"][0]["name"] == "User"
    assert info["classes"][0]["inherits_from"] == ["Model"]

    # Verify functions
    assert len(info["functions"]) == 2
    func_names = [f["name"] for f in info["functions"]]
    assert "User.save" in func_names
    assert "helper_func" in func_names

    # Verify function calls
    save_func = next(f for f in info["functions"] if f["name"] == "User.save")
    assert "get_session" in save_func["calls"]
    assert "commit" in save_func["calls"]

    helper = next(f for f in info["functions"] if f["name"] == "helper_func")
    assert "getenv" in helper["calls"]


def test_javascript_ast_extraction():
    """Verify that JavaScript classes, arrow functions, and calls are correctly extracted."""
    source_content = (
        "import { auth } from './auth';\n"
        "import logger from '../utils/logger';\n"
        "\n"
        "class AuthService extends BaseService {\n"
        "    async login(user) {\n"
        "        const token = await auth.generate(user);\n"
        "        logger.info('success');\n"
        "        return token;\n"
        "    }\n"
        "}\n"
        "\n"
        "const arrowHelper = (x) => {\n"
        "    doSomething();\n"
        "};\n"
    )
    source_bytes = source_content.encode("utf-8")
    info = extract_ast_info(source_bytes, "javascript")

    # Verify imports
    assert "./auth" in info["imports"]
    assert "../utils/logger" in info["imports"]

    # Verify classes
    assert len(info["classes"]) == 1
    assert info["classes"][0]["name"] == "AuthService"
    assert info["classes"][0]["inherits_from"] == ["BaseService"]

    # Verify functions
    assert len(info["functions"]) == 2
    func_names = [f["name"] for f in info["functions"]]
    assert "AuthService.login" in func_names
    assert "arrowHelper" in func_names

    # Verify calls
    login_func = next(f for f in info["functions"] if f["name"] == "AuthService.login")
    assert "generate" in login_func["calls"]
    assert "info" in login_func["calls"]

    helper = next(f for f in info["functions"] if f["name"] == "arrowHelper")
    assert "doSomething" in helper["calls"]


def test_get_changed_functions_helper():
    """Verify that only the definitions enclosing the changed lines are identified."""
    source_content = (
        "def func_one():\n"
        "    pass\n"
        "\n"
        "class MyClass:\n"
        "    def method_a(self):\n"
        "        pass\n"
        "\n"
        "    def method_b(self):\n"
        "        pass\n"
    )
    # Assume method_a was modified (lines 5-6)
    diff_hunks = [
        "@@ -4,4 +4,4 @@\n"
        " class MyClass:\n"
        "     def method_a(self):\n"
        "+        print('changed!')\n"
        "         pass"
    ]
    changed = get_changed_functions(source_content, "python", diff_hunks)
    assert changed == ["MyClass.method_a"]


@patch("app.parser.neo4j_ingest.get_neo4j_driver")
def test_neo4j_idempotency(mock_get_driver):
    """
    Verify that executing file ingestion twice deletes old function/class
    nodes before writing the new ones, ensuring idempotency.
    """
    mock_session = MagicMock()
    mock_driver = MagicMock()
    mock_driver.session.return_value.__enter__.return_value = mock_session
    mock_get_driver.return_value = mock_driver

    source_content = (
        "def main():\n"
        "    print('hello')\n"
    )

    # Ingest once
    ingest_file_to_neo4j(
        file_path="main.py",
        language="python",
        source_content=source_content,
        repo_id="test_repo",
        project_root="/dummy/root"
    )

    # Ingest twice (simulation of synchronize event / new commit)
    ingest_file_to_neo4j(
        file_path="main.py",
        language="python",
        source_content=source_content,
        repo_id="test_repo",
        project_root="/dummy/root"
    )

    # Assert that session.execute_write was called twice
    assert mock_session.execute_write.call_count == 2

    # Verify that the first Cypher query in the transaction deletes old nodes for idempotency
    # Let's inspect the query passed to tx.run inside execute_write
    # We call mock_session.execute_write, which executes _ingest_tx(tx, ...)
    mock_tx = MagicMock()
    from app.parser.neo4j_ingest import _ingest_tx
    _ingest_tx(
        mock_tx,
        file_path="main.py",
        language="python",
        repo_id="test_repo",
        info={"classes": [], "functions": [], "imports": []},
        project_root="/dummy/root"
    )

    # Ensure detaching and deleting old nodes query is executed first
    first_call_args = mock_tx.run.call_args_list[0]
    query_str = first_call_args[0][0]
    assert "DETACH DELETE" in query_str
    assert "File {path: $file_path, repo_id: $repo_id}" in query_str


@patch("app.parser.neo4j_ingest.get_neo4j_driver")
def test_blast_radius_logging_and_results(mock_get_driver):
    """Verify that get_blast_radius returns correct results and triggers correct logs."""
    mock_session = MagicMock()
    mock_driver = MagicMock()
    mock_driver.session.return_value.__enter__.return_value = mock_session
    mock_get_driver.return_value = mock_driver

    # Mock Neo4j session.run returns for three scenarios:
    # 1. Not indexed: empty list returned
    mock_session.run.return_value = []
    with patch("app.parser.neo4j_ingest.logger") as mock_logger:
        res = get_blast_radius("missing_func", "main.py")
        assert res == []
        mock_logger.info.assert_called_with(
            "blast_radius_function_not_indexed",
            function_name="missing_func",
            file_path="main.py"
        )

    # 2. Leaf function: single row with None caller_name
    mock_record_leaf = MagicMock()
    mock_record_leaf.__getitem__.side_effect = lambda key: None if key == "caller_name" else "main.py"
    mock_session.run.return_value = [mock_record_leaf]
    with patch("app.parser.neo4j_ingest.logger") as mock_logger:
        res = get_blast_radius("leaf_func", "main.py")
        assert res == []
        mock_logger.info.assert_called_with(
            "blast_radius_leaf_function",
            function_name="leaf_func",
            file_path="main.py",
            callers_count=0
        )

    # 3. Has callers
    mock_record_caller = MagicMock()
    mock_record_caller.__getitem__.side_effect = lambda key: "caller_func" if key == "caller_name" else "caller.py"
    mock_session.run.return_value = [mock_record_caller]
    with patch("app.parser.neo4j_ingest.logger") as mock_logger:
        res = get_blast_radius("target_func", "main.py")
        assert res == ["caller_func (caller.py)"]
        mock_logger.info.assert_called_with(
            "blast_radius_callers_found",
            function_name="target_func",
            file_path="main.py",
            callers_count=1
        )
