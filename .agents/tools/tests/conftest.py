# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Make `.agents/tools` importable.

Under pytest's default ``prepend`` import mode the directory added to ``sys.path`` is the first
ancestor without an ``__init__.py`` - that is this ``tests/`` directory, not its parent - so the
tools themselves would not be importable without this.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
