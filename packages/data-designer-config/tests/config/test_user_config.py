# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from data_designer.config.errors import InvalidConfigError, InvalidUserConfigError
from data_designer.config.mcp import LocalStdioMCPProvider, MCPProvider, ToolConfig
from data_designer.config.models import ChatCompletionInferenceParams, ModelConfig, ModelProvider
from data_designer.config.user_config import UserConfig, load_user_config

FULL_USER_CONFIG = """
version = 1

[[model.providers]]
name = "my-nim"
endpoint = "http://localhost:8000/v1"
provider_type = "openai"

[[model.configs]]
alias = "local-text"
model = "meta/llama-3.1-8b-instruct"
provider = "my-nim"

[model.configs.inference_parameters]
generation_type = "chat-completion"
temperature = 0.7

[[mcp.providers]]
provider_type = "sse"
name = "remote-tools"
endpoint = "http://localhost:8080/sse"

[[mcp.providers]]
provider_type = "stdio"
name = "local-tools"
command = "python"
args = ["-m", "my_mcp_server"]

[[tools.configs]]
tool_alias = "default"
providers = ["local-tools"]
"""


def _write(tmp_path: Path, content: str | bytes) -> Path:
    path = tmp_path / "config.toml"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_load_user_config_returns_none_when_file_is_absent(tmp_path: Path) -> None:
    assert load_user_config(tmp_path / "config.toml") is None


def test_load_user_config_reads_every_section(tmp_path: Path) -> None:
    user_config = load_user_config(_write(tmp_path, FULL_USER_CONFIG))

    assert user_config == UserConfig(
        version=1,
        model={
            "providers": [ModelProvider(name="my-nim", endpoint="http://localhost:8000/v1", provider_type="openai")],
            "configs": [
                ModelConfig(
                    alias="local-text",
                    model="meta/llama-3.1-8b-instruct",
                    provider="my-nim",
                    inference_parameters=ChatCompletionInferenceParams(temperature=0.7),
                )
            ],
        },
        mcp={
            "providers": [
                MCPProvider(name="remote-tools", endpoint="http://localhost:8080/sse"),
                LocalStdioMCPProvider(name="local-tools", command="python", args=["-m", "my_mcp_server"]),
            ]
        },
        tools={"configs": [ToolConfig(tool_alias="default", providers=["local-tools"])]},
    )


@pytest.mark.parametrize(
    "content, expected_model_providers, expected_model_configs",
    [
        pytest.param("version = 1\n", None, None, id="sections-left-out-are-undefined"),
        pytest.param("version = 1\n[model]\nproviders = []\n", [], None, id="empty-list-is-defined"),
    ],
)
def test_load_user_config_distinguishes_undefined_from_empty(
    tmp_path: Path,
    content: str,
    expected_model_providers: list[ModelProvider] | None,
    expected_model_configs: list[ModelConfig] | None,
) -> None:
    user_config = load_user_config(_write(tmp_path, content))

    assert user_config is not None
    assert user_config.model.providers == expected_model_providers
    assert user_config.model.configs == expected_model_configs
    assert user_config.mcp.providers is None
    assert user_config.tools.configs is None


@pytest.mark.parametrize(
    "content, expected_location",
    [
        pytest.param("version = 1\n[[model.providers]\n", "Invalid TOML", id="malformed-toml"),
        pytest.param(b"version = 1\n# \xff\n", "Invalid TOML", id="invalid-utf8"),
        pytest.param("[model]\nproviders = []\n", "version", id="missing-version"),
        pytest.param("version = 2\n", "version", id="unsupported-version"),
        pytest.param("version = 1\n[run]\nbuffer_size = 10\n", "run", id="unknown-section"),
        pytest.param(
            'version = 1\n[[model.configs]]\nalias = "a"\nmodel = "m"\n',
            "model.configs.0.provider",
            id="entry-missing-required-field",
        ),
    ],
)
def test_load_user_config_fails_loudly_with_file_and_location(
    tmp_path: Path, content: str | bytes, expected_location: str
) -> None:
    path = _write(tmp_path, content)

    with pytest.raises(InvalidUserConfigError) as exc_info:
        load_user_config(path)

    assert isinstance(exc_info.value, InvalidConfigError)
    assert str(path) in str(exc_info.value)
    assert expected_location in str(exc_info.value)


def test_load_user_config_reports_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.mkdir()

    with pytest.raises(InvalidUserConfigError, match="Cannot read") as exc_info:
        load_user_config(path)

    assert str(path) in str(exc_info.value)
