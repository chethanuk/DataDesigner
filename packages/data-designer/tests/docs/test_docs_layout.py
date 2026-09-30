# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
# Links that GitHub and Colab resolve against this repository's main branch.
MAIN_BRANCH_LINK_RE = re.compile(r"NVIDIA-NeMo/DataDesigner/(?:(?:blob|tree)/)?main/([^\s\"'<>()`\\{}\[\]#?]+)")


def tracked_files(*pathspecs: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--", *pathspecs], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.splitlines()


def test_docs_support_files_have_a_single_root() -> None:
    assert tracked_files("docs") == [], "documentation sources and support files belong under fern/"
    assert sorted((REPO_ROOT / "fern" / "notebook_source").glob("*.py"))


def test_repository_links_on_main_resolve() -> None:
    missing: set[str] = set()
    for name in tracked_files():
        path = REPO_ROOT / name
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in MAIN_BRANCH_LINK_RE.finditer(text):
            target = match.group(1).rstrip(".,;:")
            if not (REPO_ROOT / target).exists():
                missing.add(f"{name}: {target}")

    assert not missing, "links to missing files on main:\n" + "\n".join(sorted(missing))


# Built from parts so this file does not itself contain a broken main-branch link.
@pytest.mark.parametrize(
    "host,ref",
    [("github.com", "blob/main"), ("github.com", "tree/main"), ("raw.githubusercontent.com", "main")],
)
def test_main_branch_link_forms_are_checked(host: str, ref: str) -> None:
    url = f"https://{host}/NVIDIA-NeMo/DataDesigner/{ref}/docs/assets/nope.png"
    match = MAIN_BRANCH_LINK_RE.search(f'<img src="{url}">')
    assert match is not None and match.group(1) == "docs/assets/nope.png"
    assert not (REPO_ROOT / match.group(1)).exists()


def test_run_recipes_target_skips_image_generation_recipes(tmp_path: Path) -> None:
    # Stub uv so the real Makefile loop runs without executing any recipe.
    log = tmp_path / "ran.txt"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(f'#!/bin/sh\nfor a; do case "$a" in *.py) echo "$a" >> "{log}";; esac; done\n')
    fake_uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}
    subprocess.run(["make", "-s", "test-run-recipes"], cwd=REPO_ROOT, env=env, check=True, capture_output=True)

    ran = sorted(Path(line).relative_to(REPO_ROOT).as_posix() for line in log.read_text().splitlines())
    recipes = sorted(str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "fern/assets/recipes").glob("*/*.py"))
    assert ran == [r for r in recipes if not r.startswith("fern/assets/recipes/image_generation/")]
