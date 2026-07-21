import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from harness.executor import (
    BlockApplyError,
    PathEscapeError,
    apply_blocks,
    apply_change,
    safe_full_path,
)


def test_safe_full_path_allows_paths_inside_root(tmp_path):
    root = str(tmp_path)
    full = safe_full_path(root, "sub/file.py")
    assert full.startswith(os.path.realpath(root))


def test_safe_full_path_blocks_parent_escape(tmp_path):
    root = str(tmp_path / "work")
    os.makedirs(root, exist_ok=True)
    with pytest.raises(PathEscapeError):
        safe_full_path(root, "../../etc/passwd")


def test_safe_full_path_blocks_absolute_escape(tmp_path):
    root = str(tmp_path / "work")
    os.makedirs(root, exist_ok=True)
    with pytest.raises(PathEscapeError):
        safe_full_path(root, "/etc/passwd")


def test_apply_blocks_replaces_matching_search():
    current = "def foo():\n    return 1\n"
    raw = "<<<<<<< SEARCH\n    return 1\n=======\n    return 2\n>>>>>>> REPLACE"
    result = apply_blocks(current, raw)
    assert "return 2" in result
    assert "return 1" not in result


def test_apply_blocks_raises_when_search_not_found():
    current = "def foo():\n    return 1\n"
    raw = "<<<<<<< SEARCH\n    return 999\n=======\n    return 2\n>>>>>>> REPLACE"
    with pytest.raises(BlockApplyError):
        apply_blocks(current, raw)


def test_apply_blocks_raises_when_search_ambiguous():
    current = "x = 1\nx = 1\n"
    raw = "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE"
    with pytest.raises(BlockApplyError):
        apply_blocks(current, raw)


def test_apply_blocks_falls_back_to_full_rewrite_when_no_blocks():
    raw = "print('hello')\n"
    result = apply_blocks("old content", raw)
    assert result == raw.strip()


def test_apply_change_backs_up_existing_file(tmp_path):
    root = str(tmp_path)
    target = "notes.txt"
    full = safe_full_path(root, target)
    with open(full, "w", encoding="utf-8") as f:
        f.write("original")

    backup_path = apply_change(root, target, "updated", backup_dir=".agent_backup")

    assert backup_path is not None
    assert os.path.exists(backup_path)
    with open(backup_path, "r", encoding="utf-8") as f:
        assert f.read() == "original"
    with open(full, "r", encoding="utf-8") as f:
        assert f.read() == "updated"


def test_apply_change_no_backup_for_new_file(tmp_path):
    root = str(tmp_path)
    backup_path = apply_change(root, "new_file.txt", "content", backup_dir=".agent_backup")
    assert backup_path is None
