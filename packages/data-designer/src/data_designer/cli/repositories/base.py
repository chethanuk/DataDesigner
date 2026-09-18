# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel

from data_designer.config.user_config import UserConfig, load_user_config
from data_designer.config.utils.constants import USER_CONFIG_FILE_NAME

T = TypeVar("T", bound=BaseModel)


class ConfigRepository(ABC, Generic[T]):
    """Abstract base for configuration persistence."""

    def __init__(self, config_dir: Path):
        self.config_dir = config_dir

    @property
    @abstractmethod
    def config_file(self) -> Path:
        """Get the configuration file path."""

    @abstractmethod
    def load(self) -> T | None:
        """Load configuration from file."""

    @abstractmethod
    def save(self, config: T) -> None:
        """Save configuration to file."""

    def exists(self) -> bool:
        """Check if configuration file exists."""
        return self.config_file.exists()

    def delete(self) -> None:
        """Delete configuration file."""
        if self.exists():
            self.config_file.unlink()


class UserConfigSectionReadOnlyError(ValueError):
    """Raised when writing a section that the user configuration file (``config.toml``) defines."""


class UserConfigSectionRepository(ConfigRepository[T]):
    """Repository for settings that ``config.toml`` can also define.

    When ``config.toml`` in ``config_dir`` defines this repository's section, the section wins and the legacy
    file (``config_file``) is ignored. ``config.toml`` is only read for now, so saving or deleting a section it
    defines raises ``UserConfigSectionReadOnlyError`` instead of writing a legacy file that would be ignored.
    """

    user_config_section: ClassVar[str]

    @property
    def user_config_file(self) -> Path:
        """Get the user configuration file path."""
        return self.config_dir / USER_CONFIG_FILE_NAME

    @property
    def source_file(self) -> Path:
        """Get the file this repository's configuration is read from."""
        return self.user_config_file if self._load_user_config_section() is not None else self.config_file

    @abstractmethod
    def _from_user_config(self, user_config: UserConfig) -> T | None:
        """Return this repository's section of ``config.toml``, or None if the file does not define it."""

    @abstractmethod
    def _load_legacy(self) -> T | None:
        """Load configuration from the legacy file."""

    @abstractmethod
    def _save_legacy(self, config: T) -> None:
        """Save configuration to the legacy file."""

    def load(self) -> T | None:
        """Load configuration, preferring the ``config.toml`` section over the legacy file."""
        registry = self._load_user_config_section()
        return registry if registry is not None else self._load_legacy()

    def save(self, config: T) -> None:
        """Save configuration to the legacy file unless ``config.toml`` defines the section."""
        self._raise_if_defined_in_user_config()
        self._save_legacy(config)

    def exists(self) -> bool:
        """Check if ``config.toml`` defines the section or the legacy file exists."""
        return self._load_user_config_section() is not None or self.config_file.exists()

    def delete(self) -> None:
        """Delete the legacy file unless ``config.toml`` defines the section."""
        self._raise_if_defined_in_user_config()
        self.config_file.unlink(missing_ok=True)

    def _load_user_config_section(self) -> T | None:
        user_config = load_user_config(self.user_config_file)
        return None if user_config is None else self._from_user_config(user_config)

    def _raise_if_defined_in_user_config(self) -> None:
        if self._load_user_config_section() is not None:
            raise UserConfigSectionReadOnlyError(
                f"{self.user_config_section!r} is defined in {self.user_config_file}, which Data Designer only reads "
                "for now. Edit that file directly, or remove the section to manage these settings with the CLI."
            )
