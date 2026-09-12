# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from unittest.mock import patch

import pytest
from wcwidth import wcswidth

from data_designer.engine.models.clients.errors import ProviderError, ProviderErrorKind
from data_designer.engine.models.clients.model_request_executor import ModelRequestExecutor
from data_designer.engine.models.clients.retry import RetryConfig
from data_designer.engine.models.clients.types import AssistantMessage, ChatCompletionRequest, ChatCompletionResponse
from data_designer.engine.models.request_admission.controller import AdaptiveRequestAdmissionController
from data_designer.engine.models.usage_events import TokenUsageEvent, emit_token_usage_event
from data_designer.engine.observability import (
    RequestAdmissionEvent,
    RuntimeCorrelation,
    emit_request_admission_event,
    runtime_correlation_provider,
)
from data_designer.engine.progress.reporter import AsyncProgressReporter
from data_designer.engine.progress.terminal.throughput_panel import (
    _CHART_LINE_COUNT,
    _MAX_RATE_SAMPLES,
    _MIN_CHART_LINE_COUNT,
    _RATE_SAMPLE_INTERVAL_SECONDS,
    TerminalThroughputPanel,
    _BarState,
    _fit_ansi,
    _fit_series,
)
from data_designer.engine.progress.tracker import ProgressTracker

CURSOR_UP_CLEAR = "\033[A\033[2K"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
ERASE_TO_END = "\033[J"
_ALL_ANSI_RE = re.compile(r"\033\[[0-9;?]*[a-zA-Z]")


class FakeTTY(io.StringIO):
    """StringIO that reports itself as a TTY so TerminalThroughputPanel activates."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def tty_stream() -> FakeTTY:
    return FakeTTY()


@pytest.fixture(autouse=True)
def fixed_terminal_size() -> Iterator[None]:
    with patch.object(shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))):
        yield


def _clean(text: str) -> str:
    return _ALL_ANSI_RE.sub("", text).replace("\r", "")


def _correlation(run_id: str) -> RuntimeCorrelation:
    return RuntimeCorrelation(
        run_id=run_id,
        row_group=0,
        task_column="col_a",
        task_type="cell",
        scheduling_group_kind="model",
        scheduling_group_identity_hash="hash",
        task_execution_id="task-exec",
    )


def _last_panel_lines(output: str) -> list[str]:
    clean = _clean(output)
    panel_start = clean.rfind("\n╭")
    panel_start = panel_start + 1 if panel_start >= 0 else clean.rfind("╭")
    assert panel_start >= 0
    return clean[panel_start:].splitlines()


def _chart_lines(panel_lines: list[str]) -> list[str]:
    separator_index = next(index for index, line in enumerate(panel_lines) if "├" in line)
    return panel_lines[2:separator_index]


def _marker_positions(panel_lines: list[str]) -> list[tuple[int, int]]:
    return [(row_index, line.index("◆")) for row_index, line in enumerate(_chart_lines(panel_lines)) if "◆" in line]


def test_no_output_when_not_tty() -> None:
    stream = io.StringIO()
    with TerminalThroughputPanel(stream=stream) as bar:
        bar.add_bar("a", "col_a", 10)
        bar.update("a", completed=5, success=5)
    assert stream.getvalue() == ""


def test_hides_and_shows_cursor(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream):
        pass
    output = tty_stream.getvalue()
    assert output.startswith(HIDE_CURSOR)
    assert output.endswith(SHOW_CURSOR)


def test_tiny_terminal_falls_back_to_no_panel(tty_stream: FakeTTY) -> None:
    with patch.object(shutil, "get_terminal_size", return_value=os.terminal_size((20, 24))):
        with TerminalThroughputPanel(stream=tty_stream) as bar:
            assert bar.is_active is False
            bar.add_bar("a", "col_a", 10)
            bar.update("a", completed=5, success=5, force=True)

    assert tty_stream.getvalue() == ""


def test_short_terminal_falls_back_to_no_panel(tty_stream: FakeTTY) -> None:
    with patch.object(shutil, "get_terminal_size", return_value=os.terminal_size((80, 9))):
        with TerminalThroughputPanel(stream=tty_stream) as bar:
            assert bar.is_active is False
            bar.add_bar("a", "col_a", 10)
            bar.update("a", completed=5, success=5, force=True)

    assert tty_stream.getvalue() == ""


def test_only_one_panel_owns_a_stream(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as outer:
        with TerminalThroughputPanel(stream=tty_stream) as inner:
            assert outer.is_active is True
            assert inner.is_active is False
        assert outer.is_active is True

    assert tty_stream.getvalue().count(HIDE_CURSOR) == 1
    assert tty_stream.getvalue().count(SHOW_CURSOR) == 1


def test_active_panel_falls_back_when_terminal_shrinks(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        bar.add_bar("a", "column 'a'", 100)
        snapshot = tty_stream.getvalue()
        with patch.object(shutil, "get_terminal_size", return_value=os.terminal_size((20, 5))):
            bar.update("a", completed=1, success=1, force=True)
            assert bar.is_active is False
            assert bar.drawn_lines == 0

        resize_output = tty_stream.getvalue()[len(snapshot) :]
        assert ERASE_TO_END in resize_output
        assert resize_output.index(ERASE_TO_END) < resize_output.index(SHOW_CURSOR)

    assert tty_stream.getvalue().count(HIDE_CURSOR) == 1
    assert tty_stream.getvalue().count(SHOW_CURSOR) == 1


def test_renders_bounded_throughput_panel(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        bar.add_bar("a", "column 'a'", 100)
        bar.add_bar("b", "column 'b'", 100)
        bar.update_many({"a": (10, 10, 0, 0), "b": (20, 20, 0, 0)}, force=True)

        assert bar.drawn_lines == 22
        panel_lines = _last_panel_lines(tty_stream.getvalue())
        panel = "\n".join(panel_lines)
        assert "Throughput" in panel
        assert "rec/s" in panel
        assert "now rec/s" in panel
        assert "avg rec/s" in panel
        assert "column 'a'" in panel
        assert "10/100" in panel
        assert "column 'b'" in panel
        assert "20/100" in panel
        header = next(line for line in panel_lines if "now rec/s" in line)
        row = next(line for line in panel_lines if "column 'a'" in line)
        assert "|" not in header
        assert "|" not in row
        assert "in tok/s" not in panel
        assert "out tok/s" not in panel
        assert header.index("avg rec/s") < header.index("done")
        assert "━" in row
        assert row.rindex("0.0") < row.index("10/100")
        assert row.index("10/100") < row.index("━")
        assert "╭" in panel
        assert "╰" in panel


def test_model_usage_rates_render_in_separate_table(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        bar.add_bar("a", "column 'a'", 100)
        bar.update("a", completed=10, success=10, force=True)
        bar._start_time = time.perf_counter() - 10.0  # noqa: SLF001
        bar.record_model_usage(
            model_alias="test",
            model_name="test-model",
            input_tokens=100,
            output_tokens=25,
            force=True,
        )

        panel = "\n".join(_last_panel_lines(tty_stream.getvalue()))
        assert "model alias" in panel
        assert "model name" in panel
        assert "test" in panel
        assert "test-model" in panel
        assert "rpm" in panel
        assert "in tok/s" in panel
        assert "out tok/s" in panel
        assert "6.0" in panel
        assert "10.0" in panel
        assert "2.5" in panel


def test_many_columns_and_models_flex_chart_to_fit_viewport(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        for index in range(4):
            bar.add_bar(f"col_{index}", f"column_{index}", 100)
        bar.update_many(
            {f"col_{index}": (index + 1, index + 1, 0, 0) for index in range(4)},
            force=True,
        )
        for index in range(4):
            bar.record_model_usage(
                model_alias=f"model_{index}",
                model_name=f"provider/model-{index}",
                input_tokens=100 + index,
                output_tokens=10 + index,
                force=True,
            )

        panel_lines = _last_panel_lines(tty_stream.getvalue())
        panel = "\n".join(panel_lines)
        assert len(_chart_lines(panel_lines)) < _CHART_LINE_COUNT
        assert len(panel_lines) == 22
        assert "... +" not in panel
        for index in range(4):
            assert f"column_{index}" in panel
            assert f"model_{index}" in panel


def test_large_tables_are_truncated_after_chart_reaches_minimum(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        for index in range(8):
            bar.add_bar(f"col_{index}", f"column_{index}", 100)
        bar.update_many(
            {f"col_{index}": (index + 1, index + 1, 0, 0) for index in range(8)},
            force=True,
        )
        for index in range(8):
            bar.record_model_usage(
                model_alias=f"model_{index}",
                model_name=f"provider/model-{index}",
                input_tokens=100 + index,
                output_tokens=10 + index,
                force=True,
            )

        panel_lines = _last_panel_lines(tty_stream.getvalue())
        panel = "\n".join(panel_lines)
        assert len(_chart_lines(panel_lines)) == _MIN_CHART_LINE_COUNT
        assert len(panel_lines) <= shutil.get_terminal_size().lines - 1
        assert "... +5 models" in panel


def test_overflow_summary_keeps_both_counts_at_minimum_width(tty_stream: FakeTTY) -> None:
    with patch.object(shutil, "get_terminal_size", return_value=os.terminal_size((30, 10))):
        with TerminalThroughputPanel(stream=tty_stream) as bar:
            for index in range(8):
                bar.add_bar(f"col_{index}", f"column_{index}", 100)
            for index in range(20):
                bar.record_model_usage(
                    model_alias=f"model_{index}",
                    model_name=f"provider/model-{index}",
                    input_tokens=0,
                    output_tokens=0,
                    force=True,
                )

            panel_lines = _last_panel_lines(tty_stream.getvalue())
            assert len(panel_lines) <= 9
            assert "... +8 columns, +20 models" in "\n".join(panel_lines)


def test_fit_ansi_closes_style_after_truncation() -> None:
    assert _fit_ansi("\033[31mabcdef\033[0m", 3) == "abc\033[0m"


def test_feedback_marker_reprojects_as_elapsed_time_grows(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        bar.add_bar("a", "column_a", 100)
        state = bar._bars["a"]  # noqa: SLF001
        state.rates = [0.0, 10.0, 20.0]
        state.latest_rate = 12.0
        bar._start_time = time.perf_counter() - 10.0  # noqa: SLF001

        bar.record_feedback_signal(force=True)
        before_positions = _marker_positions(_last_panel_lines(tty_stream.getvalue()))
        assert before_positions

        bar._start_time = time.perf_counter() - 100.0  # noqa: SLF001
        bar.update("a", completed=20, success=20, force=True)
        after_positions = _marker_positions(_last_panel_lines(tty_stream.getvalue()))

        assert after_positions
        assert after_positions[0][1] < before_positions[0][1]


def test_control_sequences_are_removed_from_labels(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        bar.add_bar("a", "col\x1b[31m_a\nsuffix", 100)
        bar.update("a", completed=10, success=10, force=True)

        clean = _clean(tty_stream.getvalue())
        assert "col_asuffix" in clean


def test_rate_samples_are_bounded() -> None:
    state = _BarState(label="col_a", total=1_000_000, start_time=0.0, last_sample_time=0.0)

    for index in range(_MAX_RATE_SAMPLES + 5):
        completed = (index + 1) * 10
        state.record_update(
            completed=completed,
            success=completed,
            failed=0,
            skipped=0,
            now=(index + 1) * _RATE_SAMPLE_INTERVAL_SECONDS,
        )

    assert len(state.rates) == _MAX_RATE_SAMPLES


def test_completed_rate_sample_is_not_diluted_by_later_updates() -> None:
    state = _BarState(label="col_a", total=100, start_time=0.0, last_sample_time=0.0)

    state.record_update(completed=100, success=100, failed=0, skipped=0, now=10.0)
    completed_rates = list(state.rates)
    completed_latest_rate = state.latest_rate

    for now in (12.0, 14.0, 16.0):
        state.record_update(completed=100, success=100, failed=0, skipped=0, now=now)

    assert state.rates == completed_rates
    assert state.latest_rate == completed_latest_rate


def test_sparse_rate_samples_span_chart_width() -> None:
    fitted = _fit_series([0.0, 10.0, 5.0], 7)

    assert len(fitted) == 7
    assert fitted[0] == 0.0
    assert fitted[3] == pytest.approx(10.0)
    assert fitted[-1] == 5.0


def test_frequent_updates_are_redraw_throttled(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        bar.add_bar("a", "col_a", 100)
        bar.add_bar("b", "col_b", 100)
        bar.update_many({"a": (1, 1, 0, 0), "b": (2, 2, 0, 0)}, force=True)

        snapshot = tty_stream.getvalue()
        for i in range(50):
            bar.update_many({"a": (i, i, 0, 0), "b": (i * 2, i * 2, 0, 0)})

        assert tty_stream.getvalue()[len(snapshot) :].count(CURSOR_UP_CLEAR) == 0

        bar.update("a", completed=50, success=50, force=True)
        assert tty_stream.getvalue()[len(snapshot) :].count(CURSOR_UP_CLEAR) == 22
        assert bar.drawn_lines == 22


def test_log_interleaving_preserves_panel_height(tty_stream: FakeTTY) -> None:
    root_logger = logging.getLogger()
    handler = logging.StreamHandler(tty_stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(handler)

    try:
        with TerminalThroughputPanel(stream=tty_stream) as bar:
            bar.add_bar("x", "col_x", 100)
            bar.add_bar("y", "col_y", 100)

            for i in range(10):
                bar.update("x", completed=i, success=i)
                root_logger.info("log at step %d", i)
                bar.update("y", completed=i, success=i)

            snapshot = tty_stream.getvalue()
            bar.update("x", completed=20, success=20, force=True)
            assert tty_stream.getvalue()[len(snapshot) :].count(CURSOR_UP_CLEAR) == 22
    finally:
        root_logger.removeHandler(handler)


def test_narrow_terminal_keeps_panel_within_width(tty_stream: FakeTTY) -> None:
    narrow = os.terminal_size((36, 24))
    with patch.object(shutil, "get_terminal_size", return_value=narrow):
        with TerminalThroughputPanel(stream=tty_stream) as bar:
            bar.add_bar("a", "column 'verification_1'", 300)
            bar.update("a", completed=50, success=50, force=True)

            output = tty_stream.getvalue()
            for line in _last_panel_lines(output):
                assert len(line) <= 35


@pytest.mark.parametrize(
    ("terminal_width", "label", "model_alias", "model_name"),
    [
        # Exercise double-width CJK/emoji, combining marks, and ZWJ sequences whose
        # code-point counts differ from their displayed terminal cell widths.
        (30, "列🚀" * 20, "模型" * 20, "提供者/模型" * 20),
        (36, "e\u0301" * 20, "👩🏽‍💻" * 20, "模型/e\u0301" * 20),
        (50, "列🚀" * 20, "模型" * 20, "提供者/模型" * 20),
        (80, "e\u0301" * 20, "👩🏽‍💻" * 20, "模型/e\u0301" * 20),
    ],
)
def test_wide_unicode_labels_stay_within_terminal_width(
    tty_stream: FakeTTY,
    terminal_width: int,
    label: str,
    model_alias: str,
    model_name: str,
) -> None:
    with patch.object(shutil, "get_terminal_size", return_value=os.terminal_size((terminal_width, 24))):
        with TerminalThroughputPanel(stream=tty_stream) as bar:
            bar.add_bar("a", label, 100)
            bar.record_model_usage(
                model_alias=model_alias,
                model_name=model_name,
                input_tokens=100,
                output_tokens=25,
                force=True,
            )

            assert all(wcswidth(line) == terminal_width - 1 for line in _last_panel_lines(tty_stream.getvalue()))


def test_active_panel_clears_visible_artifacts_when_terminal_resizes(tty_stream: FakeTTY) -> None:
    terminal_size = os.terminal_size((80, 24))

    with patch.object(shutil, "get_terminal_size", side_effect=lambda: terminal_size):
        with TerminalThroughputPanel(stream=tty_stream) as bar:
            bar.add_bar("a", "column 'a'", 100)
            snapshot = tty_stream.getvalue()
            terminal_size = os.terminal_size((50, 12))

            bar.update("a", completed=1, success=1, force=True)

            assert bar.is_active is True
            assert ERASE_TO_END in tty_stream.getvalue()[len(snapshot) :]


def test_update_many_single_redraw(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        bar.add_bar("a", "col_a", 100)
        bar.add_bar("b", "col_b", 100)
        before = tty_stream.getvalue()

        bar.update_many({"a": (10, 10, 0, 0), "b": (20, 20, 0, 0)}, force=True)
        after = tty_stream.getvalue()

        new_output = after[len(before) :]
        assert new_output.count(CURSOR_UP_CLEAR) == 22

        clean = _clean(after)
        assert "10/100" in clean
        assert "20/100" in clean


def test_update_many_includes_failures_and_skips(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        bar.add_bar("a", "col_a", 100)
        bar.update_many({"a": (10, 7, 2, 1), "unknown": (5, 5, 0, 0)}, force=True)

        clean = _clean(tty_stream.getvalue())
        assert "10/100" in clean
        assert "2 failed" in clean
        assert "1 skipped" in clean
        assert "unknown" not in clean


def test_remove_bar_redraws_panel(tty_stream: FakeTTY) -> None:
    with TerminalThroughputPanel(stream=tty_stream) as bar:
        bar.add_bar("a", "col_a", 100)
        bar.add_bar("b", "col_b", 100)

        snapshot = tty_stream.getvalue()
        bar.remove_bar("a")

        new_output = tty_stream.getvalue()[len(snapshot) :]
        assert new_output.count(CURSOR_UP_CLEAR) == 22
        panel = "\n".join(_last_panel_lines(tty_stream.getvalue()))
        assert "col_a" not in panel
        assert "col_b" in panel


def test_reporter_initializes_panel_from_resumed_progress() -> None:
    tracker = ProgressTracker(total_records=100, label="column 'a'", quiet=True, initial_completed=90)
    bar = TerminalThroughputPanel(stream=io.StringIO())
    reporter = AsyncProgressReporter({"col_a": tracker}, progress_bar=bar)
    try:
        state = bar._bars["col_a"]  # noqa: SLF001
        assert state.completed == 90
        assert state.last_completed == 90

        state.start_time = 0.0
        state.last_sample_time = 0.0
        state.record_update(completed=91, success=91, failed=0, skipped=0, now=2.0)
        assert state.rates[-1] == 0.5
        assert state.average_rate(2.0) == 0.5
    finally:
        reporter.close()


def test_reporter_sanitizes_column_labels_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    unsafe = "evil\033]52;c;payload\007\ncolumn"
    tracker = ProgressTracker(total_records=1, label=f"column '{unsafe}'", quiet=True)
    reporter = AsyncProgressReporter({unsafe: tracker})

    with caplog.at_level(logging.INFO):
        reporter.log_start(num_row_groups=1)
        reporter._emit()  # noqa: SLF001

    assert caplog.records
    assert all(record.getMessage().isprintable() for record in caplog.records)


def test_reporter_updates_and_logs_keep_drawn_lines_in_sync(tty_stream: FakeTTY) -> None:
    root_logger = logging.getLogger()
    old_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(tty_stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(handler)

    try:
        bar = TerminalThroughputPanel(stream=tty_stream)
        trackers = {
            "col_a": ProgressTracker(total_records=100, label="column 'a'", quiet=True),
            "col_b": ProgressTracker(total_records=100, label="column 'b'", quiet=True),
            "col_c": ProgressTracker(total_records=100, label="column 'c'", quiet=True),
        }

        with bar:
            reporter = AsyncProgressReporter(trackers, report_interval=0.1, progress_bar=bar)
            reporter.log_start(num_row_groups=1)
            panel = "\n".join(_last_panel_lines(tty_stream.getvalue()))
            assert "column 'a'" in panel

            emit_token_usage_event(
                TokenUsageEvent(
                    model_alias="test",
                    model_name="test-model",
                    input_tokens=120,
                    output_tokens=30,
                )
            )
            assert bar._model_usage["test"].input_tokens == 120  # noqa: SLF001
            assert bar._model_usage["test"].output_tokens == 30  # noqa: SLF001

            snapshot = tty_stream.getvalue()
            reporter.record_success("col_a")
            assert tty_stream.getvalue()[len(snapshot) :].count(CURSOR_UP_CLEAR) == 0

            for i in range(49):
                if i % 10 == 0:
                    root_logger.info("Processing batch %d", i)
                reporter.record_success("col_b")
                reporter.record_skipped("col_c")

            snapshot = tty_stream.getvalue()
            reporter.log_final()
            assert bar.drawn_lines == 22
            clear_count = tty_stream.getvalue()[len(snapshot) :].count(CURSOR_UP_CLEAR)
            assert clear_count >= bar.drawn_lines
            assert clear_count % bar.drawn_lines == 0
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(old_level)


def test_reporter_records_feedback_markers_from_request_events(tty_stream: FakeTTY) -> None:
    trackers = {"col_a": ProgressTracker(total_records=100, label="column 'a'", quiet=True)}

    with TerminalThroughputPanel(stream=tty_stream) as bar:
        reporter = AsyncProgressReporter(trackers, report_interval=0.1, progress_bar=bar)
        try:
            emit_request_admission_event(
                RequestAdmissionEvent.capture("request_rate_limited", sequence=1),
            )
            assert len(bar._feedback_markers) == 1  # noqa: SLF001

            emit_request_admission_event(
                RequestAdmissionEvent.capture("request_wait_started", sequence=2),
            )
            assert len(bar._feedback_markers) == 1  # noqa: SLF001
        finally:
            reporter.close()

        emit_request_admission_event(
            RequestAdmissionEvent.capture("request_rate_limited", sequence=3),
        )
        assert len(bar._feedback_markers) == 1  # noqa: SLF001


def test_inactive_reporter_does_not_subscribe_to_global_events() -> None:
    stream = io.StringIO()
    trackers = {"col_a": ProgressTracker(total_records=100, label="column 'a'", quiet=True)}
    reporter = AsyncProgressReporter(trackers, report_interval=0.1, progress_bar=TerminalThroughputPanel(stream=stream))
    try:
        emit_token_usage_event(
            TokenUsageEvent(
                model_alias="inactive",
                model_name="inactive-model",
                input_tokens=120,
                output_tokens=30,
            )
        )
        emit_request_admission_event(RequestAdmissionEvent.capture("request_rate_limited", sequence=1))

        assert reporter._bar is not None  # noqa: SLF001
        assert not reporter._bar._model_usage  # noqa: SLF001
        assert not reporter._bar._feedback_markers  # noqa: SLF001
    finally:
        reporter.close()


def test_reporter_filters_global_events_by_run_id(tty_stream: FakeTTY) -> None:
    trackers = {"col_a": ProgressTracker(total_records=100, label="column 'a'", quiet=True)}

    with TerminalThroughputPanel(stream=tty_stream) as bar:
        reporter = AsyncProgressReporter(trackers, report_interval=0.1, progress_bar=bar, run_id="run-a")
        try:
            emit_token_usage_event(
                TokenUsageEvent(
                    model_alias="other",
                    model_name="other-model",
                    input_tokens=100,
                    output_tokens=10,
                    correlation=_correlation("run-b"),
                )
            )
            emit_token_usage_event(
                TokenUsageEvent(
                    model_alias="uncorrelated",
                    model_name="uncorrelated-model",
                    input_tokens=100,
                    output_tokens=10,
                )
            )
            assert not bar._model_usage  # noqa: SLF001

            emit_token_usage_event(
                TokenUsageEvent(
                    model_alias="owned",
                    model_name="owned-model",
                    input_tokens=120,
                    output_tokens=30,
                    correlation=_correlation("run-a"),
                )
            )
            assert set(bar._model_usage) == {"owned"}  # noqa: SLF001

            emit_request_admission_event(
                RequestAdmissionEvent.capture("request_rate_limited", sequence=1, correlation=_correlation("run-b"))
            )
            emit_request_admission_event(RequestAdmissionEvent.capture("request_rate_limited", sequence=2))
            assert not bar._feedback_markers  # noqa: SLF001

            emit_request_admission_event(
                RequestAdmissionEvent.capture("request_rate_limited", sequence=3, correlation=_correlation("run-a"))
            )
            assert len(bar._feedback_markers) == 1  # noqa: SLF001
        finally:
            reporter.close()


_REQUEST_WAIT_LINE_RE = re.compile(
    r"(column '[^']*'): models=(\S+), request_wait_wall_time_s=([\d.]+), idle_time_s=([\d.]+), "
    r"idle_pct_of_run=([\d.]+)%, requests=(\d+)"
)


def _wait_correlation(column: str = "prompt", run_id: str = "run-a") -> RuntimeCorrelation:
    return RuntimeCorrelation(
        run_id=run_id,
        row_group=0,
        task_column=column,
        task_type="cell",
        scheduling_group_kind="model",
        scheduling_group_identity_hash="hash",
        task_execution_id="task-exec",
    )


def _lease_event(
    kind: str,
    at: float,
    lease: str,
    correlation: RuntimeCorrelation | None,
    model_id: str = "nemotron",
) -> RequestAdmissionEvent:
    # Built directly because capture() stamps "now"; __post_init__ turns the correlation into a dict as in production.
    return RequestAdmissionEvent(
        event_kind=kind,
        captured_at_monotonic=at,
        sequence=0,
        captured_correlation=correlation,
        request_lease_id=lease,
        request_resource_key={"provider_name": "nvidia", "model_id": model_id, "domain": "chat"},
    )


def _wait_lines(caplog: pytest.LogCaptureFixture) -> dict[str, tuple[str, float, float, float, int]]:
    lines = {}
    for record in caplog.records:
        if match := _REQUEST_WAIT_LINE_RE.search(record.getMessage()):
            label, models, wait, idle, pct, requests = match.groups()
            lines[label] = (models, float(wait), float(idle), float(pct), int(requests))
    return lines


def _assert_wait_lines(
    caplog: pytest.LogCaptureFixture, expected: dict[str, tuple[str, float, float, float, int]]
) -> None:
    # Wait comes from event timestamps and is exact; idle and pct also depend on when log_final runs.
    got = _wait_lines(caplog)
    assert got.keys() == expected.keys()
    for label, (models, wait, idle, pct, requests) in expected.items():
        assert got[label] == (models, wait, pytest.approx(idle, abs=0.5), pytest.approx(pct, abs=2.5), requests)


def _wait_reporter() -> AsyncProgressReporter:
    trackers = {
        column: ProgressTracker(total_records=1, label=f"column '{column}'", quiet=True)
        for column in ("prompt", "label", "expr")
    }
    return AsyncProgressReporter(trackers, run_id="run-a")


# (lease, column, model, acquired_s, released_s) relative to a 20 s run -> {label: (models, wait, idle, pct, requests)}
_WAIT_CASES = {
    "overlap": (
        [("a", "prompt", "nemotron", 0, 10), ("b", "prompt", "nemotron", 5, 15)],
        {"column 'prompt'": ("nemotron", 15.0, 5.0, 25.0, 2)},
    ),
    "disjoint": (
        [("a", "prompt", "nemotron", 0, 4), ("b", "prompt", "nemotron", 6, 8)],
        {"column 'prompt'": ("nemotron", 6.0, 14.0, 70.0, 2)},
    ),
    "nested": (
        [("a", "prompt", "nemotron", 0, 10), ("b", "prompt", "nemotron", 2, 3)],
        {"column 'prompt'": ("nemotron", 10.0, 10.0, 50.0, 2)},
    ),
    "two-columns": (
        [
            ("a", "prompt", "nemotron", 0, 10),
            ("b", "label", "gpt-4.1-mini", 2, 4),
            ("c", "label", "gpt-4.1-mini", 3, 5),
        ],
        {
            "column 'prompt'": ("nemotron", 10.0, 10.0, 50.0, 1),
            "column 'label'": ("gpt-4.1-mini", 3.0, 17.0, 85.0, 2),
        },
    ),
    "before-start-clipped": (
        [("a", "prompt", "nemotron", -5, 2)],
        {"column 'prompt'": ("nemotron", 2.0, 18.0, 90.0, 1)},
    ),
    # A column whose first request starts late still measures idle against the whole run, so the time
    # before it started counts as idle. The denominator is the run, not the column's own window.
    "late-start": (
        [("a", "prompt", "nemotron", 12, 17)],
        {"column 'prompt'": ("nemotron", 5.0, 15.0, 75.0, 1)},
    ),
}


@pytest.mark.parametrize("delivery", ["in-order", "release-before-acquire"])
@pytest.mark.parametrize("case", list(_WAIT_CASES), ids=list(_WAIT_CASES))
def test_reporter_logs_per_column_request_wait_as_union_of_leases(
    caplog: pytest.LogCaptureFixture, case: str, delivery: str
) -> None:
    """Overlapping requests count once (union, not sum); events stamped in order can be delivered out of order."""
    leases, expected = _WAIT_CASES[case]
    reporter = _wait_reporter()
    base = time.monotonic() - 20.0
    reporter._start_monotonic = base  # noqa: SLF001
    events = []
    for lease, column, model_id, acquired_s, released_s in leases:
        correlation = _wait_correlation(column)
        events.append(_lease_event("request_lease_acquired", base + acquired_s, lease, correlation, model_id))
        events.append(_lease_event("request_lease_released", base + released_s, lease, correlation, model_id))
    if delivery == "release-before-acquire":
        events.reverse()

    with caplog.at_level(logging.INFO):
        reporter.log_start(num_row_groups=1)
        for event in events:
            emit_request_admission_event(event)
        reporter.log_final()

    _assert_wait_lines(caplog, expected)
    header = next(r.getMessage() for r in caplog.records if "Model request wait per column" in r.getMessage())
    run_wall_time = re.search(r"idle = run wall time ([\d.]+)s minus time with >=1 request in flight", header)
    assert run_wall_time is not None
    assert float(run_wall_time.group(1)) == pytest.approx(20.0, abs=0.5)


def test_reporter_counts_paired_wait_completed_and_lease_acquired_once(caplog: pytest.LogCaptureFixture) -> None:
    """The controller emits request_wait_completed next to request_lease_acquired; only the lease is a request."""
    reporter = _wait_reporter()
    base = time.monotonic() - 20.0
    reporter._start_monotonic = base  # noqa: SLF001
    with caplog.at_level(logging.INFO):
        reporter.log_start(num_row_groups=1)
        for kind, at in [("request_wait_completed", 0), ("request_lease_acquired", 0), ("request_lease_released", 10)]:
            emit_request_admission_event(_lease_event(kind, base + at, "a", _wait_correlation()))
        reporter.log_final()

    _assert_wait_lines(caplog, {"column 'prompt'": ("nemotron", 10.0, 10.0, 50.0, 1)})


@pytest.mark.parametrize("scenario", ["no-requests", "foreign-or-unpaired-events"])
def test_reporter_omits_request_wait_block_without_owned_leases(
    caplog: pytest.LogCaptureFixture, scenario: str
) -> None:
    """Columns that made no model request (and other runs' or uncorrelated events) get no idle line."""
    reporter = _wait_reporter()
    base = time.monotonic() - 20.0
    reporter._start_monotonic = base  # noqa: SLF001
    events = []
    if scenario == "foreign-or-unpaired-events":
        other_run = _wait_correlation(run_id="run-b")
        events = [
            _lease_event("request_lease_acquired", base, "x", other_run),
            _lease_event("request_lease_released", base + 10, "x", other_run),
            _lease_event("request_lease_acquired", base, "y", None),
            _lease_event("request_wait_completed", base, "z", _wait_correlation()),
            _lease_event("request_lease_released", base + 1, "orphan", _wait_correlation()),
        ]
    with caplog.at_level(logging.INFO):
        reporter.log_start(num_row_groups=1)
        for event in events:
            emit_request_admission_event(event)
        reporter.log_final()

    assert not any("Model request wait per column" in record.getMessage() for record in caplog.records)


def test_reporter_counts_lease_open_at_end_and_unsubscribes_on_close(caplog: pytest.LogCaptureFixture) -> None:
    """A lease still held when the run ends counts up to the end; events after close() are ignored."""
    reporter = _wait_reporter()
    reporter._start_monotonic = time.monotonic() - 20.0  # noqa: SLF001
    with caplog.at_level(logging.INFO):
        reporter.log_start(num_row_groups=1)
        reporter.log_start(num_row_groups=1)
        emit_request_admission_event(
            _lease_event("request_lease_acquired", time.monotonic() - 3.0, "a", _wait_correlation())
        )
        reporter.log_final()
        emit_request_admission_event(_lease_event("request_lease_acquired", time.monotonic(), "b", _wait_correlation()))

    ((models, wait, _idle, _pct, requests),) = _wait_lines(caplog).values()
    assert models == "nemotron"
    assert requests == 1
    assert 2.5 < wait < 4.0


def test_reporter_sanitizes_column_labels_in_request_wait_block(caplog: pytest.LogCaptureFixture) -> None:
    """Column names are user input; the new block must not write terminal control sequences to the log."""
    unsafe = "evil\033]52;c;payload\007\ncolumn"
    reporter = AsyncProgressReporter({unsafe: ProgressTracker(total_records=1, label=f"column '{unsafe}'", quiet=True)})
    with caplog.at_level(logging.INFO):
        reporter.log_start(num_row_groups=1)
        now = time.monotonic()
        emit_request_admission_event(_lease_event("request_lease_acquired", now, "a", _wait_correlation(unsafe)))
        emit_request_admission_event(_lease_event("request_lease_released", now, "a", _wait_correlation(unsafe)))
        reporter.log_final()

    assert any("request_wait_wall_time_s" in record.getMessage() for record in caplog.records)
    assert all(record.getMessage().isprintable() for record in caplog.records)


class _SlowChatClient:
    provider_name = "nvidia"

    def __init__(self, delay: float, failures: int = 0, barrier: threading.Barrier | None = None) -> None:
        self.delay = delay
        self.failures = failures
        self.barrier = barrier
        self.calls = 0
        self.durations: list[float] = []

    def supports_chat_completion(self) -> bool:
        return True

    def _finish(self, started: float) -> ChatCompletionResponse:
        self.durations.append(time.monotonic() - started)
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderError(kind=ProviderErrorKind.INTERNAL_SERVER, message="unavailable", status_code=503)
        return ChatCompletionResponse(AssistantMessage(content="ok"))

    def completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        started = time.monotonic()
        if self.barrier is not None:
            self.barrier.wait()  # all calls are in flight together, however the threads get scheduled
        time.sleep(self.delay)
        return self._finish(started)

    async def acompletion(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        started = time.monotonic()
        await asyncio.sleep(self.delay)
        return self._finish(started)


def _in_run(fn: Callable[[], object]) -> object:
    token = runtime_correlation_provider.set(_wait_correlation())
    try:
        return fn()
    finally:
        runtime_correlation_provider.reset(token)


@pytest.mark.parametrize(
    "mode,expected_requests,retries,backoff",
    [
        pytest.param("async-gather", 3, 0, 0.0, id="async-gather"),
        pytest.param("sync-threads", 3, 0, 0.0, id="sync-threads"),
        pytest.param("async-retry", 2, 1, 0.0, id="async-retry"),
        pytest.param("retry-backoff-idle", 2, 1, 0.5, id="retry-backoff-idle"),
    ],
)
def test_reporter_request_wait_through_real_admission_controller(
    caplog: pytest.LogCaptureFixture, mode: str, expected_requests: int, retries: int, backoff: float
) -> None:
    """Lease events come from the real controller and executor; the controller always emits them on the global bus."""
    concurrent = mode in ("async-gather", "sync-threads")
    client = _SlowChatClient(
        0.3,
        failures=0 if concurrent else 1,
        barrier=threading.Barrier(3, timeout=5.0) if mode == "sync-threads" else None,
    )
    controller = AdaptiveRequestAdmissionController()
    controller.register(provider_name="nvidia", model_id="nemotron", alias="default", max_parallel_requests=4)
    executor = ModelRequestExecutor(
        client, controller, "nvidia", "nemotron", retry_config=RetryConfig(max_retries=retries, backoff_factor=backoff)
    )
    request = ChatCompletionRequest(model="nemotron", messages=[])
    reporter = _wait_reporter()

    async def gather() -> None:
        await asyncio.gather(*(executor.acompletion(request) for _ in range(3)))

    with caplog.at_level(logging.INFO):
        reporter.log_start(num_row_groups=1)
        if mode == "async-gather":
            _in_run(lambda: asyncio.run(gather()))
        elif mode == "sync-threads":
            threads = [threading.Thread(target=lambda: _in_run(lambda: executor.completion(request))) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        else:
            _in_run(lambda: asyncio.run(executor.acompletion(request)))
        reporter.log_final()

    ((models, wait, idle, _pct, requests),) = _wait_lines(caplog).values()
    summed = sum(client.durations)
    assert models == "nemotron"
    assert requests == expected_requests
    # Bounds are relative to the measured call time so load on the test machine cannot flip them;
    # the logged value is rounded to 0.1 s.
    if concurrent:
        # Three calls share their last 0.3 s, so the union is at least 0.6 s below the sum.
        assert 0.25 < wait < summed - 0.4
    else:
        # Attempts run one after another, so the union equals the sum; a counted backoff would add 0.5 s.
        assert summed - 0.1 < wait < summed + 0.25
    if backoff:
        assert idle >= 0.4
    # Every acquire this run emitted reached the reporter, so nothing is left half-paired: the pairing
    # state is empty once the run ends, including across retries and concurrent threads.
    assert reporter._open_leases == {}  # noqa: SLF001
    assert reporter._early_releases == {}  # noqa: SLF001
