# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[4] / "fern" / "scripts" / "fern-published-branch.py"


def load_script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fern_published_branch", SCRIPT_PATH)
    assert spec is not None
    loader = spec.loader
    assert loader is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def patch_args(source_root: Path, published_root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        source_root=source_root,
        published_root=published_root,
        metadata_source_repository=None,
        metadata_source_ref=None,
        metadata_source_sha=None,
        metadata_release_tag=None,
        metadata_published_branch=None,
    )


def test_patch_devnotes_syncs_root_config_and_preserves_published_versions(tmp_path: Path) -> None:
    module = load_script_module()
    source_root = tmp_path / "source"
    published_root = tmp_path / "published"

    write_text(
        source_root / "fern" / "docs.yml",
        """instances:
- url: datadesigner.docs.buildwithfern.com/nemo/datadesigner
title: Source Fern Docs
global-theme: nvidia
navbar-links:
- type: github
  value: https://github.com/NVIDIA-NeMo/DataDesigner
versions:
- display-name: "Latest"
  path: versions/latest.yml
  slug: latest
redirects:
  - source: "/nemo/datadesigner/getting-started"
    destination: "/nemo/datadesigner/getting-started/welcome"
""",
    )
    write_text(source_root / "fern" / "fern.config.json", '{"organization": "nvidia", "version": "5.41.1"}\n')
    write_text(source_root / "fern" / "assets" / "current-devnote-asset.png", "new asset")
    write_text(source_root / "fern" / "components" / "Figure.tsx", "export const Figure = () => null;\n")
    write_text(source_root / "fern" / "components" / "ImageExample.tsx", "export type ImageExample = string;\n")
    write_text(
        source_root / "fern" / "components" / "ImageExampleGallery.tsx",
        'import type { ImageExample } from "./ImageExample";\nexport type ImageGallery = ImageExample[];\n',
    )
    write_text(
        source_root / "fern" / "versions" / "latest.yml",
        """navigation:
  - section: Recipes
    contents:
      - page: New Recipe
        path: ./latest/pages/recipes/new-recipe.mdx
  - section: Dev Notes
    contents:
      - page: New Note
        path: ./latest/pages/devnotes/posts/new-note.mdx
  - section: Concepts
    contents: []
""",
    )
    write_text(source_root / "fern" / "versions" / "latest" / "pages" / "recipes" / "new-recipe.mdx", "# New")
    write_text(source_root / "fern" / "versions" / "latest" / "pages" / "devnotes" / "posts" / "new-note.mdx", "# New")

    write_text(
        published_root / "fern" / "docs.yml",
        """instances:
- url: datadesigner.docs.buildwithfern.com/nemo/datadesigner
title: Published Fern Docs
footer: ./components/OldFooter.tsx
layout:
  searchbar-placement: header
versions:
- display-name: latest
  path: versions/latest.yml
  slug: latest
- display-name: "v0.6.0"
  path: versions/v0.6.0.yml
  slug: v0.6.0
redirects:
  - source: "/nemo/datadesigner/getting-started"
    destination: "/nemo/datadesigner/getting-started/welcome"
""",
    )
    write_text(published_root / "fern" / "fern.config.json", '{"organization": "nvidia", "version": "4.106.0"}\n')
    write_text(
        published_root / "fern" / "publish-metadata.json",
        '{"action": "release-snapshot", "release_tag": "v0.6.0"}\n',
    )
    write_text(
        published_root / "fern" / "notebook-snapshot.json",
        '{"release_tag": "v0.6.0", "asset": "notebooks.tar.gz"}\n',
    )
    write_text(published_root / "fern" / "assets" / "published-only-asset.png", "old asset")
    write_text(
        published_root / "fern" / "versions" / "latest.yml",
        """navigation:
  - section: Recipes
    contents:
      - page: Released Recipe
        path: ./v0.6.0/pages/recipes/released-recipe.mdx
  - section: Dev Notes
    contents:
      - page: Old Note
        path: ./latest/pages/devnotes/posts/old-note.mdx
  - section: Concepts
    contents:
      - page: Released Concept
        path: ./v0.6.0/pages/concepts/released-concept.mdx
""",
    )
    write_text(
        published_root / "fern" / "versions" / "v0.6.0.yml",
        """navigation:
  - section: Recipes
    contents:
      - page: Released Recipe
        path: ./v0.6.0/pages/recipes/released-recipe.mdx
  - section: Dev Notes
    contents:
      - page: Released Note
        path: ./v0.6.0/pages/devnotes/posts/released-note.mdx
""",
    )
    write_text(
        published_root / "fern" / "versions" / "v0.6.0" / "pages" / "devnotes" / "posts" / "released-note.mdx",
        "# Released",
    )
    write_text(published_root / "fern" / "versions" / "latest" / "pages" / "recipes" / "old-recipe.mdx", "# Old")

    assert module.patch_devnotes(patch_args(source_root, published_root)) == 0
    assert module.patch_devnotes(patch_args(source_root, published_root)) == 0

    published_docs = (published_root / "fern" / "docs.yml").read_text()
    assert "title: Source Fern Docs" in published_docs
    assert "global-theme: nvidia" in published_docs
    assert "navbar-links:" in published_docs
    assert "title: Published Fern Docs" not in published_docs
    assert "footer: ./components/OldFooter.tsx" not in published_docs
    assert "searchbar-placement: header" not in published_docs
    assert '- display-name: "Latest"' in published_docs
    assert 'display-name: "v0.6.0"' in published_docs
    assert (published_root / "fern" / "fern.config.json").read_text() == (
        '{"organization": "nvidia", "version": "5.41.1"}\n'
    )
    assert (published_root / "fern" / "assets" / "current-devnote-asset.png").read_text() == "new asset"
    assert (published_root / "fern" / "components" / "Figure.tsx").read_text() == (
        "export const Figure = () => null;\n"
    )
    assert (published_root / "fern" / "components" / "ImageExample.tsx").read_text() == (
        "export type ImageExample = string;\n"
    )
    assert (published_root / "fern" / "components" / "ImageExampleGallery.tsx").read_text() == (
        'import type { ImageExample } from "./ImageExample";\nexport type ImageGallery = ImageExample[];\n'
    )
    assert not (published_root / "fern" / "assets" / "published-only-asset.png").exists()
    assert (published_root / "fern" / "publish-metadata.json").read_text() == (
        '{"action": "release-snapshot", "release_tag": "v0.6.0"}\n'
    )
    assert (published_root / "fern" / "notebook-snapshot.json").read_text() == (
        '{"release_tag": "v0.6.0", "asset": "notebooks.tar.gz"}\n'
    )
    assert (published_root / "fern" / "versions" / "latest" / "pages" / "devnotes" / "posts" / "new-note.mdx").exists()
    published_nav = (published_root / "fern" / "versions" / "latest.yml").read_text()
    assert published_nav.count("section: Recipes") == 1
    assert "path: ./latest/pages/recipes/new-recipe.mdx" in published_nav
    assert "path: ./v0.6.0/pages/recipes/released-recipe.mdx" not in published_nav
    assert "path: ./v0.6.0/pages/concepts/released-concept.mdx" in published_nav
    assert (published_root / "fern" / "versions" / "latest" / "pages" / "recipes" / "new-recipe.mdx").exists()
    assert not (published_root / "fern" / "versions" / "latest" / "pages" / "recipes" / "old-recipe.mdx").exists()
    assert (
        "./v0.6.0/pages/devnotes/posts/released-note.mdx"
        in (published_root / "fern" / "versions" / "v0.6.0.yml").read_text()
    )
    assert (
        published_root / "fern" / "versions" / "v0.6.0" / "pages" / "devnotes" / "posts" / "released-note.mdx"
    ).read_text() == "# Released"


REPO_BLOB_MAIN = "https://github.com/NVIDIA-NeMo/DataDesigner/blob/main/"
COLAB_BLOB_MAIN = "https://colab.research.google.com/github/NVIDIA-NeMo/DataDesigner/blob/main/"


def seed_minimal_publish_trees(source_root: Path, published_root: Path) -> None:
    docs_yml = "versions:\n- display-name: latest\n  path: versions/latest.yml\n  slug: latest\n"
    latest_nav = """navigation:
  - section: Recipes
    contents: []
  - section: Dev Notes
    contents: []
"""
    for root in (source_root, published_root):
        write_text(root / "fern" / "docs.yml", docs_yml)
        write_text(root / "fern" / "fern.config.json", '{"organization": "nvidia", "version": "5.41.1"}\n')
        write_text(root / "fern" / "versions" / "latest.yml", latest_nav)


@pytest.mark.parametrize(
    ("published_link", "expected_link"),
    [
        pytest.param(
            COLAB_BLOB_MAIN + "docs/" + "colab_notebooks/1-the-basics.ipynb",
            COLAB_BLOB_MAIN + "fern/colab_notebooks/1-the-basics.ipynb",
            id="colab-badge",
        ),
        pytest.param(
            REPO_BLOB_MAIN + "docs/" + "assets/recipes/qa_and_chat/multi_turn_chat.py",
            REPO_BLOB_MAIN + "fern/assets/recipes/qa_and_chat/multi_turn_chat.py",
            id="recipe-download",
        ),
        pytest.param(
            REPO_BLOB_MAIN + "docs/" + "notebook_source/1-the-basics.py",
            REPO_BLOB_MAIN + "fern/notebook_source/1-the-basics.py",
            id="notebook-source",
        ),
        pytest.param(
            "https://github.com/NVIDIA-NeMo/DataDesigner/blob/v0.9.2/docs/" + "assets/recipes/x.py",
            "https://github.com/NVIDIA-NeMo/DataDesigner/blob/v0.9.2/docs/" + "assets/recipes/x.py",
            id="tag-pinned-link-unchanged",
        ),
        pytest.param(
            REPO_BLOB_MAIN + "docs/concepts/columns.md",
            REPO_BLOB_MAIN + "docs/concepts/columns.md",
            id="unmoved-docs-path-unchanged",
        ),
    ],
)
def test_publishing_retargets_links_to_moved_support_files(
    tmp_path: Path, published_link: str, expected_link: str
) -> None:
    module = load_script_module()
    source_root = tmp_path / "source"
    published_root = tmp_path / "published"
    seed_minimal_publish_trees(source_root, published_root)
    frozen_page = published_root / "fern" / "versions" / "v0.9.2" / "pages" / "notebooks" / "1-the-basics.mdx"
    latest_page = published_root / "fern" / "versions" / "latest" / "pages" / "concepts" / "columns.mdx"
    for page in (frozen_page, latest_page):
        write_text(page, f"[Open]({published_link})\n")

    assert module.patch_devnotes(patch_args(source_root, published_root)) == 0
    assert module.patch_devnotes(patch_args(source_root, published_root)) == 0

    for page in (frozen_page, latest_page):
        assert page.read_text() == f"[Open]({expected_link})\n"
