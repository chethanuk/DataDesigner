# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[4] / ".github" / "workflows"


def test_notebook_cache_is_scoped_to_execution_profile() -> None:
    workflow = (WORKFLOWS_DIR / "build-notebooks.yml").read_text()

    assert (
        "DATA_DESIGNER_FLUX_2_PRO_CREATE_NUM_RECORDS: ${{ github.event_name == 'schedule' && '2' || '5' }}" in workflow
    )
    assert "NOTEBOOK_EXECUTION_PROFILE: ${{ github.event_name == 'schedule'" in workflow
    assert "NOTEBOOK_CACHE_CONTEXT=${NOTEBOOK_EXECUTION_PROFILE}:" in workflow
    assert workflow.count("'docs/scripts/build_notebooks_cached.sh'") == 2
    assert "key: notebooks-${{ env.NOTEBOOK_EXECUTION_PROFILE }}-" in workflow
    assert "notebooks-${{ env.NOTEBOOK_EXECUTION_PROFILE }}-\n" in workflow
    assert "gh run list --workflow build-fern-docs.yml --status success" in workflow


def test_notebook_cache_can_be_disabled() -> None:
    workflow = (WORKFLOWS_DIR / "build-notebooks.yml").read_text()

    assert "NOTEBOOK_CACHE_ENABLED: ${{ inputs.use_cache && '1' || '0' }}" in workflow
    assert 'if [ "$NOTEBOOK_CACHE_ENABLED" != "1" ]; then' in workflow
    assert "rm -rf .notebook-cache" in workflow


def test_fern_publish_excludes_cancelled_notebook_builds() -> None:
    workflow = (WORKFLOWS_DIR / "build-fern-docs.yml").read_text()

    assert "(needs.build-notebooks.result == 'success' || needs.build-notebooks.result == 'failure')" in workflow
    assert "if: needs.build-notebooks.result == 'failure'" in workflow


def test_fern_release_resolution_sets_repository() -> None:
    workflow = (WORKFLOWS_DIR / "build-fern-docs.yml").read_text()

    assert 'gh release list --repo "$GITHUB_REPOSITORY"' in workflow


def test_fern_publish_persists_and_restores_notebook_snapshots() -> None:
    workflow = (WORKFLOWS_DIR / "build-fern-docs.yml").read_text()

    assert "Publish source notebook snapshot" in workflow
    assert 'source-fallback "$archive"' in workflow
    assert "Restore prepared notebook snapshot" in workflow
    assert "Run the Build Fern docs workflow successfully once" in workflow
    assert 'gh release download "$release_tag"' in workflow
    assert "Publish executed notebook snapshot" in workflow
    assert 'executed "$archive"' in workflow
    assert "git add fern/notebook-snapshot.json" in workflow


def test_devnotes_publish_does_not_reuse_notebook_artifacts() -> None:
    workflow = (WORKFLOWS_DIR / "publish-fern-devnotes.yml").read_text()

    assert "Reuse notebooks from last successful docs build" not in workflow
    assert "gh run download" not in workflow
    assert "Require published notebook snapshot" in workflow
    assert "Run the Build Fern docs workflow successfully once" in workflow
    assert "Restore published notebook snapshot" in workflow
    assert 'gh release download "$release_tag"' in workflow
    assert "run: make -f ../workflow/Makefile check-fern-published-docs" in workflow
    assert "run: make check-fern-docs\n" not in workflow


def _load_workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text())


def test_docs_preview_pr_workflow_holds_no_deploy_credentials() -> None:
    text = (WORKFLOWS_DIR / "docs-preview.yml").read_text()
    workflow = yaml.safe_load(text)

    assert "secrets." not in text
    for job in workflow["jobs"].values():
        assert set(job["permissions"].values()) <= {"read"}


def test_docs_preview_deploy_publishes_the_preview_build_artifact() -> None:
    build = _load_workflow("docs-preview.yml")
    deploy_text = (WORKFLOWS_DIR / "docs-preview-deploy.yml").read_text()
    deploy = yaml.safe_load(deploy_text)

    # PyYAML (YAML 1.1) reads the `on:` key as True.
    assert deploy[True]["workflow_run"] == {"workflows": [build["name"]], "types": ["completed"]}
    job_if = deploy["jobs"]["deploy"]["if"]
    assert "github.event.workflow_run.event == 'pull_request'" in job_if
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in job_if
    upload = next(
        s for s in build["jobs"]["build"]["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert f"--name {upload['with']['name']} " in deploy_text
    assert "FERN_TOKEN: ${{ secrets.DOCS_FERN_TOKEN }}" in deploy_text


def test_docs_preview_deploy_does_not_trust_pr_code() -> None:
    deploy_text = (WORKFLOWS_DIR / "docs-preview-deploy.yml").read_text()
    steps = yaml.safe_load(deploy_text)["jobs"]["deploy"]["steps"]

    assert not any(s.get("uses", "").startswith("actions/checkout@") for s in steps)
    # PR number from the artifact must match the event; Fern must not re-launch at the artifact's version.
    assert "github.event.workflow_run.pull_requests.*.number" in deploy_text
    assert 'FERN_NO_VERSION_REDIRECTION: "true"' in deploy_text


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.skipif(shutil.which("yq") is None, reason="the guard parses YAML with yq (preinstalled on GitHub runners)")
@pytest.mark.parametrize(
    ("files", "symlink", "allowed"),
    [
        pytest.param(
            {"docs.yml": "navigation:\n  - page: Home\n    path: ./pages/index.mdx\n"}, False, True, id="in-tree"
        ),
        pytest.param(
            {"versions/v.yml": "navigation:\n  - path: ../pages/index.mdx\n"}, False, False, id="dotdot-from-root"
        ),
        pytest.param({"docs.yml": "navigation:\n  - path: ../../outside.mdx\n"}, False, False, id="dotdot"),
        pytest.param({"docs.yml": "navigation:\n  - path: /proc/self/environ\n"}, False, False, id="absolute"),
        pytest.param({"docs.yml": "x: {path: '/etc/passwd'}\n"}, False, False, id="flow-style"),
        pytest.param({"docs.yml": "navigation:\n  - path: ./pages/index.mdx\n"}, True, False, id="symlink"),
    ],
)
def test_docs_preview_deploy_rejects_paths_outside_the_artifact(
    tmp_path: Path, files: dict[str, str], symlink: bool, allowed: bool
) -> None:
    steps = _load_workflow("docs-preview-deploy.yml")["jobs"]["deploy"]["steps"]
    guard = next(s for s in steps if s.get("id") == "paths")
    fern = tmp_path / "fern"
    _write(fern / "pages" / "index.mdx", "# Home\n")
    for name, text in files.items():
        _write(fern / name, text)
    if symlink:
        (fern / "pages" / "env.mdx").symlink_to("/proc/self/environ")
    script = tmp_path / "guard.py"
    script.write_text(guard["run"])

    result = subprocess.run([sys.executable, str(script)], cwd=fern, capture_output=True, text=True)

    assert (result.returncode == 0) == allowed, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("yq") is None, reason="the guard parses YAML with yq (preinstalled on GitHub runners)")
def test_docs_preview_deploy_path_guard_accepts_the_real_docs_tree() -> None:
    steps = _load_workflow("docs-preview-deploy.yml")["jobs"]["deploy"]["steps"]
    guard = next(s for s in steps if s.get("id") == "paths")

    result = subprocess.run(
        [sys.executable, "-c", guard["run"]], cwd=WORKFLOWS_DIR.parents[1] / "fern", capture_output=True, text=True
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_docs_preview_deploy_keeps_github_token_away_from_fern() -> None:
    steps = _load_workflow("docs-preview-deploy.yml")["jobs"]["deploy"]["steps"]
    deploy = next(s for s in steps if s.get("id") == "fern-preview")

    assert "generate --docs --preview" in deploy["run"]
    assert not any("github.token" in str(v) for v in deploy["env"].values())
    assert "GH_TOKEN" not in deploy["env"]
