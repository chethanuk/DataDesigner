# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the pure helpers in `.agents/tools/structural_impact.py`."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import structural_impact
from structural_impact import (
    _collect_source_files,
    _dedup,
    _get_package,
    _unknown_package_dirs,
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
    assert _get_package(filepath) == expected


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
    assert _dedup(items) == expected


def test_dedup_honours_a_narrower_key_tuple() -> None:
    items = [_edge("A", "T", "imports"), _edge("A", "T", "inherits")]
    assert _dedup(items, keys=("from_label", "to_label")) == [items[0]]


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

    assert _collect_source_files(repo_root) == expected


def test_collect_source_files_tolerates_a_missing_package_directory(repo_root: Path) -> None:
    expected = sorted(
        [
            _write(repo_root, f"{ENGINE_SRC}/a.py"),
            _write(repo_root, f"{INTERFACE_SRC}/c.py"),
        ]
    )
    assert not (repo_root / "packages" / "data-designer-config").exists()

    assert _collect_source_files(repo_root) == expected


def test_collect_source_files_returns_empty_for_an_empty_root(repo_root: Path) -> None:
    assert _collect_source_files(repo_root) == []


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
        # The check is positional - `parts[1]` under `packages/` is taken as the package name
        # whether or not it is a directory. Pinned as the current boundary, not endorsed.
        pytest.param(["packages/loose.py"], ["loose.py"], id="loose-file-under-packages-named"),
    ],
)
def test_unknown_package_dirs_reports_only_packages_outside_the_known_set(
    repo_root: Path, relpaths: list[str], expected: list[str]
) -> None:
    paths = [_write(repo_root, rel) for rel in relpaths]

    assert _unknown_package_dirs(paths, repo_root) == expected


def test_unknown_package_dirs_skips_paths_outside_the_repo_root(repo_root: Path, tmp_path: Path) -> None:
    outside = tmp_path.resolve().parent / "elsewhere" / "packages" / "ghost" / "g.py"

    assert _unknown_package_dirs([outside], repo_root) == []


def test_no_function_reads_a_module_level_repo_root() -> None:
    """Guard for every `_REPO_ROOT` reader, including `_full_mode`'s `graphify-out` path.

    Python resolves a global name inside a function body at call time, so a reader left behind
    after the global is deleted raises `NameError` only when that path runs - and `--full` is
    exercised by a nightly agentic recipe, not by this suite. `co_names` is an exact match, so
    `_REPO_ROOT_DEFAULT` does not count.
    """
    assert not hasattr(structural_impact, "_REPO_ROOT")

    offenders = sorted(
        fn.__name__
        for fn in vars(structural_impact).values()
        if inspect.isfunction(fn) and "_REPO_ROOT" in fn.__code__.co_names
    )
    assert offenders == []
