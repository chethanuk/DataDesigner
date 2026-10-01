# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Behavior tests for fork isolation in .github/workflows/agentic-ci-pr-review.yml (issue #804).

Two layers:
- Behavioral: the gate's "Resolve PR context" script runs under ``bash -e`` with a fake ``gh``.
- Structural invariants of the workflow YAML (no fork checkout, no claude, secret separation, routing).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).parents[3] / ".github/workflows/agentic-ci-pr-review.yml"
REPO_ID = 1001
BASE_SHA = "b" * 40
HEAD_SHA = "a" * 40
OTHER_SHA = "c" * 40
MODEL_SECRET = re.compile(r"AGENTIC_CI_API_KEY|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps(job: str) -> list[dict[str, Any]]:
    return _workflow()["jobs"][job].get("steps", [])


def _resolve_step() -> dict[str, Any]:
    return next(s for s in _steps("gate") if s.get("name") == "Resolve PR context")


def _all_checkouts() -> list[tuple[str, dict[str, Any]]]:
    return [
        (job, step)
        for job, body in _workflow()["jobs"].items()
        for step in body.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


def _pr(head_repo_id: int | None, head_sha: str = HEAD_SHA) -> dict[str, Any]:
    head_repo = None if head_repo_id is None else {"id": head_repo_id}
    return {"number": 42, "head": {"sha": head_sha, "repo": head_repo}, "base": {"sha": BASE_SHA}}


def _run_resolve(
    *,
    event_name: str = "pull_request_target",
    pr: dict[str, Any] | None = None,
    api: str = "ok",
    pr_number: str = "42",
    expected_head_sha: str = "",
    repo_id: str = str(REPO_ID),
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run the gate step script. Event runs read the payload; dispatch runs read `gh api pulls/N`."""
    script = _resolve_step()["run"]
    with TemporaryDirectory() as directory:
        path = Path(directory)
        gh = path / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            'if [ "$API_SCENARIO" = "error" ]; then echo "gh: Service Unavailable (HTTP 503)" >&2; exit 1; fi\n'
            'printf "%s\\n" "$PR_JSON"\n'
        )
        gh.chmod(0o755)
        event = path / "event.json"
        event.write_text(json.dumps({"pull_request": pr} if event_name != "workflow_dispatch" else {}))
        output = path / "output"
        output.write_text("")
        env = os.environ | {
            "PATH": f"{path}:{os.environ['PATH']}",
            "GH_TOKEN": "x",
            "EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_OUTPUT": str(output),
            "REPO": "NVIDIA-NeMo/DataDesigner",
            "REPO_ID": repo_id,
            "PR_NUMBER": pr_number,
            "EXPECTED_HEAD_SHA": expected_head_sha,
            "API_SCENARIO": api,
            "PR_JSON": json.dumps(pr or {}),
        }
        result = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
        return result, outputs


@pytest.mark.parametrize(
    "kwargs, expected_exit, expected",
    [
        pytest.param(
            {"pr": _pr(REPO_ID)}, 0, {"is_fork": "false", "head_sha": HEAD_SHA, "base_sha": BASE_SHA}, id="same-repo"
        ),
        pytest.param({"pr": _pr(2002)}, 0, {"is_fork": "true", "head_sha": HEAD_SHA, "base_sha": BASE_SHA}, id="fork"),
        pytest.param({"pr": _pr(None)}, 0, {"is_fork": "true"}, id="null-head-repo-is-fork"),
        pytest.param({"pr": _pr(REPO_ID, head_sha="")}, 1, {}, id="empty-head-sha"),
        pytest.param({"pr": _pr(REPO_ID, head_sha="abc123")}, 1, {}, id="short-head-sha"),
        pytest.param({"pr": _pr(REPO_ID, head_sha="Z" * 40)}, 1, {}, id="non-hex-head-sha"),
        pytest.param({"pr": _pr(REPO_ID), "repo_id": ""}, 1, {}, id="empty-repo-id-never-picks-privileged-path"),
        pytest.param({"pr": _pr(REPO_ID), "pr_number": "abc"}, 1, {}, id="non-numeric-pr-number"),
        pytest.param(
            {"event_name": "workflow_dispatch", "pr": _pr(REPO_ID)},
            0,
            {"is_fork": "false", "head_sha": HEAD_SHA, "base_sha": BASE_SHA},
            id="dispatch-same-repo-gets-base-sha-from-api",
        ),
        pytest.param(
            {"event_name": "workflow_dispatch", "pr": _pr(2002)},
            0,
            {"is_fork": "true", "base_sha": BASE_SHA},
            id="dispatch-fork",
        ),
        pytest.param(
            {"event_name": "workflow_dispatch", "pr": _pr(REPO_ID), "api": "error"},
            1,
            {},
            id="dispatch-api-error-fails-closed",
        ),
        pytest.param(
            {"event_name": "workflow_dispatch", "pr": _pr(REPO_ID), "expected_head_sha": OTHER_SHA},
            1,
            {},
            id="dispatch-stale-expected-head-sha",
        ),
        pytest.param(
            {"event_name": "workflow_dispatch", "pr": _pr(REPO_ID), "expected_head_sha": HEAD_SHA},
            0,
            {"is_fork": "false"},
            id="dispatch-matching-expected-head-sha",
        ),
    ],
)
def test_resolve_pr_context_routes_and_fails_closed(
    kwargs: dict[str, Any], expected_exit: int, expected: dict[str, str]
) -> None:
    result, outputs = _run_resolve(**kwargs)
    assert result.returncode == expected_exit, result.stderr
    for key, value in expected.items():
        assert outputs.get(key) == value
    if expected_exit != 0:
        # A failed resolution must not leave a routable is_fork value behind.
        assert outputs.get("is_fork") not in ("true", "false")


def test_gate_exposes_routing_outputs() -> None:
    outputs = _workflow()["jobs"]["gate"]["outputs"]
    for name in ("allowed", "is_fork", "head_sha", "base_sha", "number"):
        assert name in outputs, name


@pytest.mark.parametrize(
    "job, expected",
    [("review-internal", "is_fork == 'false'"), ("review-fork", "is_fork == 'true'")],
)
def test_review_jobs_route_on_exact_string_equality(job: str, expected: str) -> None:
    condition = _workflow()["jobs"][job]["if"]
    assert "needs.gate.outputs.allowed == 'true'" in condition
    assert f"needs.gate.outputs.{expected}" in condition
    assert "!=" not in condition
    assert "!needs" not in condition and "! needs" not in condition


def test_no_checkout_persists_credentials() -> None:
    checkouts = _all_checkouts()
    assert checkouts, "expected at least one actions/checkout"
    for job, step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False, (job, step.get("name"))


def test_unsafe_pr_checkout_opt_in_is_gone() -> None:
    assert "allow-unsafe-pr-checkout" not in WORKFLOW.read_text()


def test_fork_job_never_checks_out_fork_head() -> None:
    assert "review-fork" in _workflow()["jobs"]
    checkouts = [step for job, step in _all_checkouts() if job == "review-fork"]
    for step in checkouts:
        with_ = step.get("with", {})
        assert "sparse-checkout" in with_, step
        ref = str(with_.get("ref", ""))
        assert "head_sha" not in ref and "head.sha" not in ref and "pull/" not in ref
        assert "repository" not in with_
    assert not any("head_sha" in json.dumps(s) for s in checkouts)


def test_fork_job_has_no_checkout_at_all() -> None:
    # Non-vacuous guard: a checkout added later must be a conscious decision that also revisits the test above.
    assert [step for job, step in _all_checkouts() if job == "review-fork"] == []


def test_fork_job_has_no_claude_and_no_tools() -> None:
    body = json.dumps(_workflow()["jobs"]["review-fork"])
    assert not re.search(r"\bclaude\b(?! *-?code-action)", body.replace("agentic-ci", "")), "claude CLI in fork job"
    for step in _steps("review-fork"):
        run = str(step.get("run", ""))
        assert not re.search(r"""["']?\btools["']?\s*:|--rawfile tools|--arg tools""", run), step.get("name")


def test_fork_job_secrets_are_separated_at_every_env_level() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["review-fork"]
    inherited = json.dumps(workflow.get("env", {})) + json.dumps(job.get("env", {}))
    assert "GH_TOKEN" not in inherited and not MODEL_SECRET.search(inherited), "secret in workflow/job env"
    for step in job["steps"]:
        text = json.dumps(step.get("env", {}))
        assert not ("GH_TOKEN" in text and MODEL_SECRET.search(text)), step.get("name")
    assert any(MODEL_SECRET.search(json.dumps(s.get("env", {}))) for s in job["steps"]), "model step missing"


def test_fork_job_run_blocks_never_expand_secrets_inline() -> None:
    for step in _steps("review-fork"):
        assert not re.search(r"secrets\.|github\.token", str(step.get("run", ""))), step.get("name")


def test_fork_job_run_blocks_never_interpolate_untrusted_pr_text() -> None:
    pattern = re.compile(r"github\.event\.pull_request\.(title|body|head\.ref)|github\.head_ref")
    for step in _steps("review-fork"):
        assert not pattern.search(str(step.get("run", ""))), step.get("name")


def test_fork_diff_is_pinned_to_validated_head_sha_via_compare() -> None:
    runs = "\n".join(str(s.get("run", "")) for s in _steps("review-fork"))
    assert re.search(r"compare/\$\{?BASE_SHA\}?\.\.\.\$\{?HEAD_SHA\}?", runs)
    assert not re.search(r"pulls/[^\n]*\.diff|gh pr diff|git fetch", runs), "mutable diff source in fork job"


def _run_model_call(
    *, curl: str, resp: str = "", http_code: str = "200"
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the fork job's "Call model" script with stub curl; returns (result, review.md text)."""
    script = next(s for s in _steps("review-fork") if s.get("name") == "Call model")["run"]
    with TemporaryDirectory() as directory:
        path = Path(directory)
        data = path / "pr-data"
        data.mkdir()
        for name in ("title.txt", "body.txt", "diff.txt"):
            (data / name).write_text("x\n")
        stub = path / "curl"
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$CURL_MODE" = "network" ]; then exit 7; fi\n'
            'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
            'printf "%s" "$RESP" > "$out"\n'
            'printf "%s" "$HTTP_CODE"\n'
        )
        stub.chmod(0o755)
        env = os.environ | {
            "PATH": f"{path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(path),
            "ANTHROPIC_BASE_URL": "https://example.invalid",
            "ANTHROPIC_API_KEY": "k",
            "AGENTIC_CI_MODEL": "m",
            "PR_NUMBER": "42",
            "CURL_MODE": curl,
            "RESP": resp,
            "HTTP_CODE": http_code,
        }
        result = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
        review = data / "review.md"
        return result, review.read_text() if review.exists() else ""


def _resp(*blocks: dict[str, Any], stop_reason: str = "end_turn") -> str:
    return json.dumps({"content": list(blocks), "stop_reason": stop_reason})


TEXT = {"type": "text", "text": "Looks fine."}


@pytest.mark.parametrize(
    "kwargs, ok, error_line, review_has",
    [
        pytest.param({"curl": "ok", "resp": _resp(TEXT)}, True, False, "Looks fine.", id="text-review"),
        pytest.param({"curl": "ok", "resp": _resp()}, False, True, None, id="empty-content-is-not-a-review"),
        pytest.param(
            {"curl": "ok", "resp": _resp({"type": "tool_use", "id": "t", "name": "n", "input": {}})},
            False,
            True,
            None,
            id="only-non-text-blocks-is-not-a-review",
        ),
        pytest.param(
            {"curl": "ok", "resp": _resp({"type": "text", "text": " \n"})}, False, True, None, id="whitespace-only-text"
        ),
        pytest.param({"curl": "ok", "resp": "{}", "http_code": "500"}, False, True, None, id="http-500"),
        pytest.param({"curl": "network"}, False, True, None, id="curl-network-error"),
        pytest.param({"curl": "ok", "resp": "<html>oops</html>"}, False, True, None, id="non-json-200"),
        pytest.param(
            {"curl": "ok", "resp": _resp(TEXT, stop_reason="max_tokens")},
            True,
            False,
            "review truncated",
            id="max-tokens-marked",
        ),
    ],
)
def test_call_model_never_posts_an_empty_review(
    kwargs: dict[str, str], ok: bool, error_line: bool, review_has: str | None
) -> None:
    result, review = _run_model_call(**kwargs)
    assert (result.returncode == 0) is ok, (result.stdout, result.stderr)
    if error_line:
        assert "::error::" in result.stdout + result.stderr
    if review_has is not None:
        assert review_has in review
    else:
        assert not review.strip(), "review.md must not carry content on a failed model call"
