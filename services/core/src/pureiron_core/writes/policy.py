"""쓰기 선언 해석·실행 전 검사·결과 판정 — 순수 함수(plan M3c D4·D5·D9·D10·D12).

- 선언(`tags.definition.write`)은 project-tools가 검증해 들어온다. 그래도 다시 읽어 어긋나면
  그 태그만 쓰기 불가로 둔다(호출자가 경고 로그). 주소·형식 규칙은 project-tools `writes.py`와
  같다.
- 검사(`evaluate`)는 이미 모은 사실(`WriteFacts`)만 본다: I/O·await 없음(TagStore 동기 스냅샷).
- 판정(`classify`): setpoint는 readback이 쓴 값과 같아야 CONFIRMED다. float32는 목표값을 그
  형식으로 인코딩→디코딩한 값(가장 가까운 float32 — collector `encode`와 같은 규칙, collector
  코드를 import하지 않으므로 여기 둔다, M3c-1 R5)과 비교한다. command는 장비 ack(OK)면 WRITTEN이고
  readback은 기록만 한다(PLC가 명령 비트를 바로 지울 수 있다, D4). OK인데 readback이 없는
  setpoint도 WRITTEN이다 — 장비는 받았고 값은 확인하지 못했다(CONFIRMED도, 결과 모름도 아니다).
- 시한(`send_timeout_ms`): core 응답 대기 시한 W = `PUREIRON_WRITE_TIMEOUT_S`, collector 보내기
  기한 `timeout_ms` = W - 여유 - 2 * 장비 timeout_ms. 그러면 W >= timeout_ms + 2 * 장비
  timeout_ms + 여유라 core가 NO_RESPONSE를 확정한 뒤에는 그 쓰기가 PLC에 닿지 않는다(M3c-1 계약,
  여유 >= 수신 지연).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import cast

from pureiron_core.acquisition.ports import WritePathState, WriteReply, WriteReplyStatus
from pureiron_core.auth.ports import Role
from pureiron_core.project.ports import Definitions, TagDef
from pureiron_core.tags.ports import Quality, TagValue, Value
from pureiron_core.writes.ports import CheckName, WriteCheck, WriteKind, WriteOutcome, WriteTarget

WRITABLE_REGISTER_TYPES = frozenset({"holding", "coil"})
DATA_TYPES = frozenset({"bool", "int16", "uint16", "int32", "uint32", "float32", "float64"})
INTEGER_RANGES: Mapping[str, tuple[int, int]] = {
    "int16": (-(2**15), 2**15 - 1),
    "uint16": (0, 2**16 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "uint32": (0, 2**32 - 1),
}
FLOAT32_MAX = 3.4028234663852886e38
WRITE_ROLES: Mapping[str, Role] = {"operator": Role.OPERATOR, "engineer": Role.ENGINEER}
# core 응답 대기 시한 중 collector 수신 지연(네트워크·처리)에 남기는 여유. plan 적용 중
# 정지(최대 5s)는 `plan_reported` 검사가 막으므로 여기 넣지 않는다.
WRITE_MARGIN_S = 2.0
# collector가 poll 사이 빈틈을 기다릴 최소 기한. 이보다 짧으면 장비 timeout이 시한에 비해 길다.
MIN_SEND_TIMEOUT_MS = 1000
MAX_SEND_TIMEOUT_MS = 60_000  # collector 상한(넘으면 REJECTED)


# --- 선언 해석 ---------------------------------------------------------------------------------


def _mapping(raw: object, name: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be an object")
    return cast("dict[str, object]", raw)  # 정의는 JSON 객체(키는 항상 문자열)


def type_problem(value: object, data_type: str, *, bit: bool) -> str | None:
    """`value`를 이 형식으로 장비에 쓸 수 없으면 이유(project-tools `value_problem`과 같은 규칙)."""
    if bit:
        return None if isinstance(value, bool) else f"{value!r} is not a boolean"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return f"{value!r} is not a number"
    if not math.isfinite(value):
        return f"{value!r} is not finite"
    if data_type in INTEGER_RANGES:
        low, high = INTEGER_RANGES[data_type]
        if not float(value).is_integer() or not low <= value <= high:
            return f"{value!r} is not an integer in {data_type} range [{low}, {high}]"
    if data_type == "float32" and abs(value) > FLOAT32_MAX:
        return f"{value!r} is outside float32 range"
    return None


def _value(raw: object, data_type: str, *, bit: bool) -> Value:
    problem = type_problem(raw, data_type, bit=bit)
    if problem is not None:
        raise ValueError(problem)
    return cast("Value", raw)


def _permissive(raw: object, tag: TagDef, known: frozenset[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("permissive must be a list of tag paths")
    paths: list[str] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, str) or item not in known:
            raise ValueError(f"permissive {item!r} is not a known tag")
        if item == tag.path:
            raise ValueError(f"permissive {item!r} is the tag itself")
        paths.append(item)
    return tuple(paths)


def parse_target(tag: TagDef, known_paths: frozenset[str]) -> WriteTarget:
    """`write` 블록이 있는 태그 하나. 어긋나면 ValueError(호출자가 그 태그만 뺀다)."""
    write = _mapping(tag.definition.get("write"), "write")
    modbus = _mapping(tag.definition.get("modbus"), "modbus")
    register_type, data_type = modbus.get("register_type"), modbus.get("data_type")
    if register_type not in WRITABLE_REGISTER_TYPES:
        raise ValueError(f"register_type {register_type!r} is read-only")
    if not isinstance(data_type, str) or data_type not in DATA_TYPES:
        raise ValueError(f"unknown data_type {data_type!r}")
    if register_type == "holding" and data_type == "bool":
        raise ValueError("data_type bool on a holding register would overwrite all 16 bits")
    bit = register_type == "coil" or data_type == "bool"
    kind = WriteKind(str(write.get("kind")))
    role = WRITE_ROLES.get(str(write.get("role", "operator")))
    if role is None:
        raise ValueError(f"unknown write role {write.get('role')!r}")
    permissive = _permissive(write.get("permissive"), tag, known_paths)
    base = WriteTarget(
        tag_id=tag.tag_id,
        path=tag.path,
        device_id=tag.device_id,
        kind=kind,
        role=role,
        data_type=data_type,
        bit=bit,
        permissive=permissive,
    )
    if kind is WriteKind.SETPOINT:
        if bit:
            raise ValueError("setpoint needs a numeric data_type")
        low = float(_value(write.get("min"), data_type, bit=False))
        high = float(_value(write.get("max"), data_type, bit=False))
        if low > high:
            raise ValueError(f"min {low:g} is greater than max {high:g}")
        return replace(base, minimum=low, maximum=high)
    raw_values = write.get("values")
    if not isinstance(raw_values, list) or not raw_values:
        raise ValueError("command needs values")
    values = tuple(_value(v, data_type, bit=bit) for v in cast("list[object]", raw_values))
    if not permissive:
        raise ValueError("command needs permissive")
    return replace(base, values=values)


@dataclass(frozen=True, slots=True)
class ParsedTargets:
    targets: Mapping[str, WriteTarget]  # path → 선언
    tag_ids: Mapping[str, int]  # 정의의 모든 태그 path → tag_id(허가 조건 조회용)
    problems: tuple[str, ...]


def parse_targets(definitions: Definitions) -> ParsedTargets:
    known = frozenset(tag.path for tag in definitions.tags)
    targets: dict[str, WriteTarget] = {}
    problems: list[str] = []
    for tag in definitions.tags:
        if "write" not in tag.definition:
            continue
        try:
            targets[tag.path] = parse_target(tag, known)
        except ValueError as exc:
            problems.append(f"tag {tag.path!r}: write declaration ignored: {exc}")
    return ParsedTargets(
        targets=targets,
        tag_ids={tag.path: tag.tag_id for tag in definitions.tags},
        problems=tuple(problems),
    )


# --- 값·시한 -------------------------------------------------------------------------------------


def value_problem(target: WriteTarget, value: Value) -> str | None:
    problem = type_problem(value, target.data_type, bit=target.bit)
    if problem is not None:
        return problem
    if target.kind is WriteKind.SETPOINT:
        low, high = target.minimum, target.maximum
        if low is None or high is None or not low <= value <= high:
            return f"{value!r} is outside the allowed range [{low!r}, {high!r}]"
        return None
    if not any(
        isinstance(a, bool) == isinstance(value, bool) and a == value for a in target.values
    ):
        return f"{value!r} is not an allowed value {list(target.values)!r}"
    return None


def send_timeout_ms(write_timeout_s: float, device_timeout_ms: int | None) -> int | None:
    """collector `WriteRequest.timeout_ms`. 장비 timeout이 시한에 비해 길면(또는 모르면) None."""
    if device_timeout_ms is None:
        return None
    budget_ms = math.floor((write_timeout_s - WRITE_MARGIN_S) * 1000) - 2 * device_timeout_ms
    if budget_ms < MIN_SEND_TIMEOUT_MS:
        return None
    return min(budget_ms, MAX_SEND_TIMEOUT_MS)


def written_value(target: WriteTarget, value: Value) -> float:
    """장비에 실제로 들어가는 값(readback 비교 기준). float32는 가장 가까운 float32로 반올림된다."""
    number = float(value)
    if target.data_type == "float32":
        (rounded,) = struct.unpack(">f", struct.pack(">f", number))
        return float(rounded)
    return number


# --- 검사 -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteFacts:
    """검사 한 번에 쓰는 사실. 서비스가 edit-lock 조회(await) 뒤 동기 구간에서 모은다.

    `edit_lock_error`는 site-db를 못 읽은 이유(있으면 실패 — fail-closed). `permissive`는 허가 조건
    path → 현재값(정의에 없거나 plan 밖이면 None).
    """

    enabled: bool
    tag_path: str
    target: WriteTarget | None
    role: Role
    value: Value
    current: TagValue | None
    path: WritePathState
    definitions_rev: int | None
    permissive: Mapping[str, TagValue | None]
    edit_lock_owner: str | None
    edit_lock_error: str | None
    in_flight: bool
    write_timeout_s: float


def _check(name: CheckName, problem: str | None) -> WriteCheck:
    return WriteCheck(name, problem is None, problem)


def _permits(value: TagValue | None) -> bool:
    return value is not None and value.quality is Quality.GOOD and value.value == 1


def _plan_problem(path: WritePathState, rev: int | None) -> str | None:
    if path.plan_rev is None or path.plan_rev != rev:
        return f"plan rev {path.plan_rev} is not the definitions rev {rev} (plan changing)"
    if path.reported_plan_rev != path.plan_rev:
        return (
            f"collector has not reported plan rev {path.plan_rev} yet"
            f" (last batch rev {path.reported_plan_rev})"
        )
    return None


def _target_checks(facts: WriteFacts, target: WriteTarget) -> list[WriteCheck]:
    current, path = facts.current, facts.path
    blocked = [p for p in target.permissive if not _permits(facts.permissive.get(p))]
    lock = None
    if facts.edit_lock_error is not None:
        lock = f"cannot read edit lock: {facts.edit_lock_error}"
    elif facts.edit_lock_owner is not None:
        lock = f"edit lock held by {facts.edit_lock_owner!r} (update in progress)"
    return [
        _check(
            CheckName.ROLE,
            None if facts.role >= target.role else f"requires role {target.role.label} or higher",
        ),
        _check(CheckName.VALUE, value_problem(target, facts.value)),
        _check(
            CheckName.TAG_GOOD,
            None
            if current is not None and current.quality is Quality.GOOD
            else f"tag quality is {current.quality.name if current else 'unknown'}",
        ),
        _check(
            CheckName.DEVICE_CONNECTED,
            None if path.device_connected else f"device {target.device_id!r} is not connected",
        ),
        _check(
            CheckName.SINGLE_SESSION,
            None if path.sessions == 1 else f"{path.sessions} collector session(s), need one",
        ),
        _check(CheckName.PLAN_REPORTED, _plan_problem(path, facts.definitions_rev)),
        _check(
            CheckName.SEND_WINDOW,
            None
            if send_timeout_ms(facts.write_timeout_s, path.device_timeout_ms) is not None
            else f"device timeout_ms {path.device_timeout_ms} leaves no send window within"
            f" PUREIRON_WRITE_TIMEOUT_S={facts.write_timeout_s:g}",
        ),
        _check(
            CheckName.PERMISSIVE,
            None if not blocked else f"permissive not GOOD/true: {', '.join(blocked)}",
        ),
        _check(CheckName.NO_EDIT_LOCK, lock),
        _check(
            CheckName.NOT_IN_FLIGHT,
            "another write to this tag is in progress" if facts.in_flight else None,
        ),
    ]


def evaluate(facts: WriteFacts) -> tuple[WriteCheck, ...]:
    """D9 검사 전부(순서 고정). 선언이 없으면 태그별 검사는 할 수 없어 앞의 둘만 낸다."""
    checks = [
        _check(
            CheckName.WRITES_ENABLED,
            None if facts.enabled else "writes are disabled on this site",
        )
    ]
    if facts.target is None:
        checks.append(
            _check(CheckName.DECLARED, f"tag {facts.tag_path!r} has no write declaration")
        )
        return tuple(checks)
    checks.append(_check(CheckName.DECLARED, None))
    checks.extend(_target_checks(facts, facts.target))
    return tuple(checks)


def failed(checks: Sequence[WriteCheck]) -> tuple[WriteCheck, ...]:
    return tuple(check for check in checks if not check.ok)


# --- 판정 -------------------------------------------------------------------------------------


# ack가 아닌 결과는 readback 판정 없이 옮긴다. INDETERMINATE·NO_RESPONSE는 써졌을 수 있다
# (결과 모름).
_NOT_ACKED: Mapping[WriteReplyStatus, WriteOutcome] = {
    WriteReplyStatus.REJECTED: WriteOutcome.REJECTED,
    WriteReplyStatus.FAILED: WriteOutcome.FAILED,
    WriteReplyStatus.INDETERMINATE: WriteOutcome.NO_RESPONSE,
    WriteReplyStatus.NO_RESPONSE: WriteOutcome.NO_RESPONSE,
}


def classify(
    target: WriteTarget, value: Value, reply: WriteReply
) -> tuple[WriteOutcome, str | None]:
    """collector 응답 → 결과(D10)와 오류 문구."""
    error = reply.error or None
    if reply.status is not WriteReplyStatus.OK:
        return _NOT_ACKED[reply.status], error
    if target.kind is WriteKind.COMMAND:
        return WriteOutcome.WRITTEN, error
    if reply.readback is None:
        return WriteOutcome.WRITTEN, f"readback unavailable: {error or 'no readback'}"
    expected = written_value(target, value)
    if reply.readback == expected:
        return WriteOutcome.CONFIRMED, None
    return WriteOutcome.MISMATCH, f"readback {reply.readback!r} differs from written {expected!r}"
