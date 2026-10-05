from __future__ import annotations

import logging

import pytest

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector.ring import RingBuffer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def samples(n: int) -> list[pb.Sample]:
    return [pb.Sample(tag_id=i, ts_unix_ns=1, num=float(i), quality=pb.GOOD) for i in range(n)]


def test_append_assigns_increasing_seq_and_ack_is_cumulative() -> None:
    ring = RingBuffer(60.0, Clock())
    assert [ring.append(4, samples(1)).seq for _ in range(4)] == [1, 2, 3, 4]
    ring.ack(2)
    assert [b.seq for b in ring.pending()] == [3, 4]
    ring.ack(1)  # 이미 해제된 seq
    ring.ack(10)  # 보낸 적 없는 seq까지 Ack해도 남은 것만 비운다
    assert len(ring) == 0
    assert ring.oldest_unacked_seq == 0


def test_pending_returns_replay_copies_without_touching_originals() -> None:
    ring = RingBuffer(60.0, Clock())
    live = ring.append(9, samples(2))
    (replay,) = ring.pending()
    assert (replay.seq, replay.plan_rev, replay.replay, len(replay.samples)) == (1, 9, True, 2)
    assert live.replay is False


def test_batches_older_than_window_are_dropped_and_counted() -> None:
    clock = Clock()
    ring = RingBuffer(60.0, clock)
    ring.append(1, samples(3))  # t=0
    clock.now += 30
    ring.append(1, samples(2))  # t=30
    clock.now += 31  # t=61: 첫 batch만 창(60s) 밖
    ring.append(1, samples(1))
    assert [b.seq for b in ring.pending()] == [2, 3]
    assert (ring.dropped_batches, ring.dropped_samples) == (1, 3)
    clock.now += 100  # 모두 창 밖
    assert ring.pending() == []
    assert (ring.dropped_batches, ring.dropped_samples) == (3, 6)


def test_drop_warning_is_logged_once_per_outage(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    ring = RingBuffer(1.0, clock)
    with caplog.at_level(logging.WARNING, logger="pureiron_collector.ring"):
        for _ in range(5):
            ring.append(1, samples(2))
            clock.now += 2  # 매번 이전 batch가 창 밖
        ring.ack(ring.oldest_unacked_seq)  # core 복구
        ring.ack(ring.oldest_unacked_seq)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "dropping oldest batches (from seq 1)" in messages[0]
    assert "8 sample(s) were dropped" in messages[1]
