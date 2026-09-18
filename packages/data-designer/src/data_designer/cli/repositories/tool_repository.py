# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from data_designer.cli.repositories.base import UserConfigSectionRepository
from data_designer.config.mcp import ToolConfig
from data_designer.config.user_config import UserConfig
from data_designer.config.utils.constants import TOOL_CONFIGS_FILE_NAME
from data_designer.config.utils.io_helpers import load_config_file, save_config_file


class ToolConfigRegistry(BaseModel):
    """Registry for tool configurations."""

    tool_configs: list[ToolConfig]


class ToolRepository(UserConfigSectionRepository[ToolConfigRegistry]):
    """Repository for tool configurations."""

    user_config_section = "tools.configs"

    @property
    def config_file(self) -> Path:
        """Get the tool configuration file path."""
        return self.config_dir / TOOL_CONFIGS_FILE_NAME

    def _from_user_config(self, user_config: UserConfig) -> ToolConfigRegistry | None:
        return None if user_config.tools.configs is None else ToolConfigRegistry(tool_configs=user_config.tools.configs)

    def _load_legacy(self) -> ToolConfigRegistry | None:
        """Load tool configuration from file."""
        if not self.config_file.exists():
            return None

        try:
            config_dict = load_config_file(self.config_file)
            return ToolConfigRegistry.model_validate(config_dict)
        except Exception:
            return None

    def _save_legacy(self, config: ToolConfigRegistry) -> None:
        """Save tool configuration to file."""
        config_dict = config.model_dump(mode="json", exclude_none=True)
        save_config_file(self.config_file, config_dict)
