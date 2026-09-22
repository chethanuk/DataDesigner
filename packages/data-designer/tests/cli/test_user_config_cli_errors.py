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
    "args",
    [
        pytest.param(["config", "reset"], id="config-reset"),
        pytest.param(["config", "models"], id="config-models"),
        pytest.param(["config", "providers"], id="config-providers"),
        pytest.param(["config", "mcp"], id="config-mcp"),
        pytest.param(["config", "tools"], id="config-tools"),
        pytest.param(["config", "list"], id="config-list"),
    ],
)
def test_cli_reports_a_malformed_config_toml_without_a_traceback(tmp_path: Path, args: list[str]) -> None:
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
    assert result.returncode == 1, output
    assert output.count("Invalid TOML in") == 1, output
    assert "config.toml" in output
    assert "Traceback" not in output


@pytest.mark.parametrize(
    "config_toml, expected_in_output, not_expected_in_output",
    [
        pytest.param(None, [], ["User configuration file", "toml-text"], id="no-config-toml"),
        pytest.param(
            'version = 1\n[[model.configs]]\nalias = "toml-text"\nmodel = "toml-model"\nprovider = "nvidia"\n',
            ["User configuration file", "config.toml", "toml-text"],
            [],
            id="config-toml-defines-model-configs",
        ),
    ],
)
def test_config_list_shows_the_user_configuration_file_and_its_settings(
    tmp_path: Path, config_toml: str | None, expected_in_output: list[str], not_expected_in_output: list[str]
) -> None:
    if config_toml is not None:
        (tmp_path / "config.toml").write_text(config_toml, encoding="utf-8")
    env = {**os.environ, "DATA_DESIGNER_HOME": str(tmp_path), "COLUMNS": "200"}

    result = subprocess.run(
        [sys.executable, "-c", _RUN_CLI, "config", "list"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    for text in expected_in_output:
        assert text in output
    for text in not_expected_in_output:
        assert text not in output
