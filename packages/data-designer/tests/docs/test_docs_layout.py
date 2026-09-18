# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
# Links that GitHub and Colab resolve against this repository's main branch.
MAIN_BRANCH_LINK_RE = re.compile(r"NVIDIA-NeMo/DataDesigner/(?:blob|tree)/main/([^\s\"'<>()`\\{}\[\]#?]+)")


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
