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


_TOML_MODEL_PROVIDERS = '[[model.providers]]\nname = "toml-provider"\nendpoint = "http://localhost:8000/v1"\n'
_TOML_MCP_PROVIDERS = '[[mcp.providers]]\nprovider_type = "stdio"\nname = "toml-mcp"\ncommand = "python"\n'


@pytest.mark.parametrize(
    "command, config_toml, section",
    [
        pytest.param("providers", _TOML_MODEL_PROVIDERS, "model.providers", id="config-providers"),
        pytest.param(
            "models",
            _TOML_MODEL_PROVIDERS + '[[model.configs]]\nalias = "a"\nmodel = "m"\nprovider = "toml-provider"\n',
            "model.configs",
            id="config-models",
        ),
        pytest.param("mcp", _TOML_MCP_PROVIDERS, "mcp.providers", id="config-mcp"),
        pytest.param(
            "tools",
            _TOML_MCP_PROVIDERS + '[[tools.configs]]\ntool_alias = "t"\nproviders = ["toml-mcp"]\n',
            "tools.configs",
            id="config-tools",
        ),
    ],
)
def test_config_commands_refuse_a_toml_owned_section_before_changing_anything(
    tmp_path: Path, command: str, config_toml: str, section: str
) -> None:
    (tmp_path / "config.toml").write_text("version = 1\n" + config_toml, encoding="utf-8")
    # A YAML list that config.toml does not own; providers used to delete these models before being refused.
    model_configs_yaml = "model_configs:\n- alias: yaml-text\n  model: m\n  provider: toml-provider\n"
    (tmp_path / "model_configs.yaml").write_text(model_configs_yaml, encoding="utf-8")
    env = {**os.environ, "DATA_DESIGNER_HOME": str(tmp_path), "COLUMNS": "200"}

    result = subprocess.run(
        [sys.executable, "-c", _RUN_CLI, "config", command],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 0, output
    assert f"'{section}' is defined in" in output
    assert "Traceback" not in output
    assert (tmp_path / "model_configs.yaml").read_text(encoding="utf-8") == model_configs_yaml


def test_config_reset_skips_toml_owned_sections_without_prompting(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        "version = 1\n[model]\nconfigs = []\n" + _TOML_MODEL_PROVIDERS, encoding="utf-8"
    )
    env = {**os.environ, "DATA_DESIGNER_HOME": str(tmp_path), "COLUMNS": "200"}

    result = subprocess.run(
        [sys.executable, "-c", _RUN_CLI, "config", "reset"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 0, output
    assert "Skipped model providers configuration: defined in" in output
    assert "Skipped model configs configuration: defined in" in output
    assert "Delete model" not in output
    assert (tmp_path / "config.toml").exists()
