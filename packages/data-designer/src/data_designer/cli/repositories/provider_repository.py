# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from data_designer.cli.repositories.base import UserConfigSectionRepository
from data_designer.config.models import ModelProvider
from data_designer.config.user_config import UserConfig
from data_designer.config.utils.constants import MODEL_PROVIDERS_FILE_NAME
from data_designer.config.utils.io_helpers import load_config_file, save_config_file


class ModelProviderRegistry(BaseModel):
    """Registry for model provider configurations.

    Inherits from ``BaseModel`` directly so pydantic's default ``extra="ignore"``
    drops legacy ``default:`` keys from older on-disk YAMLs.
    """

    providers: list[ModelProvider]


class ProviderRepository(UserConfigSectionRepository[ModelProviderRegistry]):
    """Repository for provider configurations."""

    user_config_section = "model.providers"

    @property
    def config_file(self) -> Path:
        """Get the provider configuration file path."""
        return self.config_dir / MODEL_PROVIDERS_FILE_NAME

    def _from_user_config(self, user_config: UserConfig) -> ModelProviderRegistry | None:
        return (
            None
            if user_config.model.providers is None
            else ModelProviderRegistry(providers=user_config.model.providers)
        )

    def _load_legacy(self) -> ModelProviderRegistry | None:
        """Load provider configuration from file.

        ``extra="ignore"`` (pydantic v2 default) silently drops any legacy
        ``default:`` key carried over from older YAMLs, so existing on-disk
        configs continue to load cleanly after #590 dropped the field.
        """
        if not self.config_file.exists():
            return None

        try:
            config_dict = load_config_file(self.config_file)
        except Exception:
            return None

        try:
            return ModelProviderRegistry.model_validate(config_dict)
        except Exception:
            return None

    def _save_legacy(self, config: ModelProviderRegistry) -> None:
        """Save provider configuration to file."""
        config_dict = config.model_dump(mode="json", exclude_none=True)
        save_config_file(self.config_file, config_dict)
