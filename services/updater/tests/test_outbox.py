"""보고 대기열·seq 카운터(`outbox.py`): 처음 매긴 seq 하나(N-b), crash 지점, 연속 구간, r4 저널.

온라인 전송 쪽 crash 지점(전송 뒤·이동 전)과 온라인 실패분의 USB 반출은 `test_send.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pureiron_bundle import keys
from pureiron_bundle.manifest import Json
from pureiron_bundle.sitesign import SitePurpose, open_document
from pureiron_updater import outbox
from pureiron_updater.enroll import EnrollError, enroll, load_identity
from pureiron_updater.metrics import storable_text, write_report
from pureiron_updater.outbox import (
    JOURNAL_FILE,
    QueuedReport,
    SeqPendingError,
    assign,
    contiguous_runs,
    export_reports,
    pending,
    reserve_seq,
)
from pureiron_updater.settings import Settings
from pureiron_updater.state import Attempt, Phase

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = tmp_path / "pureiron"
    settings = Settings(
        site_id="site-a",
        root=root,
        root_pubkey_file=root / "root.pub",
        core_url="http://core.invalid/api/v1",
        env_file=tmp_path / "updater.env",
    )
    enroll(settings, "site-a", now=NOW)
    return settings


def queue(settings: Settings, name: str, generation: int, **extra: Json) -> Path:
    directory = settings.state_dir / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    body: dict[str, Json] = {"generation": generation, "result": "succeeded", **extra}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def next_seq(settings: Settings) -> int:
    identity = load_identity(settings.state_dir)
    assert identity is not None
    return identity.next_report_seq


def stored(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8")).get("report_seq")


def bundle_reports(settings: Settings, path: Path) -> list[tuple[object, object]]:
    public = keys.public_key(settings.site_key_file.read_bytes())
    payload = open_document(path.read_bytes(), SitePurpose.REPORTS, public)
    reports = payload["reports"]
    assert isinstance(reports, list)
    return [(r["report_seq"], r["generation"]) for r in reports if isinstance(r, dict)]


def test_reserve_moves_the_shared_counter(settings: Settings) -> None:
    assert reserve_seq(settings.state_dir) == 1
    assert reserve_seq(settings.state_dir, 3) == 2
    assert next_seq(settings) == 5
    with pytest.raises(ValueError, match="count"):
        reserve_seq(settings.state_dir, 0)


def test_assign_writes_the_seq_once_into_the_queue_file(settings: Settings) -> None:
    path = queue(settings, "a.json", 1)
    first = assign(settings.state_dir, path)
    assert (first.report_seq, stored(path), next_seq(settings)) == (1, 1, 2)
    again = assign(settings.state_dir, path)
    assert again.report_seq == 1  # 파일의 값 — 다시 예약하지 않는다
    assert next_seq(settings) == 2
    assert again.body["generation"] == 1


def test_crash_after_reserve_before_file_write_skips_one_seq(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """예약 뒤 파일 쓰기 전에 죽으면 seq 하나가 빈다 — 재실행은 새 seq, 같은 seq 두 번은 없다."""
    path = queue(settings, "a.json", 1)

    def crash(*_args: object) -> None:
        raise OSError("power cut")

    with monkeypatch.context() as patch:
        patch.setattr(outbox, "write_report_file", crash)
        with pytest.raises(OSError, match="power cut"):
            assign(settings.state_dir, path)
    assert stored(path) is None
    assert next_seq(settings) == 2
    assert assign(settings.state_dir, path).report_seq == 2
    assert next_seq(settings) == 3


def test_crash_after_file_write_keeps_the_seq(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """파일에 seq를 쓴 뒤(묶음 서명 전) 죽으면 재실행이 그 seq로 낸다."""
    path = queue(settings, "a.json", 1)
    out = settings.root / "usb"

    def crash(*_args: object) -> object:
        raise OSError("USB pulled")

    with monkeypatch.context() as patch:
        patch.setattr(outbox, "sign_document", crash)
        with pytest.raises(OSError, match="USB pulled"):
            export_reports(settings, out, now=NOW)
    assert stored(path) == 1
    result = export_reports(settings, out, now=NOW)
    assert result is not None
    assert bundle_reports(settings, result[0]) == [(1, 1)]
    assert next_seq(settings) == 2


def test_crash_after_bundle_before_move_rewrites_the_same_bundle(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue(settings, "a.json", 1)
    queue(settings, "b.json", 2)
    out = settings.root / "usb"

    def crash(*_args: object) -> Path:
        raise OSError("power cut")

    with monkeypatch.context() as patch:
        patch.setattr(outbox, "move", crash)
        with pytest.raises(OSError, match="power cut"):
            export_reports(settings, out, now=NOW)
    first = out / "pureiron-reports-site-a-000001-000002.json"
    assert first.exists()
    first.unlink()
    result = export_reports(settings, out, now=NOW)
    assert result == (first, 2)
    assert bundle_reports(settings, first) == [(1, 1), (2, 2)]
    assert export_reports(settings, out, now=NOW) is None


def test_export_uses_stored_seq_and_splits_contiguous_runs(settings: Settings) -> None:
    """온라인으로 먼저 매긴 seq(3)는 그대로, 새 보고는 새 seq — 구간마다 묶음 하나(E7)."""
    queue(settings, "a.json", 1, report_seq=3)
    reserve_seq(settings.state_dir, 3)  # 1..3 사용(3은 a, 1·2는 heartbeat)
    queue(settings, "b.json", 2)
    reserve_seq(settings.state_dir)  # 4 = heartbeat
    queue(settings, "c.json", 3)
    out = settings.root / "usb"
    # 할당은 export 때 이름순: b = 5, c = 6
    written: list[tuple[Path, int]] = []
    while (result := export_reports(settings, out, now=NOW)) is not None:
        written.append(result)
    assert [(path.name, count) for path, count in written] == [
        ("pureiron-reports-site-a-000003-000003.json", 1),
        ("pureiron-reports-site-a-000005-000006.json", 2),
    ]
    assert bundle_reports(settings, written[1][0]) == [(5, 2), (6, 3)]
    assert pending(settings.state_dir) == []


def test_contiguous_runs_sorts_by_seq() -> None:
    def item(seq: int) -> QueuedReport:
        return QueuedReport(Path(f"{seq}.json"), seq, {})

    runs = contiguous_runs([item(7), item(2), item(3), item(9), item(8)])
    assert [[r.report_seq for r in run] for run in runs] == [[2, 3], [7, 8, 9]]


def _journal(settings: Settings, first: int, names: list[str]) -> None:
    path = settings.state_dir / "reports" / JOURNAL_FILE
    path.write_text(json.dumps({"first_seq": first, "names": names}), encoding="utf-8")


def test_r4_journal_before_reservation_is_finished_with_its_seq(settings: Settings) -> None:
    """r4 export가 저널만 쓰고 죽었다(카운터 예약 전) — 저널의 seq·보고 그대로 낸다."""
    queue(settings, "a.json", 1)
    queue(settings, "b.json", 2)
    queue(settings, "c.json", 3)  # 저널 뒤에 쌓인 보고
    _journal(settings, 1, ["a.json", "b.json"])
    assert next_seq(settings) == 1
    out = settings.root / "usb"
    result = export_reports(settings, out, now=NOW)
    assert result is not None
    assert bundle_reports(settings, result[0]) == [(1, 1), (2, 2), (3, 3)]
    assert not (settings.state_dir / "reports" / JOURNAL_FILE).exists()
    assert next_seq(settings) == 4


def test_r4_journal_after_partial_move_keeps_moved_reports_out(settings: Settings) -> None:
    """r4가 묶음을 쓰고 일부만 옮긴 뒤 죽었다 — 남은 보고만 같은 seq로 다시 낸다."""
    queue(settings, "a.json", 1)
    queue(settings, "b.json", 2)
    exported = settings.state_dir / "reports" / "exported"
    exported.mkdir()
    (settings.state_dir / "reports" / "a.json").replace(exported / "a.json")
    _journal(settings, 4, ["a.json", "b.json"])
    reserve_seq(settings.state_dir, 5)  # r4가 이미 5까지 예약(next 6)
    result = export_reports(settings, settings.root / "usb", now=NOW)
    assert result is not None
    assert bundle_reports(settings, result[0]) == [(5, 2)]
    assert next_seq(settings) == 6
    assert "report_seq" not in json.loads((exported / "a.json").read_text("utf-8"))


def test_seq_pending_and_unenrolled_refuse(settings: Settings, tmp_path: Path) -> None:
    identity_file = settings.state_dir / "site.json"
    doc = json.loads(identity_file.read_text("utf-8"))
    identity_file.write_text(json.dumps({**doc, "seq_pending": True}), encoding="utf-8")
    queue(settings, "a.json", 1)
    with pytest.raises(SeqPendingError):
        export_reports(settings, settings.root / "usb", now=NOW)
    with pytest.raises(SeqPendingError):
        reserve_seq(settings.state_dir)
    assert stored(settings.state_dir / "reports" / "a.json") is None
    bare = Settings(
        site_id="site-b",
        root=tmp_path / "bare",
        root_pubkey_file=tmp_path / "root.pub",
        core_url="http://core.invalid/api/v1",
    )
    with pytest.raises(EnrollError, match="not enrolled"):
        reserve_seq(bare.state_dir)


def _attempt(detail: str) -> Attempt:
    return Attempt(
        phase=Phase.FAILED,
        bundle="/var/lib/pureiron/bundles/apply-a.pib",
        bundle_sha256="sha256:" + "a" * 64,
        extract_dir="/var/lib/pureiron/state/extract",
        started_at=NOW,
        generation=3,
        from_generation=2,
        failed_phase=Phase.SWITCHING,
        code="E_SWITCH",
        detail=detail,
        timeline={"verified": NOW},
    )


def test_surrogate_command_output_is_stored_and_signs(settings: Settings) -> None:
    """r3: `surrogateescape`로 읽힌 명령 출력·NUL이 든 보고도 서명·저장 가능하게 쓴다."""
    raw = b"compose: \xff\xfe failed".decode("utf-8", "surrogateescape")
    path = write_report(settings.state_dir, _attempt(f"{raw}\x00 lone \ud800"), NOW)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["logs"] == "compose: \ufffd\ufffd failed\ufffd lone ?"
    result = export_reports(settings, settings.root / "usb", now=NOW)
    assert result is not None
    assert bundle_reports(settings, result[0]) == [(1, 3)]


def test_r4_queue_file_with_escaped_surrogate_still_signs(settings: Settings) -> None:
    """r4는 서로게이트를 `\\udcff` 이스케이프로 큐에 남겼다 — 읽을 때 정리해 서명한다."""
    directory = settings.state_dir / "reports"
    directory.mkdir(parents=True)
    (directory / "a.json").write_text(
        '{"generation": 1, "result": "failed", "logs": "x\\udcff"}', encoding="utf-8"
    )
    result = export_reports(settings, settings.root / "usb", now=NOW)
    assert result is not None
    assert bundle_reports(settings, result[0]) == [(1, 1)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain 한글", "plain 한글"),
        ("nul\x00", "nul\ufffd"),
        (b"\xc3(".decode("utf-8", "surrogateescape"), "\ufffd("),
        ("\udc80\ud83d", "\ufffd?"),
    ],
)
def test_storable_text(text: str, expected: str) -> None:
    assert storable_text(text) == expected
    storable_text(expected).encode("utf-8")


def test_finish_rerun_keeps_the_seq_and_does_not_requeue_sent_reports(settings: Settings) -> None:
    """crash 뒤 `_finish`가 보고를 다시 써도 이미 매긴 seq는 그대로, 이미 나간 보고는 다시 큐에
    넣지 않는다(그 사이 `report export`가 끼어든 경우)."""
    attempt = _attempt("boom")
    path = write_report(settings.state_dir, attempt, NOW)
    assert assign(settings.state_dir, path).report_seq == 1
    assert write_report(settings.state_dir, attempt, NOW) == path
    assert stored(path) == 1
    assert export_reports(settings, settings.root / "usb", now=NOW) is not None
    write_report(settings.state_dir, attempt, NOW)
    assert pending(settings.state_dir) == []
