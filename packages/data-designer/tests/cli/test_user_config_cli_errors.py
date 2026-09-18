# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# A subprocess, not typer's CliRunner: CliRunner.invoke(app, ...) bypasses main(), which is where
# config.toml errors are turned into a message. DATA_DESIGNER_HOME is also read at import time.
_RUN_CLI = "from data_designer.cli.main import main; main()"


@pytest.mark.parametrize(
    "args, expected_exit_code",
    [
        pytest.param(["config", "reset"], 1, id="config-reset"),
        pytest.param(["config", "models"], 1, id="config-models"),
        pytest.param(["config", "providers"], 1, id="config-providers"),
        pytest.param(["config", "mcp"], 1, id="config-mcp"),
        pytest.param(["config", "tools"], 1, id="config-tools"),
        pytest.param(["config", "list"], 0, id="config-list"),
    ],
)
def test_cli_reports_a_malformed_config_toml_without_a_traceback(
    tmp_path: Path, args: list[str], expected_exit_code: int
) -> None:
    (tmp_path / "config.toml").write_text("version = 1\n[[model.providers]\n", encoding="utf-8")
    env = {**os.environ, "DATA_DESIGNER_HOME": str(tmp_path), "COLUMNS": "200"}

    result = subprocess.run(
        [sys.executable, "-c", _RUN_CLI, *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = result.stdout + result.stderr
    assert result.returncode == expected_exit_code, output
    assert "config.toml" in output
    assert "Traceback" not in output
