"""`LoginThrottle`(plan M3c D16): 창 안 실패 수 → 제한·남은 시간, 창 밖 실패는 빠짐, 동시 시도도 셈,
결과 모름은 실패가 아님, 추적 상한(차단 항목은 축출하지 않고, 버릴 것이 없으면 거부)."""

from __future__ import annotations

from dataclasses import dataclass

from pureiron_core.auth.throttle import LoginThrottle


@dataclass
class Monotonic:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def _fail(throttle: LoginThrottle, username: str = "alice") -> bool:
    assert throttle.begin(username) is None
    return throttle.failed(username)


def test_fifth_failure_in_window_blocks_until_block_ends() -> None:
    clock = Monotonic()
    throttle = LoginThrottle(clock=clock)
    assert [_fail(throttle) for _ in range(5)] == [False, False, False, False, True]
    clock.now = 100.0
    assert throttle.begin("alice") == 500.0
    assert throttle.begin("bob") is None
    clock.now = 600.0
    assert throttle.begin("alice") is None  # 제한이 끝나면 처음부터 다시 센다


def test_failures_older_than_window_do_not_count() -> None:
    clock = Monotonic()
    throttle = LoginThrottle(clock=clock)
    for at in (0.0, 1.0, 2.0, 3.0):
        clock.now = at
        _fail(throttle)
    clock.now = 600.0  # 0.0의 실패는 창(600s) 밖이다
    assert _fail(throttle) is False
    assert throttle.begin("alice") is None


def test_pending_attempts_count_against_the_limit() -> None:
    """결과를 기다리는 동시 시도 다섯 개가 있으면 여섯 번째는 바로 거부한다. 곧 끝날 시도를
    기다리는 것이라 Retry-After는 차단 시간이 아니라 짧은 고정값이다."""
    throttle = LoginThrottle(clock=Monotonic())
    assert [throttle.begin("alice") for _ in range(5)] == [None] * 5
    assert throttle.begin("alice") == 5.0
    throttle.succeeded("alice")
    assert throttle.begin("alice") is None


def test_abandoned_and_unreported_attempts_are_not_failures() -> None:
    clock = Monotonic()
    throttle = LoginThrottle(clock=clock)
    for _ in range(5):
        assert throttle.begin("alice") is None
        throttle.abandoned("alice")  # site-db 장애
    assert throttle.begin("alice") is None
    for _ in range(3):
        throttle.begin("alice")  # 결과를 끝내 알리지 않은 시도(연결 끊김)
    clock.now = 600.0
    assert [_fail(throttle) for _ in range(4)] == [False] * 4


def test_tracked_usernames_are_bounded() -> None:
    throttle = LoginThrottle(max_tracked=2, clock=Monotonic())
    for name in ("a", "b"):
        for _ in range(4):
            _fail(throttle, name)
    _fail(throttle, "c")  # 가득 차 가장 먼저 추적한 "a"를 버린다
    assert _fail(throttle, "b") is True
    assert _fail(throttle, "a") is False  # 처음부터 다시 센다


def test_blocked_entries_are_never_evicted() -> None:
    """다른 이름을 뿌려 상한을 채워도 차단이 풀리지 않는다(리뷰 M-1)."""
    throttle = LoginThrottle(max_tracked=2, clock=Monotonic())
    for _ in range(5):
        _fail(throttle, "admin")
    _fail(throttle, "x")
    _fail(throttle, "y")  # 가득 참 → 차단되지 않은 "x"를 버린다
    assert throttle.begin("admin") == 600.0
    assert _fail(throttle, "z") is False  # "y"를 버린다
    assert throttle.begin("admin") == 600.0


def test_full_of_blocked_entries_rejects_new_usernames() -> None:
    """버릴 항목이 없으면 새 username을 추적 없이 통과시키지 않는다(fail-closed)."""
    clock = Monotonic()
    throttle = LoginThrottle(max_tracked=2, clock=clock)
    for name in ("a", "b"):
        for _ in range(5):
            _fail(throttle, name)
    assert throttle.begin("c") == 5.0
    clock.now = 600.0  # 차단이 끝나면 빈 항목이 정리돼 자리가 난다
    assert throttle.begin("c") is None
