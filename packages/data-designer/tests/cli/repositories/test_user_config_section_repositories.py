# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from data_designer.cli.repositories.base import UserConfigSectionReadOnlyError, UserConfigSectionRepository
from data_designer.cli.repositories.mcp_provider_repository import MCPProviderRepository
from data_designer.cli.repositories.model_repository import ModelRepository
from data_designer.cli.repositories.provider_repository import ProviderRepository
from data_designer.cli.repositories.tool_repository import ToolRepository
from data_designer.cli.services.model_service import ModelService
from data_designer.config.errors import InvalidUserConfigError
from data_designer.config.models import ModelConfig
from data_designer.config.utils.io_helpers import save_config_file


@dataclass(frozen=True)
class SectionCase:
    repository_cls: type[UserConfigSectionRepository[Any]]
    section: str
    legacy_content: dict[str, Any]
    toml_content: str
    names: Callable[[Any], list[str]]


SECTION_CASES = [
    pytest.param(
        SectionCase(
            repository_cls=ModelRepository,
            section="model.configs",
            legacy_content={"model_configs": [{"alias": "yaml-text", "model": "m", "provider": "p"}]},
            toml_content='[[model.configs]]\nalias = "toml-text"\nmodel = "m"\nprovider = "p"\n',
            names=lambda registry: [mc.alias for mc in registry.model_configs],
        ),
        id="model-configs",
    ),
    pytest.param(
        SectionCase(
            repository_cls=ProviderRepository,
            section="model.providers",
            legacy_content={"providers": [{"name": "yaml-provider", "endpoint": "http://localhost:9000/v1"}]},
            toml_content='[[model.providers]]\nname = "toml-provider"\nendpoint = "http://localhost:8000/v1"\n',
            names=lambda registry: [p.name for p in registry.providers],
        ),
        id="model-providers",
    ),
    pytest.param(
        SectionCase(
            repository_cls=MCPProviderRepository,
            section="mcp.providers",
            legacy_content={"providers": [{"provider_type": "stdio", "name": "yaml-mcp", "command": "python"}]},
            toml_content='[[mcp.providers]]\nprovider_type = "stdio"\nname = "toml-mcp"\ncommand = "python"\n',
            names=lambda registry: [p.name for p in registry.providers],
        ),
        id="mcp-providers",
    ),
    pytest.param(
        SectionCase(
            repository_cls=ToolRepository,
            section="tools.configs",
            legacy_content={"tool_configs": [{"tool_alias": "yaml-tools", "providers": ["yaml-mcp"]}]},
            toml_content='[[tools.configs]]\ntool_alias = "toml-tools"\nproviders = ["toml-mcp"]\n',
            names=lambda registry: [tc.tool_alias for tc in registry.tool_configs],
        ),
        id="tool-configs",
    ),
]


def _write_legacy(tmp_path: Path, case: SectionCase) -> Path:
    repository = case.repository_cls(tmp_path)
    save_config_file(repository.config_file, case.legacy_content)
    return repository.config_file


def _write_toml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("case", SECTION_CASES)
def test_config_toml_section_wins_over_legacy_file(tmp_path: Path, case: SectionCase) -> None:
    _write_legacy(tmp_path, case)
    toml_path = _write_toml(tmp_path, "version = 1\n" + case.toml_content)
    repository = case.repository_cls(tmp_path)

    registry = repository.load()

    assert registry is not None
    assert case.names(registry)[0].startswith("toml-")
    assert repository.source_file == toml_path


@pytest.mark.parametrize("case", SECTION_CASES)
def test_legacy_file_is_used_when_config_toml_leaves_the_section_out(tmp_path: Path, case: SectionCase) -> None:
    legacy_path = _write_legacy(tmp_path, case)
    _write_toml(tmp_path, "version = 1\n")
    repository = case.repository_cls(tmp_path)

    registry = repository.load()

    assert registry is not None
    assert case.names(registry)[0].startswith("yaml-")
    assert repository.source_file == legacy_path


@pytest.mark.parametrize("case", SECTION_CASES)
def test_section_defined_only_in_config_toml_exists(tmp_path: Path, case: SectionCase) -> None:
    _write_toml(tmp_path, "version = 1\n" + case.toml_content)
    repository = case.repository_cls(tmp_path)

    assert not repository.config_file.exists()
    assert repository.exists()


@pytest.mark.parametrize("case", SECTION_CASES)
@pytest.mark.parametrize("operation", ["save", "delete"])
def test_writes_to_a_config_toml_section_are_refused(tmp_path: Path, case: SectionCase, operation: str) -> None:
    legacy_path = _write_legacy(tmp_path, case)
    legacy_before = legacy_path.read_text()
    toml_path = _write_toml(tmp_path, "version = 1\n" + case.toml_content)
    toml_before = toml_path.read_text()
    repository = case.repository_cls(tmp_path)
    registry = repository.load()

    with pytest.raises(UserConfigSectionReadOnlyError) as exc_info:
        if operation == "save":
            repository.save(registry)
        else:
            repository.delete()

    assert isinstance(exc_info.value, ValueError)
    assert case.section in str(exc_info.value)
    assert str(toml_path) in str(exc_info.value)
    assert legacy_path.read_text() == legacy_before
    assert toml_path.read_text() == toml_before


@pytest.mark.parametrize("case", SECTION_CASES)
def test_malformed_config_toml_fails_loudly(tmp_path: Path, case: SectionCase) -> None:
    _write_legacy(tmp_path, case)
    toml_path = _write_toml(tmp_path, "version = 1\n[[model.providers]\n")
    repository = case.repository_cls(tmp_path)

    with pytest.raises(InvalidUserConfigError, match="config.toml"):
        repository.load()
    assert toml_path.exists()


def test_adding_a_model_to_a_config_toml_section_reports_the_file(
    tmp_path: Path, stub_new_model_config: ModelConfig
) -> None:
    toml_path = _write_toml(
        tmp_path, 'version = 1\n[[model.configs]]\nalias = "toml-text"\nmodel = "m"\nprovider = "p"\n'
    )
    service = ModelService(ModelRepository(tmp_path))

    with pytest.raises(ValueError, match="model.configs") as exc_info:
        service.add(stub_new_model_config)

    assert str(toml_path) in str(exc_info.value)
    assert not (tmp_path / "model_configs.yaml").exists()
