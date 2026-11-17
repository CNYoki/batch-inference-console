"""普惠队列模式的领取顺序，以及按运行段累加的耗时。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Job
from app.services.fair_queue import ModelShare, fair_quotas, user_order


def _quotas(limit: int, demand: dict[str, int], running: dict[str, int] | None = None,
            last_stop: dict[str, float] | None = None) -> dict[str, int]:
    """用户按 dict 的先后当作到达先后（A 最早）。"""
    arrival = {u: float(i) for i, u in enumerate(demand)}
    order = user_order(arrival, running or {}, last_stop or {})
    return fair_quotas(limit, order, demand)


def test_slots_split_evenly_between_users():
    assert _quotas(4, {"A": 10, "B": 3}) == {"A": 2, "B": 2}


def test_user_with_fewer_jobs_leaves_slots_to_others():
    assert _quotas(4, {"A": 10, "B": 1}) == {"A": 3, "B": 1}


def test_four_users_get_one_each_however_many_jobs():
    # A 先排 10 个，B、C、D 后来，D 有 100 个：仍是各 1 个
    assert _quotas(4, {"A": 10, "B": 2, "C": 5, "D": 100}, running={"A": 4}) == \
        {"A": 1, "B": 1, "C": 1, "D": 1}


def test_remainder_goes_to_earliest_user():
    assert _quotas(4, {"A": 10, "B": 10, "C": 10}) == {"A": 2, "B": 1, "C": 1}


def test_more_users_than_slots_wait_in_order():
    running = {"A": 1, "B": 1, "C": 1, "D": 1}
    assert _quotas(4, {u: 5 for u in "ABCDE"}, running=running) == \
        {"A": 1, "B": 1, "C": 1, "D": 1, "E": 0}


def test_freed_slot_goes_to_whoever_waited_longest():
    # A 的任务刚跑完（还有任务排着），E 一直在等：名额给 E
    running = {"B": 1, "C": 1, "D": 1}
    q = _quotas(4, {u: 5 for u in "ABCDE"}, running=running, last_stop={"A": 100.0})
    assert q == {"A": 0, "B": 1, "C": 1, "D": 1, "E": 1}


def test_excess_keeps_earliest_jobs_of_each_user():
    share = ModelShare(limit=4, quotas={"A": 1, "B": 1}, running={"A": ["a1", "a2", "a3"], "B": ["b1"]})
    assert share.excess() == ["a2", "a3"]
    assert not share.can_claim("A") and not share.can_claim("B")
    assert ModelShare(limit=4, quotas={"C": 1}).can_claim("C")


@pytest.mark.parametrize("naive", [False, True])
def test_close_run_segment_accumulates(naive: bool):
    start = datetime.now(UTC) - timedelta(seconds=90)
    job = Job(run_seconds=30, run_started_at=start.replace(tzinfo=None) if naive else start)
    job.close_run_segment()
    assert 119 <= job.run_seconds <= 121
    assert job.run_started_at is None
    job.close_run_segment()  # 不在运行时什么也不做
    assert 119 <= job.run_seconds <= 121
