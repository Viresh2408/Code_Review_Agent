"""
Unit tests for the tree-sitter AST parser (Python, JS, TS).
"""

from __future__ import annotations

import pytest
from app.parser.ast import parse_changed_lines, parse_and_summarize_file


def test_parse_changed_lines():
    diff_hunk = (
        "@@ -10,4 +10,5 @@\n"
        " unchanged context line\n"
        "-removed line\n"
        "+added line 1\n"
        "+added line 2\n"
        " another context line"
    )
    # Post-image line numbers:
    # 10: unchanged context line (starts at 10)
    # -removed line (not in post-image, doesn't increment line number)
    # 11: added line 1 (starts at 11)
    # 12: added line 2 (starts at 12)
    # 13: another context line (starts at 13)
    lines = parse_changed_lines(diff_hunk)
    assert lines == [11, 11, 12]


def test_python_ast_parsing():
    source_content = (
        "class Calculator:\n"
        "    def add(self, a, b):\n"
        "        return a + b\n"
        "\n"
        "    def subtract(self, a, b):\n"
        "        return a - b\n"
    )
    # Assume we changed the subtract body (line 6)
    diff_hunks = [
        "@@ -5,2 +5,2 @@\n"
        "     def subtract(self, a, b):\n"
        "+        return a - b  # modified"
    ]
    res = parse_and_summarize_file("calc.py", "python", source_content, diff_hunks)
    
    assert res.path == "calc.py"
    assert res.language == "python"
    assert "Calculator" not in res.ast_summary  # Calculator class itself wasn't modified directly inside its line definition
    assert "subtract" in res.ast_summary
    assert "def subtract(self, a, b)" in res.ast_summary
    assert "return a - b" in res.ast_summary


def test_javascript_ast_parsing():
    source_content = (
        "class Person {\n"
        "    constructor(name) {\n"
        "        this.name = name;\n"
        "    }\n"
        "    sayHello() {\n"
        "        console.log('Hello ' + this.name);\n"
        "    }\n"
        "}\n"
    )
    # Assume sayHello console.log changed (line 6)
    diff_hunks = [
        "@@ -5,3 +5,3 @@\n"
        "     sayHello() {\n"
        "+        console.log('Hi ' + this.name);\n"
        "     }"
    ]
    res = parse_and_summarize_file("person.js", "javascript", source_content, diff_hunks)
    
    assert res.path == "person.js"
    assert res.language == "javascript"
    assert "sayHello" in res.ast_summary
    assert "sayHello()" in res.ast_summary
    assert "console.log" in res.ast_summary


def test_typescript_ast_parsing():
    source_content = (
        "interface User {\n"
        "    id: number;\n"
        "    name: string;\n"
        "}\n"
        "function greetUser(user: User): string {\n"
        "    return `Hello, ${user.name}`;\n"
        "}\n"
    )
    # Assume greetUser return statement changed (line 6)
    diff_hunks = [
        "@@ -5,3 +5,3 @@\n"
        " function greetUser(user: User): string {\n"
        "+    return `Welcome, ${user.name}`;\n"
        " }"
    ]
    res = parse_and_summarize_file("greet.ts", "typescript", source_content, diff_hunks)
    
    assert res.path == "greet.ts"
    assert res.language == "typescript"
    assert "greetUser" in res.ast_summary
    assert "greetUser(user: User): string" in res.ast_summary


def test_pure_deletion_hunk_mapping():
    source_content = (
        "def helper():\n"
        "    print('line 1')\n"
        "    print('line 2')\n"
        "    return 42\n"
    )
    # Deletion of print('line 2') which was line 3 in old file.
    # In new file, helper() is defined. Line 3 in new file is now return 42.
    diff_hunks = [
        "@@ -2,3 +2,2 @@\n"
        "     print('line 1')\n"
        "-    print('line 2')\n"
        "     return 42"
    ]
    res = parse_and_summarize_file("helper.py", "python", source_content, diff_hunks)
    assert "helper" in res.ast_summary


def test_new_file_ast_parsing():
    source_content = (
        "def new_function():\n"
        "    return 'new'\n"
    )
    # Hunk has only additions
    diff_hunks = [
        "@@ -0,0 +1,2 @@\n"
        "+def new_function():\n"
        "+    return 'new'"
    ]
    res = parse_and_summarize_file("new.py", "python", source_content, diff_hunks)
    assert "new_function" in res.ast_summary


def test_multiple_hunks_ast_parsing():
    source_content = (
        "def first():\n"
        "    pass\n"
        "\n"
        "def second():\n"
        "    pass\n"
    )
    # Two hunks, one touching first, one touching second
    diff_hunks = [
        "@@ -1,2 +1,2 @@\n"
        " def first():\n"
        "+    print('first')\n",
        "@@ -4,2 +4,2 @@\n"
        " def second():\n"
        "+    print('second')\n"
    ]
    res = parse_and_summarize_file("multi.py", "python", source_content, diff_hunks)
    assert "first" in res.ast_summary
    assert "second" in res.ast_summary

