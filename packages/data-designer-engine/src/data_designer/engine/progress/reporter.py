# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import threading
import time
from array import array
from collections.abc import Callable

from data_designer.engine.models.usage_events import TokenUsageEvent, subscribe_token_usage
from data_designer.engine.observability import RequestAdmissionEvent, subscribe_request_admission_events
from data_designer.engine.progress.terminal.throughput_panel import TerminalThroughputPanel, sanitize_terminal_text
from data_designer.engine.progress.tracker import ProgressTracker
from data_designer.logging import LOG_INDENT

logger = logging.getLogger(__name__)

DEFAULT_REPORT_INTERVAL = 5.0
DEFAULT_TTY_REPORT_INTERVAL = 0.75
FEEDBACK_MARKER_EVENTS = frozenset({"request_rate_limited", "request_limit_decreased"})
REQUEST_LEASE_EVENTS = frozenset({"request_lease_acquired", "request_lease_released"})


class AsyncProgressReporter:
    """Consolidated progress reporter for async generation.

    Owns per-column ProgressTracker instances (in quiet mode) and emits
    a single grouped log block at most once per ``report_interval`` seconds.
    """

    def __init__(
        self,
        trackers: dict[str, ProgressTracker],
        *,
        report_interval: float = DEFAULT_REPORT_INTERVAL,
        progress_bar: TerminalThroughputPanel | None = None,
        run_id: str | None = None,
    ) -> None:
        self._trackers = trackers
        self._report_interval = report_interval
        self._run_id = run_id
        self._start_time = time.perf_counter()
        self._start_monotonic = time.monotonic()
        self._last_report_time: float = self._start_time
        self._last_bar_report_time: float = self._start_time
        self._last_reported_total: int = -1
        self._bar = progress_bar
        self._unsubscribe_token_usage: Callable[[], None] | None = None
        self._unsubscribe_request_admission_events: Callable[[], None] | None = None
        self._unsubscribe_request_wait: Callable[[], None] | None = None
        self._wait_lock = threading.Lock()
        self._open_leases: dict[str, tuple[str, float]] = {}
        self._early_releases: dict[str, float] = {}
        self._wait_intervals: dict[str, array[float]] = {}
        self._column_requests: dict[str, int] = {}
        self._column_models: dict[str, set[str]] = {}
        if self._bar is not None:
            for col, tracker in trackers.items():
                self._bar.add_bar(
                    col,
                    tracker.label,
                    tracker.total_records,
                    initial_completed=tracker.completed,
                )
            self._ensure_event_subscriptions()

    def log_start(self, num_row_groups: int, scheduled_records: int | None = None) -> None:
        self._ensure_event_subscriptions()
        # Unlike the panel subscriptions this one runs without a TTY too; _matches_run keeps it to this run
        # (the scheduler always passes a run id).
        if self._unsubscribe_request_wait is None:
            self._unsubscribe_request_wait = subscribe_request_admission_events(self._record_request_wait)
        cols = ", ".join(sanitize_terminal_text(tracker.label) for tracker in self._trackers.values())
        total = (
            sum(max(0, tracker.total_records - tracker.completed) for tracker in self._trackers.values())
            if scheduled_records is None
            else scheduled_records * len(self._trackers)
        )
        logger.info(
            "⚡️ Async generation: %d column(s) (%s), %d tasks across %d row group(s)",
            len(self._trackers),
            cols,
            total,
            num_row_groups,
        )

    def record_success(self, column: str) -> None:
        if tracker := self._trackers.get(column):
            tracker.record_success()
            self._maybe_report()

    def record_failure(self, column: str) -> None:
        if tracker := self._trackers.get(column):
            tracker.record_failure()
            self._maybe_report()

    def record_skipped(self, column: str) -> None:
        if tracker := self._trackers.get(column):
            tracker.record_skipped()
            self._maybe_report()

    def log_final(self) -> None:
        try:
            if self._bar is not None and self._bar.is_active:
                self._update_bar(force=True)
            else:
                self._emit()
            elapsed = time.perf_counter() - self._start_time
            snapshots = [tracker.get_snapshot(elapsed) for tracker in self._trackers.values()]
            total_ok = sum(snapshot[2] for snapshot in snapshots)
            total_fail = sum(snapshot[3] for snapshot in snapshots)
            total_skipped = sum(snapshot[4] for snapshot in snapshots)
            skipped_suffix = f", {total_skipped} skipped" if total_skipped else ""
            logger.info(
                "✅ Async generation complete [%.1fs]: %d ok, %d failed%s across %d column(s)",
                elapsed,
                total_ok,
                total_fail,
                skipped_suffix,
                len(self._trackers),
            )
            self._log_request_wait()
        finally:
            self.close()

    def close(self) -> None:
        if self._unsubscribe_token_usage is not None:
            self._unsubscribe_token_usage()
            self._unsubscribe_token_usage = None
        if self._unsubscribe_request_admission_events is not None:
            self._unsubscribe_request_admission_events()
            self._unsubscribe_request_admission_events = None
        if self._unsubscribe_request_wait is not None:
            self._unsubscribe_request_wait()
            self._unsubscribe_request_wait = None

    def _maybe_report(self) -> None:
        now = time.perf_counter()
        if self._bar is not None and self._bar.is_active:
            self._ensure_event_subscriptions()
            if now - self._last_bar_report_time < DEFAULT_TTY_REPORT_INTERVAL:
                return
            self._last_bar_report_time = now
            self._update_bar()
            return
        if now - self._last_report_time < self._report_interval:
            return
        self._last_report_time = now
        self._emit()

    def _update_bar(self, *, force: bool = False) -> None:
        elapsed = time.perf_counter() - self._start_time
        updates: dict[str, tuple[int, int, int, int]] = {}
        for col, tracker in self._trackers.items():
            completed, _total, success, failed, skipped, _pct, _rate, _emoji = tracker.get_snapshot(elapsed)
            updates[col] = (completed, success, failed, skipped)
        self._bar.update_many(updates, force=force)

    def _ensure_event_subscriptions(self) -> None:
        if self._bar is None or not self._bar.is_active:
            return
        if self._unsubscribe_token_usage is None:
            self._unsubscribe_token_usage = subscribe_token_usage(self._record_token_usage)
        if self._unsubscribe_request_admission_events is None:
            self._unsubscribe_request_admission_events = subscribe_request_admission_events(
                self._record_request_admission_event
            )

    def _record_token_usage(self, event: TokenUsageEvent) -> None:
        if self._bar is not None and self._matches_run(event.correlation):
            self._bar.record_model_usage(
                model_alias=event.model_alias,
                model_name=event.model_name,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
            )

    def _record_request_admission_event(self, event: RequestAdmissionEvent) -> None:
        if (
            self._bar is not None
            and event.event_kind in FEEDBACK_MARKER_EVENTS
            and self._matches_run(event.captured_correlation)
        ):
            self._bar.record_feedback_signal()

    def _record_request_wait(self, event: RequestAdmissionEvent) -> None:
        correlation = event.captured_correlation
        lease_id = event.request_lease_id
        if event.event_kind not in REQUEST_LEASE_EVENTS or lease_id is None or not isinstance(correlation, dict):
            return
        column = correlation.get("task_column")
        if not isinstance(column, str) or not self._matches_run(correlation):
            return
        at = event.captured_at_monotonic
        # Events are stamped under the controller lock but delivered after it is released, so threads can
        # deliver them out of order. Keep raw intervals (16 bytes per request) and merge them in log_final.
        with self._wait_lock:
            if event.event_kind == "request_lease_acquired":
                self._column_requests[column] = self._column_requests.get(column, 0) + 1
                resource = event.request_resource_key
                if isinstance(resource, dict) and resource.get("model_id"):
                    self._column_models.setdefault(column, set()).add(str(resource["model_id"]))
                released_at = self._early_releases.pop(lease_id, None)
                if released_at is None:
                    self._open_leases[lease_id] = (column, at)
                else:
                    self._wait_intervals.setdefault(column, array("d")).extend((at, released_at))
            else:
                opened = self._open_leases.pop(lease_id, None)
                if opened is None:
                    self._early_releases[lease_id] = at
                else:
                    self._wait_intervals.setdefault(opened[0], array("d")).extend((opened[1], at))

    def _log_request_wait(self) -> None:
        end = time.monotonic()
        run_s = end - self._start_monotonic
        with self._wait_lock:
            requests = dict(self._column_requests)
            models = {column: sorted(names) for column, names in self._column_models.items()}
            intervals = {column: list(zip(flat[::2], flat[1::2])) for column, flat in self._wait_intervals.items()}
            for column, acquired_at in self._open_leases.values():
                intervals.setdefault(column, []).append((acquired_at, end))
        if not requests:
            return
        logger.info(
            "⏱️ Model request wait per column (idle = run wall time %.1fs minus time with >=1 request in flight):",
            run_s,
        )
        for column in sorted(requests):
            wait = _union_seconds(intervals.get(column, []), self._start_monotonic, end)
            idle = max(0.0, run_s - wait)
            tracker = self._trackers.get(column)
            label = tracker.label if tracker is not None else f"column '{column}'"
            logger.info(
                "%s%s: models=%s, request_wait_wall_time_s=%.1f, idle_time_s=%.1f, idle_pct_of_run=%.1f%%, requests=%d",
                LOG_INDENT,
                sanitize_terminal_text(label),
                sanitize_terminal_text(",".join(models.get(column, [])) or "-"),
                wait,
                idle,
                100.0 * idle / run_s if run_s > 0 else 0.0,
                requests[column],
            )

    def _matches_run(self, correlation: object) -> bool:
        if self._run_id is None:
            return True
        if correlation is None:
            return False
        if isinstance(correlation, dict):
            return correlation.get("run_id") == self._run_id
        return getattr(correlation, "run_id", None) == self._run_id

    def _emit(self) -> None:
        current_total = sum(tracker.get_snapshot(0.0)[0] for tracker in self._trackers.values())
        if current_total == self._last_reported_total:
            return
        self._last_reported_total = current_total

        elapsed = time.perf_counter() - self._start_time
        logger.info("📊 Progress [%.1fs]:", elapsed)
        for tracker in self._trackers.values():
            completed, total_records, _success, _failed, skipped, pct, rate, emoji = tracker.get_snapshot(elapsed)
            skipped_suffix = f", {skipped} skipped" if skipped else ""
            logger.info(
                "%s%s %s: %d/%d (%.0f%%) %.1f rec/s%s",
                LOG_INDENT,
                emoji,
                sanitize_terminal_text(tracker.label),
                completed,
                total_records,
                pct,
                rate,
                skipped_suffix,
            )


def _union_seconds(intervals: list[tuple[float, float]], lo: float, hi: float) -> float:
    """Length of the union of ``intervals`` clipped to ``[lo, hi]``."""
    total = 0.0
    span_start = span_end = lo
    for start, end in sorted(intervals):
        start, end = max(start, lo), min(end, hi)
        if end <= start:
            continue
        if start > span_end:
            total += span_end - span_start
            span_start = start
        span_end = max(span_end, end)
    return total + span_end - span_start
