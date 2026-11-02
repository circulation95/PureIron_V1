"""온라인 송신(`send.py`): 응답별 처리 표, 처음 매긴 seq 하나(N-b), heartbeat 내용·실패 처리."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest
from updater_fakes import T0, Kit, Site, closed_window

from pureiron_bundle import keys
from pureiron_bundle.manifest import Json
from pureiron_bundle.sitesign import SitePurpose, open_document
from pureiron_updater import metrics, send
from pureiron_updater.enroll import enroll, load_identity
from pureiron_updater.outbox import Signer, export_reports, signer
from pureiron_updater.poll import SendError
from pureiron_updater.send import (
    DiskState,
    ReportsSent,
    disk_state,
    send_all,
    send_heartbeat,
    send_reports,
    updater_phase,
)
from pureiron_updater.settings import Settings
from pureiron_updater.state import UpdaterState

CONNECTION_REFUSED = None


@dataclass
class FakeApi:
    """control-api 송신 표면. `statuses`를 앞에서부터 하나씩 응답한다(None = 연결 실패)."""

    public: bytes
    statuses: list[int | None] = field(default_factory=list[int | None])
    posts: list[tuple[str, dict[str, Json]]] = field(
        default_factory=list[tuple[str, dict[str, Json]]]
    )

    def site_path(self, suffix: str) -> str:
        return f"sites/site-a/{suffix}"

    def post(self, path: str, data: bytes, content_type: str) -> tuple[int, bytes]:
        assert content_type == "application/json"
        status = self.statuses.pop(0) if self.statuses else 200
        if status is None:  # CONNECTION_REFUSED
            raise SendError(f"POST {path}: connection refused")
        purpose = SitePurpose.HEARTBEAT if path.endswith("/heartbeat") else SitePurpose.REPORT
        self.posts.append((path, open_document(data, purpose, self.public)))
        return status, b'{"detail": "payload.result must be a non-empty string"}'

    def seqs(self, suffix: str) -> list[object]:
        return [payload["report_seq"] for path, payload in self.posts if path.endswith(suffix)]


@dataclass
class Host:
    site: Site
    settings: Settings
    api: FakeApi

    @property
    def queue(self) -> Path:
        return self.settings.state_dir / "reports"

    def report(self, name: str, generation: int) -> Path:
        self.queue.mkdir(parents=True, exist_ok=True)
        path = self.queue / name
        body = {"generation": generation, "to": generation, "result": "succeeded"}
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def sign(self) -> Signer:
        return signer(self.settings)

    def next_seq(self) -> int:
        identity = load_identity(self.settings.state_dir)
        assert identity is not None
        return identity.next_report_seq


@pytest.fixture
def host(tmp_path: Path) -> Host:
    site = Site(root=tmp_path / "pureiron", kit=Kit(tmp_path))
    settings = site.settings
    enroll(settings, "site-a", now=T0)
    public = keys.public_key(settings.site_key_file.read_bytes())
    return Host(site, settings, FakeApi(public))


# ── 보고 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "first_in", "second_sent"),
    [
        (200, "exported", True),
        (400, "rejected", True),
        (413, "rejected", True),
        (401, "queue", False),
        (403, "queue", False),
        (404, "queue", False),
        (409, "queue", False),
        (500, "queue", False),
        (CONNECTION_REFUSED, "queue", False),
    ],
)
def test_report_response_table(
    host: Host, status: int | None, first_in: str, second_sent: bool
) -> None:
    """200 → exported, 400·413 → rejected(큐를 막지 않음), 409(키 불일치 — rekey 대기)·그 밖·연결
    실패 → 남기고 이 tick 중단."""
    first = host.report("20261101T010000000000Z-g1-a.json", 1)
    second = host.report("20261101T020000000000Z-g2-b.json", 2)
    host.api.statuses = [status]
    result = send_reports(host.settings, host.api, host.sign())
    where = {"queue": host.queue, "exported": host.queue / "exported"}
    where["rejected"] = host.queue / "rejected"
    assert (where[first_in] / first.name).exists()
    assert ((host.queue / "exported" / second.name).exists()) is second_sent
    assert result.stopped is not second_sent
    assert result.rejected == (1 if first_in == "rejected" else 0)
    counters = metrics.load_counters(host.settings.state_dir)
    assert counters.rejected_total == result.rejected
    # 못 보낸 보고도 seq는 이미 파일에 있다 — 다음 tick·USB가 같은 값을 쓴다.
    stamped = json.loads((where[first_in] / first.name).read_text(encoding="utf-8"))["report_seq"]
    assert stamped == 1
    if not second_sent:
        assert "report_seq" not in json.loads(second.read_text(encoding="utf-8"))


def test_reports_are_sent_oldest_first_at_most_twenty_per_tick(host: Host) -> None:
    for i in range(23):
        host.report(f"20261101T{i:02d}0000000000Z-g{i + 1}-x.json", i + 1)
    assert send_reports(host.settings, host.api, host.sign()) == ReportsSent(sent=20)
    assert host.api.seqs("/reports") == list(range(1, 21))
    gens = [payload["generation"] for _, payload in host.api.posts]
    assert gens == list(range(1, 21))
    assert send_reports(host.settings, host.api, host.sign()) == ReportsSent(sent=3)
    assert all(p["site_id"] == "site-a" for _, p in host.api.posts)


def test_online_failure_then_usb_export_keeps_the_first_seq(host: Host) -> None:
    """N-b: 온라인에서 seq를 받고 실패한 보고를 `report export`가 새 seq로 다시 매기지 않는다."""
    host.report("20261101T010000000000Z-g1-a.json", 1)
    host.api.statuses = [503]
    send_reports(host.settings, host.api, host.sign())
    assert host.api.seqs("/reports") == [1]
    send_heartbeat(host.settings, UpdaterState(), host.api, host.sign(), now=T0)  # seq 2
    host.report("20261101T020000000000Z-g2-b.json", 2)
    out = host.settings.root / "usb"
    written: list[Path] = []
    while (result := export_reports(host.settings, out, now=T0)) is not None:
        written.append(result[0])
    assert [p.name for p in written] == [
        "pureiron-reports-site-a-000001-000001.json",
        "pureiron-reports-site-a-000003-000003.json",
    ]


def test_crash_after_send_before_move_resends_the_same_seq(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """전송 뒤 이동 전에 죽으면 다음 tick이 같은 seq로 다시 보낸다(중앙은 history로 받는다)."""
    host.report("20261101T010000000000Z-g1-a.json", 1)

    def crash(*_args: object) -> Path:
        raise OSError("power cut")

    with monkeypatch.context() as patch:
        patch.setattr(send, "move", crash)
        with pytest.raises(OSError, match="power cut"):
            send_reports(host.settings, host.api, host.sign())
    assert send_reports(host.settings, host.api, host.sign()) == ReportsSent(sent=1)
    assert host.api.seqs("/reports") == [1, 1]
    assert host.next_seq() == 2


def test_seq_pending_sends_nothing(host: Host, caplog: pytest.LogCaptureFixture) -> None:
    host.report("20261101T010000000000Z-g1-a.json", 1)
    identity_file = host.settings.state_dir / "site.json"
    doc = json.loads(identity_file.read_text(encoding="utf-8"))
    identity_file.write_text(json.dumps({**doc, "seq_pending": True}), encoding="utf-8")
    send_all(host.settings, UpdaterState(), host.api, now=T0)
    assert host.api.posts == []
    assert "report_seq is unknown" in caplog.text
    assert "report_seq" not in json.loads(
        (host.queue / "20261101T010000000000Z-g1-a.json").read_text(encoding="utf-8")
    )


def test_not_enrolled_sends_nothing(tmp_path: Path) -> None:
    site = Site(root=tmp_path / "pureiron", kit=Kit(tmp_path))
    api = FakeApi(b"\0" * 32)
    send_all(site.settings, UpdaterState(), api, now=T0)
    assert api.posts == []


# ── heartbeat ────────────────────────────────────────────────────────────


def test_heartbeat_shares_the_counter_and_is_dropped_on_failure(host: Host) -> None:
    host.api.statuses = [CONNECTION_REFUSED, 500, 200]
    sign = host.sign()
    assert not send_heartbeat(host.settings, UpdaterState(), host.api, sign, now=T0)
    assert not send_heartbeat(host.settings, UpdaterState(), host.api, sign, now=T0)
    assert metrics.load_counters(host.settings.state_dir).heartbeat_last_success is None
    later = T0 + timedelta(minutes=2)
    assert send_heartbeat(host.settings, UpdaterState(), host.api, sign, now=later)
    assert host.api.seqs("/heartbeat") == [2, 3]  # 1은 연결 실패로 버림(재전송 없음)
    host.report("20261101T010000000000Z-g1-a.json", 1)
    send_reports(host.settings, host.api, sign)
    assert host.api.seqs("/reports") == [4]
    counters = metrics.load_counters(host.settings.state_dir)
    assert counters.heartbeat_last_success == later.timestamp()


def test_heartbeat_reports_the_applied_generation_while_an_attempt_runs(host: Host) -> None:
    """N-3: 시도가 진행 중이어도 `current`는 적용된 generation의 값이다."""
    sign = host.sign()
    send_heartbeat(host.settings, UpdaterState(), host.api, sign, now=T0)
    _, before = host.api.posts[-1]
    assert (before["current"], before["updater_phase"]) == (None, "idle")
    host.site.apply(host.site.kit.bundle(1, "r0", version="v3"))
    host.site.submit(host.site.kit.bundle(2, "r1", version="v4", window=closed_window())).advance()
    state = host.site.updater().state
    assert state.attempt is not None
    send_heartbeat(host.settings, state, host.api, sign, now=T0)
    _, during = host.api.posts[-1]
    assert during["current"] == {"product": "2026.10.0", "project_version": "v3", "gen": 1}
    assert during["updater_phase"] == "verified"
    assert during["disk_state"] in {"ok", "warn", "critical"}
    assert during["at"] == "2026-10-05T01:00:00Z"


def test_updater_phase_prefers_the_alarm() -> None:
    assert updater_phase(UpdaterState()) == "idle"
    assert updater_phase(UpdaterState(alarm="g3: E_HEALTH")) == "failed_manual"


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.0, DiskState.OK),
        (0.7999, DiskState.OK),
        (0.80, DiskState.WARN),
        (0.8999, DiskState.WARN),
        (0.90, DiskState.CRITICAL),
        (1.0, DiskState.CRITICAL),
    ],
)
def test_disk_state_boundaries(ratio: float, expected: DiskState) -> None:
    assert disk_state(ratio) is expected


# ── tick 송신 단계·메트릭 ─────────────────────────────────────────────────


def test_send_all_keeps_going_after_a_heartbeat_error_and_writes_metrics(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.report("20261101T010000000000Z-g1-a.json", 1)
    host.report("20261101T020000000000Z-g2-b.json", 2)
    host.api.statuses = [400]  # 첫 보고 거부

    def broken(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("heartbeat bug")

    monkeypatch.setattr(send, "send_heartbeat", broken)
    send_all(host.settings, UpdaterState(), host.api, now=T0)
    assert host.api.seqs("/reports") == [1, 2]
    prom = (host.settings.state_dir / "metrics" / "updater.prom").read_text(encoding="utf-8")
    assert "updater_reports_pending 0" in prom
    assert "updater_reports_rejected_total 1" in prom
    assert "updater_heartbeat_last_success_timestamp_seconds 0" in prom
    # 시도가 끝나 updater.prom을 다시 써도 송신 줄은 남고, 송신이 다시 써도 시도 줄은 남는다.
    metrics.write_prom(host.settings.state_dir, UpdaterState(applied_gen=4), None, T0)
    metrics.refresh_outbox_metrics(host.settings.state_dir)
    prom = (host.settings.state_dir / "metrics" / "updater.prom").read_text(encoding="utf-8")
    assert "updater_applied_generation 4" in prom
    assert prom.count("updater_reports_rejected_total 1") == 1


# ── 리뷰 r1: H1·M2·L1·N4 ──────────────────────────────────────────────────


def test_broken_site_key_is_logged_and_sends_nothing(
    host: Host, caplog: pytest.LogCaptureFixture
) -> None:
    """H1: 길이가 틀린 site.key(`KeyFormatError`)도 송신 단계 안에서 로그로 끝난다."""
    host.report("20261101T010000000000Z-g1-a.json", 1)
    host.settings.site_key_file.write_bytes(b"")
    send_all(host.settings, UpdaterState(), host.api, now=T0)
    assert host.api.posts == []
    assert "site.key must be 32 raw bytes" in caplog.text


def test_metrics_failure_does_not_escape_the_send_step(
    host: Host, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_state_dir: Path) -> None:
        raise ValueError("metrics bug")

    monkeypatch.setattr(send, "refresh_outbox_metrics", broken)
    send_all(host.settings, UpdaterState(), host.api, now=T0)
    assert host.api.seqs("/heartbeat") == [1]
    assert "cannot write outbox metrics" in caplog.text


def test_r4_journal_is_settled_before_the_heartbeat_reserves_a_seq(host: Host) -> None:
    """M2: r4 `report export`가 저널을 쓰고 카운터를 예약하기 전에 죽었다(`first_seq` =
    `next_report_seq`). 첫 tick의 heartbeat가 그 seq를 다시 쓰면 안 된다."""
    report = host.report("20261101T010000000000Z-g1-a.json", 1)
    first = host.next_seq()
    journal = {"first_seq": first, "names": [report.name]}
    (host.queue / ".export.journal").write_text(json.dumps(journal), encoding="utf-8")
    send_all(host.settings, UpdaterState(), host.api, now=T0)
    (heartbeat,) = host.api.seqs("/heartbeat")
    assert host.api.seqs("/reports") == [first]
    assert heartbeat != first
    seqs = [payload["report_seq"] for _, payload in host.api.posts]
    assert len(seqs) == len(set(seqs))  # 같은 seq가 두 메시지에 쓰이지 않는다
    assert not (host.queue / ".export.journal").exists()


def test_every_reservation_settles_the_journal_first(host: Host) -> None:
    """M2: 예약 경로마다(heartbeat 단독 호출 포함) 저널 seq를 다시 내주지 않는다."""
    report = host.report("20261101T010000000000Z-g1-a.json", 1)
    first = host.next_seq()
    journal = {"first_seq": first, "names": [report.name]}
    (host.queue / ".export.journal").write_text(json.dumps(journal), encoding="utf-8")
    send_heartbeat(host.settings, UpdaterState(), host.api, host.sign(), now=T0)
    assert host.api.seqs("/heartbeat") == [first + 1]
    assert json.loads(report.read_text(encoding="utf-8"))["report_seq"] == first


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"], ids=["json", "not-object"])
def test_unreadable_queue_file_is_rejected_and_does_not_block(
    host: Host, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    """L1: 읽을 수 없는 큐 맨 앞 파일은 `rejected/`로 빠지고(오류 로그·카운터) 뒤 보고가 나간다."""
    host.queue.mkdir(parents=True, exist_ok=True)
    broken = host.queue / "20261101T000000000000Z-g1-broken.json"
    broken.write_text(content, encoding="utf-8")
    good = host.report("20261101T010000000000Z-g2-a.json", 2)
    assert send_reports(host.settings, host.api, host.sign()) == ReportsSent(sent=1, rejected=1)
    assert (host.queue / "rejected" / broken.name).read_text(encoding="utf-8") == content
    assert (host.queue / "exported" / good.name).exists()
    assert metrics.load_counters(host.settings.state_dir).rejected_total == 1
    assert "unreadable report" in caplog.text or "not a JSON object" in caplog.text


def test_unreadable_queue_file_does_not_block_usb_export(host: Host) -> None:
    host.queue.mkdir(parents=True, exist_ok=True)
    broken = host.queue / "20261101T000000000000Z-g1-broken.json"
    broken.write_text("{not json", encoding="utf-8")
    host.report("20261101T010000000000Z-g2-a.json", 2)
    exported = export_reports(host.settings, host.settings.root / "usb", now=T0)
    assert exported is not None
    assert exported[1] == 1
    assert (host.queue / "rejected" / broken.name).exists()
    assert metrics.load_counters(host.settings.state_dir).rejected_total == 1


def test_reports_with_a_stored_seq_go_first_in_seq_order(host: Host) -> None:
    """N4: 시계가 뒤로 가 새 파일 이름이 앞서도 이미 seq를 받은 보고가 먼저(송신 순서 = seq
    순서)."""
    older = host.report("20261101T050000000000Z-g1-a.json", 1)
    host.api.statuses = [503]
    send_reports(host.settings, host.api, host.sign())  # older: seq 1, 큐에 남음
    newer_name_first = host.report("20261101T010000000000Z-g2-b.json", 2)
    send_reports(host.settings, host.api, host.sign())
    assert host.api.seqs("/reports") == [1, 1, 2]
    gens = [payload["generation"] for _, payload in host.api.posts]
    assert gens == [1, 1, 2]
    assert not older.exists()
    assert not newer_name_first.exists()
