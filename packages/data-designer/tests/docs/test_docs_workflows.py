# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import shlex
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


def _deploy_step(key: str, value: str) -> dict:
    steps = _load_workflow("docs-preview-deploy.yml")["jobs"]["deploy"]["steps"]
    return next(s for s in steps if s.get(key) == value)


def test_docs_preview_deploy_publishes_the_preview_build_artifact() -> None:
    build = _load_workflow("docs-preview.yml")
    deploy = _load_workflow("docs-preview-deploy.yml")

    # PyYAML (YAML 1.1) reads the `on:` key as True.
    assert deploy[True]["workflow_run"] == {"workflows": [build["name"]], "types": ["completed"]}
    job_if = deploy["jobs"]["deploy"]["if"]
    assert "github.event.workflow_run.event == 'pull_request'" in job_if
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in job_if
    upload = next(
        s for s in build["jobs"]["build"]["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")
    )
    download = shlex.split(_deploy_step("name", "Download preview artifact")["run"])
    assert download[download.index("--name") + 1] == upload["with"]["name"]
    assert _deploy_step("id", "fern-preview")["env"]["FERN_TOKEN"] == "${{ secrets.DOCS_FERN_TOKEN }}"


def test_docs_preview_deploy_does_not_trust_pr_code() -> None:
    steps = _load_workflow("docs-preview-deploy.yml")["jobs"]["deploy"]["steps"]

    assert not any(s.get("uses", "").startswith("actions/checkout@") for s in steps)
    # Fern must not re-launch itself at the version in the artifact's fern.config.json.
    assert str(_deploy_step("id", "fern-preview")["env"]["FERN_NO_VERSION_REDIRECTION"]).lower() == "true"


def test_docs_preview_deploy_skipped_run_cannot_cancel_a_deploy() -> None:
    deploy = _load_workflow("docs-preview-deploy.yml")

    # Workflow-level concurrency would let a run whose deploy job is skipped cancel an in-progress deploy.
    assert "concurrency" not in deploy
    assert deploy["jobs"]["deploy"]["concurrency"]["cancel-in-progress"] is True


@pytest.mark.parametrize(
    ("artifact_pr", "run_prs", "accepted"),
    [
        pytest.param("12", "12", True, id="matches-event"),
        pytest.param("12", "7 12", True, id="one-of-several"),
        pytest.param("13", "12", False, id="other-pr"),
        pytest.param("1", "12", False, id="substring-of-event-pr"),
        pytest.param("12", "", False, id="event-has-no-prs"),
        pytest.param("12 12", "12", False, id="not-digits"),
        pytest.param("", "12", False, id="empty"),
    ],
)
def test_docs_preview_deploy_only_trusts_the_event_pr_number(
    tmp_path: Path, artifact_pr: str, run_prs: str, accepted: bool
) -> None:
    step = _deploy_step("id", "metadata")
    assert "workflow_run.pull_requests.*.number" in step["env"]["RUN_PR_NUMBERS"]
    _write(tmp_path / "fern-docs-preview" / "fern-preview-metadata" / "pr_number", artifact_pr)
    output = tmp_path / "github_output"
    output.touch()

    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "RUN_PR_NUMBERS": run_prs, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )

    assert (result.returncode == 0) == accepted, result.stdout + result.stderr
    assert output.read_text() == (f"pr_number={artifact_pr}\n" if accepted else "")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def guard_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """PATH with yq; without a real yq (preinstalled on GitHub runners), a PyYAML stand-in for `yq -o=json . FILE`."""
    if shutil.which("yq"):
        return dict(os.environ)
    bin_dir = tmp_path_factory.mktemp("bin")
    yq = bin_dir / "yq"
    yq.write_text(
        f"#!{sys.executable}\nimport json, sys, yaml\n"
        "for doc in yaml.safe_load_all(open(sys.argv[-1])):\n    print(json.dumps(doc, default=str))\n"
    )
    yq.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.parametrize(
    ("files", "symlink", "allowed"),
    [
        pytest.param(
            {"docs.yml": "navigation:\n  - page: Home\n    path: ./pages/index.mdx\n"}, False, True, id="in-tree"
        ),
        pytest.param(
            {"versions/v.yml": "navigation:\n  - path: ../pages/index.mdx\n"}, False, True, id="dotdot-from-yaml-file"
        ),
        pytest.param({"docs.yml": "navigation:\n  - path: ../../outside.mdx\n"}, False, False, id="dotdot"),
        pytest.param({"versions/v.yml": "navigation:\n  - path: ../../x.mdx\n"}, False, False, id="dotdot-nested"),
        pytest.param({"docs.yml": "navigation:\n  - path: /proc/self/environ\n"}, False, False, id="absolute"),
        pytest.param({"docs.yml": "x: {path: '/etc/passwd'}\n"}, False, False, id="flow-style"),
        pytest.param({"docs.yml": "favicon: /proc/self/environ\n"}, False, False, id="favicon"),
        pytest.param({"docs.yml": "logo:\n  dark: ../../logo.svg\n"}, False, False, id="logo"),
        pytest.param({"docs.yml": "experimental:\n  mdx-components:\n    - /tmp\n"}, False, False, id="list-item"),
        pytest.param(
            {"docs.yml": "logo:\n  href: /nemo/x\nredirects:\n  - source: /a\n    destination: /b\n"},
            False,
            True,
            id="site-urls",
        ),
        pytest.param({"docs.yml": "navigation:\n  - path: ./pages/index.mdx\n"}, True, False, id="symlink"),
    ],
)
def test_docs_preview_deploy_rejects_paths_outside_the_artifact(
    tmp_path: Path, guard_env: dict[str, str], files: dict[str, str], symlink: bool, allowed: bool
) -> None:
    guard = _deploy_step("id", "paths")
    fern = tmp_path / "fern"
    _write(fern / "pages" / "index.mdx", "# Home\n")
    for name, text in files.items():
        _write(fern / name, text)
    if symlink:
        (fern / "pages" / "env.mdx").symlink_to("/proc/self/environ")
    script = tmp_path / "guard.py"
    script.write_text(guard["run"])

    result = subprocess.run([sys.executable, str(script)], cwd=fern, env=guard_env, capture_output=True, text=True)

    assert (result.returncode == 0) == allowed, result.stdout + result.stderr


def test_docs_preview_deploy_path_guard_accepts_the_real_docs_tree(guard_env: dict[str, str]) -> None:
    guard = _deploy_step("id", "paths")

    result = subprocess.run(
        [sys.executable, "-c", guard["run"]],
        cwd=WORKFLOWS_DIR.parents[1] / "fern",
        env=guard_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_docs_preview_deploy_keeps_github_token_away_from_fern() -> None:
    deploy = _deploy_step("id", "fern-preview")

    assert "generate --docs --preview" in deploy["run"]
    assert not any("github.token" in str(v) for v in deploy["env"].values())
    assert "GH_TOKEN" not in deploy["env"]
