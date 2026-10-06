# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for `.agents/tools/structural_impact.py`: its public helpers and its CLI entry point."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import structural_impact
from structural_impact import (
    collect_source_files,
    dedup,
    get_package,
    unknown_package_dirs,
)

ENGINE_SRC = "packages/data-designer-engine/src/data_designer/engine"
CONFIG_SRC = "packages/data-designer-config/src/data_designer/config"
INTERFACE_SRC = "packages/data-designer/src/data_designer"


@pytest.mark.parametrize(
    ("filepath", "expected"),
    [
        (f"{ENGINE_SRC}/dataset_builders/builder.py", "engine"),
        (f"{CONFIG_SRC}/config_builder.py", "config"),
        (f"{INTERFACE_SRC}/interface.py", "interface"),
        # Precedence: both package paths above also contain the literal "data-designer", so an
        # implementation that tested for the interface first would call these "interface".
        (f"/abs/checkout/{ENGINE_SRC}/models/llm.py", "engine"),
        (f"/abs/checkout/{CONFIG_SRC}/columns/sampler.py", "config"),
        # "data-designer" only counts when the very next path segment is "src".
        ("packages/data-designer/tests/test_interface.py", ""),
        ("vendor/data-designer/docs/index.py", ""),
        ("scripts/update_license_headers.py", ""),
        ("", ""),
    ],
)
def test_get_package_classifies_a_path_by_its_owning_package(filepath: str, expected: str) -> None:
    assert get_package(filepath) == expected


def _edge(from_label: str, to_label: str = "Target", relation: str = "imports") -> dict[str, str]:
    return {"from_label": from_label, "to_label": to_label, "relation": relation}


# Two labels agreeing for well over 30 characters. A key built from truncated labels collapses
# them; the full-tuple key must not.
_LONG_A = "DataDesignerColumnConfigurationValidatorAlpha"
_LONG_B = "DataDesignerColumnConfigurationValidatorBeta"


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        pytest.param([], [], id="empty"),
        pytest.param(
            [_edge(_LONG_A), _edge(_LONG_B)],
            [_edge(_LONG_A), _edge(_LONG_B)],
            id="long-shared-prefix-survives",
        ),
        pytest.param([_edge("A"), _edge("A")], [_edge("A")], id="exact-duplicate-collapses"),
        pytest.param(
            [_edge("B"), _edge("A"), _edge("B")],
            [_edge("B"), _edge("A")],
            id="first-occurrence-wins-order-preserved",
        ),
        pytest.param(
            [_edge("A", "T", "imports"), _edge("A", "T", "inherits")],
            [_edge("A", "T", "imports"), _edge("A", "T", "inherits")],
            id="relation-is-part-of-the-key",
        ),
    ],
)
def test_dedup_keeps_entries_that_differ_anywhere_in_the_key(items: list[dict], expected: list[dict]) -> None:
    assert dedup(items) == expected


def test_dedup_honours_a_narrower_key_tuple() -> None:
    items = [_edge("A", "T", "imports"), _edge("A", "T", "inherits")]
    assert dedup(items, keys=("from_label", "to_label")) == [items[0]]


def _write(root: Path, relpath: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")
    return path


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A repo root that is already resolved, so `Path.resolve()` inside the helpers is a no-op."""
    return tmp_path.resolve()


def test_collect_source_files_finds_every_package_and_skips_hidden_and_non_python(repo_root: Path) -> None:
    expected = sorted(
        [
            _write(repo_root, f"{ENGINE_SRC}/a.py"),
            _write(repo_root, f"{CONFIG_SRC}/b.py"),
            _write(repo_root, f"{INTERFACE_SRC}/c.py"),
        ]
    )
    _write(repo_root, f"{ENGINE_SRC}/.hidden/skipped.py")
    (repo_root / ENGINE_SRC / "notes.txt").write_text("not python\n", encoding="utf-8")

    assert collect_source_files(repo_root) == expected


def test_collect_source_files_tolerates_a_missing_package_directory(repo_root: Path) -> None:
    expected = sorted(
        [
            _write(repo_root, f"{ENGINE_SRC}/a.py"),
            _write(repo_root, f"{INTERFACE_SRC}/c.py"),
        ]
    )
    assert not (repo_root / "packages" / "data-designer-config").exists()

    assert collect_source_files(repo_root) == expected


def test_collect_source_files_returns_empty_for_an_empty_root(repo_root: Path) -> None:
    assert collect_source_files(repo_root) == []


@pytest.mark.parametrize(
    ("relpaths", "expected"),
    [
        pytest.param([f"{ENGINE_SRC}/a.py", f"{CONFIG_SRC}/b.py"], [], id="known-packages-only"),
        pytest.param(["packages/data-designer-plugins/src/p.py"], ["data-designer-plugins"], id="one-unknown"),
        pytest.param(
            ["packages/data-designer-plugins/src/p.py", "packages/data-designer-plugins/src/q.py"],
            ["data-designer-plugins"],
            id="same-unknown-reported-once",
        ),
        pytest.param(
            ["packages/zeta/src/z.py", "packages/alpha/src/a.py"],
            ["alpha", "zeta"],
            id="multiple-unknowns-sorted",
        ),
        pytest.param(["scripts/helper.py"], [], id="outside-packages-ignored"),
        # A file directly under packages/ is not a package directory.
        pytest.param(["packages/loose.py"], [], id="loose-file-under-packages-ignored"),
    ],
)
def test_unknown_package_dirs_reports_only_packages_outside_the_known_set(
    repo_root: Path, relpaths: list[str], expected: list[str]
) -> None:
    paths = [_write(repo_root, rel) for rel in relpaths]

    assert unknown_package_dirs(paths, repo_root) == expected


def test_unknown_package_dirs_skips_paths_outside_the_repo_root(repo_root: Path, tmp_path: Path) -> None:
    outside = tmp_path.resolve().parent / "elsewhere" / "packages" / "ghost" / "g.py"

    assert unknown_package_dirs([outside], repo_root) == []


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["structural_impact.py", *argv])
    structural_impact.main()


def test_full_mode_writes_graph_under_the_given_repo_root(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(repo_root, f"{CONFIG_SRC}/b.py")
    report = repo_root / "report.md"

    _run_cli(monkeypatch, "--full", "--repo-root", str(repo_root), "--output", str(report))

    assert report.read_text(encoding="utf-8").startswith("### Structural Analysis")
    assert (repo_root / "graphify-out" / "graph.json").is_file()
    assert (repo_root / "graphify-out" / "baselines.json").is_file()


_BIG_CLASS = "class Big:\n" + "".join(f"    def m{i}(self) -> None: ...\n" for i in range(6))


def test_changed_files_resolve_against_the_given_repo_root(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo_root / CONFIG_SRC).mkdir(parents=True)
    (repo_root / CONFIG_SRC / "big.py").write_text(_BIG_CLASS, encoding="utf-8")
    _write(repo_root, "packages/data-designer-plugins/src/p.py")
    report = repo_root / "report.md"

    _run_cli(
        monkeypatch,
        "--changed-files",
        f"{CONFIG_SRC}/big.py",
        "packages/data-designer-plugins/src/p.py",
        "--repo-root",
        str(repo_root),
        "--output",
        str(report),
    )

    text = report.read_text(encoding="utf-8")
    # Relative paths were found under --repo-root, so nothing is reported as deleted.
    assert "deleted" not in text
    assert "unknown package(s) (data-designer-plugins)" in text
    # Source paths in the report are relative to --repo-root.
    assert f"in `{CONFIG_SRC}/big.py`" in text
