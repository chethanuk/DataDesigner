# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Schema and loader for the user configuration file (``config.toml``) under ``DATA_DESIGNER_HOME``.

Each list is optional. A list the file leaves out is ``None`` and callers fall back to the
legacy per-concern YAML file for it; a list the file defines, even as ``[]``, takes precedence.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator

from data_designer.config.base import ConfigBase
from data_designer.config.errors import InvalidUserConfigError
from data_designer.config.mcp import MCPProviderT, ToolConfig
from data_designer.config.models import ModelConfig, ModelProvider

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class UserModelSection(ConfigBase):
    providers: list[ModelProvider] | None = None
    configs: list[ModelConfig] | None = None


class UserMCPSection(ConfigBase):
    providers: list[MCPProviderT] | None = None


class UserToolsSection(ConfigBase):
    configs: list[ToolConfig] | None = None


class UserConfig(ConfigBase):
    """Version 1 of the user configuration file."""

    version: Literal[1]
    model: UserModelSection = Field(default_factory=UserModelSection)
    mcp: UserMCPSection = Field(default_factory=UserMCPSection)
    tools: UserToolsSection = Field(default_factory=UserToolsSection)

    @field_validator("version", mode="before")
    @classmethod
    def _require_integer_version(cls, value: object) -> object:
        # Literal[1] alone coerces TOML `true` and `1.0` to 1, and Literal can't be marked strict.
        if type(value) is not int:
            raise ValueError(f"version must be an integer, got {value!r}")
        return value


def load_user_config(file_path: Path) -> UserConfig | None:
    """Load the user configuration file.

    Returns:
        The parsed file, or None if it does not exist.

    Raises:
        InvalidUserConfigError: If the file cannot be read, is not valid TOML or does not match the schema.
            The message names the file and, for schema errors, the dotted location.
    """
    try:
        with open(file_path, "rb") as f:
            content = tomllib.load(f)
    except FileNotFoundError:
        return None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise InvalidUserConfigError(f"Invalid TOML in {file_path}: {e}") from e
    except OSError as e:
        raise InvalidUserConfigError(f"Cannot read {file_path}: {e}") from e
    try:
        return UserConfig.model_validate(content)
    except ValidationError as e:
        # Locations and messages only, and no chained ValidationError: pydantic echoes input values, which can
        # include an api_key, and a traceback prints the chained cause too.
        errors = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors(include_input=False))
        raise InvalidUserConfigError(f"Invalid user configuration in {file_path}: {errors}") from None
