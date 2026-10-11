"""WS `/api/v1/ws` 메시지와 태그·알람·장비 JSON 모델(plan M3b D10). 계약 정본은 이 모델이다.

서버 → 클라이언트: `hello` · `tag_update` · `alarm_event` · `device_update` · `ping`.
클라이언트 → 서버: `auth`(쿠키가 없을 때 첫 메시지) · `subscribe` · `unsubscribe` · `pong`.
REST(`/tags`·`/alarms`·`/devices`)도 같은 `TagValueOut`·`AlarmOut`·`DeviceOut`을 쓴다. 메시지 유니온
(`WsServerMessage`·`WsClientMessage`)은 OpenAPI components에 실려 web `schema.d.ts`로 생성된다.
NaN/Inf 값은 JSON에 없으므로 null로 보낸다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from pureiron_core.acquisition.ports import DeviceSnapshot
from pureiron_core.alarms.ports import AlarmSnapshot
from pureiron_core.tags.ports import TagValue, json_value

# 구독 한 메시지·한 연결의 tag_id 상한(메모리 상한). tag_id는 site-db int4(1 이상).
MAX_TAG_IDS = 10_000
TAG_ID_MAX = 2**31 - 1
_TOKEN_MAX_CHARS = 256

type TagId = Annotated[int, Field(ge=1, le=TAG_ID_MAX)]


def _time(ns: int | None) -> datetime | None:
    return datetime.fromtimestamp(ns / 1e9, UTC) if ns else None


class TagValueOut(BaseModel):
    tag_id: int
    path: str
    device_id: str
    value: float | bool | None = Field(description="NaN/Inf·값 없음은 null")
    quality: Literal["GOOD", "BAD", "UNCERTAIN"]
    ts: datetime | None = Field(description="샘플 시각(아직 샘플이 없으면 null)")

    @classmethod
    def of(cls, tag: TagValue) -> TagValueOut:
        return cls(
            tag_id=tag.tag_id,
            path=tag.path,
            device_id=tag.device_id,
            value=json_value(tag.value),
            quality=tag.quality.name,
            ts=_time(tag.ts_ns),
        )


class AlarmOut(BaseModel):
    alarm_id: str
    tag_path: str
    alarm_type: Literal["above", "below"] = Field(description="프로젝트 정의의 `type`")
    setpoint: float
    deadband: float
    on_delay_s: float
    priority: Literal["low", "medium", "high", "critical"]
    description: str | None
    state: Literal["NORMAL", "ACTIVE_UNACKED", "ACTIVE_ACKED", "CLEARED_UNACKED", "RETIRED"] = (
        Field(description="RETIRED = 정의에서 빠짐(WS 방송·저널에만 나온다)")
    )
    value: float | None = Field(description="마지막 전이를 만든 값(ACK는 값을 바꾸지 않는다)")
    value_ts: datetime | None = Field(description="그 값의 샘플 시각(복원한 알람은 null)")
    changed_at: datetime | None = Field(description="마지막 전이 시각")
    changed_by: str | None = Field(description="마지막 전이를 한 사용자(ACK). 조건 변화는 null")

    @classmethod
    def of(cls, alarm: AlarmSnapshot) -> AlarmOut:
        return cls(
            alarm_id=alarm.alarm_id,
            tag_path=alarm.tag_path,
            alarm_type=alarm.kind.value,
            setpoint=alarm.setpoint,
            deadband=alarm.deadband,
            on_delay_s=alarm.on_delay_s,
            priority=alarm.priority.value,
            description=alarm.description,
            state=alarm.state.value,
            value=alarm.value,
            value_ts=_time(alarm.value_ts_ns),
            changed_at=alarm.changed_at,
            changed_by=alarm.changed_by,
        )


class DeviceOut(BaseModel):
    """장비 통신 상태(태그가 아니다). health 필드는 collector `DeviceHealth`, 못 받았으면 null."""

    device_id: str
    protocol: str
    state: Literal["CONNECTED", "DISCONNECTED"] = Field(
        description="core 판정: health가 connected이고 무신호가 아니면 CONNECTED"
    )
    silent: bool = Field(description="core 무신호 판정(live 샘플이 max(3*poll, 5s) 동안 없음)")
    tag_count: int
    connected: bool | None
    status: str | None
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    consecutive_failures: int | None
    poll_latency_ms: float | None
    read_success_rate: float | None
    poll_total: int | None
    timeout_count: int | None
    bad_quality_tag_count: int | None
    reconnect_count: int | None

    @classmethod
    def of(cls, device: DeviceSnapshot) -> DeviceOut:
        return cls(
            device_id=device.device_id,
            protocol=device.protocol,
            state="CONNECTED" if device.state.value == "connected" else "DISCONNECTED",
            silent=device.silent,
            tag_count=device.tag_count,
            connected=device.connected,
            status=device.status,
            last_success_at=_time(device.last_success_unix_ns),
            last_error=device.last_error,
            last_error_at=_time(device.last_error_at_unix_ns),
            consecutive_failures=device.consecutive_failures,
            poll_latency_ms=device.poll_latency_ms,
            read_success_rate=device.read_success_rate,
            poll_total=device.poll_total,
            timeout_count=device.timeout_count,
            bad_quality_tag_count=device.bad_quality_tag_count,
            reconnect_count=device.reconnect_count,
        )


# --- 서버 → 클라이언트 ----------------------------------------------------------------------


class HelloMessage(BaseModel):
    type: Literal["hello"]
    username: str
    role: Literal["viewer", "operator", "engineer", "admin"]


class TagUpdateMessage(BaseModel):
    """구독한 태그의 변화(배치 주기마다 합침 — 태그별 최신값만). 구독 직후 현재값도 이 메시지."""

    type: Literal["tag_update"]
    values: list[TagValueOut]


class AlarmEventMessage(AlarmOut):
    """알람 상태 전이(인증된 모든 연결에 방송). 필드는 전이 뒤 `AlarmOut`."""

    type: Literal["alarm_event"]


class DeviceUpdateMessage(DeviceOut):
    """장비 상태 변화(인증된 모든 연결에 방송)."""

    type: Literal["device_update"]


class PingMessage(BaseModel):
    """20s마다. 클라이언트는 `pong`으로 답한다(60s 동안 pong이 없으면 서버가 끊는다)."""

    type: Literal["ping"]


type ServerMessage = (
    HelloMessage | TagUpdateMessage | AlarmEventMessage | DeviceUpdateMessage | PingMessage
)


class WsServerMessage(RootModel[Annotated[ServerMessage, Field(discriminator="type")]]):
    """서버가 보내는 WS 메시지(JSON 텍스트 프레임 하나에 하나)."""


# --- 클라이언트 → 서버 ----------------------------------------------------------------------


class AuthMessage(BaseModel):
    """세션 쿠키가 없는 클라이언트의 첫 메시지(연결 후 5s 안). 토큰 = 로그인 응답의 `token`."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["auth"]
    token: str = Field(min_length=1, max_length=_TOKEN_MAX_CHARS)


class SubscribeMessage(BaseModel):
    """태그 구독 추가. 새로 구독한 태그의 현재값을 곧바로 `tag_update`로 한 번 보낸다."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["subscribe"]
    tag_ids: list[TagId] = Field(max_length=MAX_TAG_IDS)


class UnsubscribeMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["unsubscribe"]
    tag_ids: list[TagId] = Field(max_length=MAX_TAG_IDS)


class PongMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pong"]


type ClientMessage = AuthMessage | SubscribeMessage | UnsubscribeMessage | PongMessage


class WsClientMessage(RootModel[Annotated[ClientMessage, Field(discriminator="type")]]):
    """클라이언트가 보내는 WS 메시지. 형식이 어긋난 메시지는 무시한다."""


def encode(message: ServerMessage) -> str:
    return message.model_dump_json()
