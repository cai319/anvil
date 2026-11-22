"""路径安全：任务/仓库/频道 id 不能变成 `.`/`..` 或逃出 data 子目录。"""
from __future__ import annotations

from pathlib import Path

import pytest

from anvil import config
from anvil.services import knowledge, memory, repo_catalog, repo_knowledge, task_workspace
from anvil.services.workspace_knowledge import channel_knowledge_dir
from anvil.util import safe_component, validate_component_id


def test_safe_component_rejects_dot_segments():
    for bad in (".", "..", "...", "", "\x00", "a/../b"):
        value = safe_component(bad, fallback="item")
        assert value not in (".", "..")
        assert "/" not in value and "\x00" not in value
        assert value


def test_validate_component_id_rejects_traversal():
    for bad in (".", "..", "a/b", "a\\b", "\x00", ""):
        with pytest.raises(ValueError):
            validate_component_id(bad)
    assert validate_component_id("T-1.2") == "T-1.2"


def test_task_workspace_stays_under_root(tmp_path, monkeypatch):
    root = tmp_path / "task-workspaces"
    monkeypatch.setenv("ANVIL_TASK_WORKSPACES_DIR", str(root))
    for bad in (".", "..", "...", "../../escape"):
        segment = knowledge.task_path_segment(bad)
        assert segment not in (".", "..")
        path = task_workspace.path_for(bad)
        path.resolve().relative_to(root.resolve())


def test_repo_paths_stay_under_root(tmp_path, monkeypatch):
    repos = tmp_path / "knowledge-repos"
    monkeypatch.setattr(config, "KNOWLEDGE_REPOS_DIR", repos)
    for bad in (".", "..", "...", "x/../../y"):
        key = repo_catalog.repo_key(bad)
        assert key not in (".", "..")
        repo_knowledge.repo_dir(key).resolve().relative_to(repos.resolve())

    lessons_repos = tmp_path / "repos"
    monkeypatch.setattr(config, "REPOS_DIR", lessons_repos)
    for bad in (".", "..", "..."):
        safe = memory._safe_repo(bad)
        assert safe not in (".", "..")
        memory._lesson_path(bad).resolve().relative_to(lessons_repos.resolve())


def test_channel_knowledge_stays_under_root(tmp_path, monkeypatch):
    channels = tmp_path / "channels"
    monkeypatch.setattr(config, "CHANNELS_DIR", channels)
    for bad in (".", "..", "../../x"):
        path = channel_knowledge_dir(bad)
        path.resolve().relative_to(channels.resolve())
