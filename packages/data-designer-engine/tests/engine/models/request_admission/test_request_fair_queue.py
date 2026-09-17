# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections import Counter

from data_designer.engine.models.request_admission.queue import RequestFairQueue, RequestWaiter
from data_designer.engine.models.request_admission.resources import (
    RequestAdmissionItem,
    RequestDomain,
    RequestGroupSpec,
    RequestResourceKey,
)


def _group(model_id: str, *, weight: float = 1.0) -> RequestGroupSpec:
    return RequestGroupSpec(RequestResourceKey("nvidia", model_id, RequestDomain.CHAT), weight=weight)


def _waiter(waiter_id: str, group: RequestGroupSpec) -> RequestWaiter:
    return RequestWaiter(
        waiter_id=waiter_id,
        item=RequestAdmissionItem(resource=group.key, group=group),
        enqueued_at=0.0,
    )


def _select_and_commit(queue: RequestFairQueue) -> RequestWaiter | None:
    selection = queue.select_next(lambda _waiter, _view: True)
    if selection is None:
        return None
    return queue.commit(selection)


def _retained_ordering_entries(queue: RequestFairQueue) -> int:
    """Every ordering tuple the queue still holds, whichever container it keeps them in."""
    return len(queue._active_heap_entries) + len(getattr(queue, "_heap", ()))


def test_request_fair_queue_equal_groups_round_robins() -> None:
    queue = RequestFairQueue()
    for row_index in range(2):
        for model_id in ("a", "b", "c"):
            assert queue.enqueue(_waiter(f"{model_id}-{row_index}", _group(model_id)))

    selected = [_select_and_commit(queue) for _ in range(6)]

    assert [waiter.item.group.key.model_id for waiter in selected if waiter is not None] == [
        "a",
        "b",
        "c",
        "a",
        "b",
        "c",
    ]


def test_request_fair_queue_weighted_groups() -> None:
    queue = RequestFairQueue()
    heavy = _group("a", weight=2)
    light = _group("b", weight=1)
    for row_index in range(6):
        assert queue.enqueue(_waiter(f"a-{row_index}", heavy))
        assert queue.enqueue(_waiter(f"b-{row_index}", light))

    selected = [_select_and_commit(queue) for _ in range(6)]
    counts = Counter(waiter.item.group.key.model_id for waiter in selected if waiter is not None)

    assert counts == {"a": 4, "b": 2}


def test_request_fair_queue_ordering_state_stays_bounded_by_active_groups() -> None:
    queue = RequestFairQueue()
    group = _group("a")

    for index in range(1_000):
        assert queue.enqueue(_waiter(str(index), group))
        assert _select_and_commit(queue) is not None
        assert _retained_ordering_entries(queue) == 0

    assert not queue.has_waiters
    assert queue.view().queued_total == 0
