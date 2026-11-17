"""현장 VM(`Site`)과 원격 조작 — probe·스테이징·수렴 대기·보고 찾기·외부 접속 카운터·
체크포인트 복원.

VM 하나를 전제하지 않는다: 현장마다 `Site`(site id·Hyper-V VM 이름·IP·상태 파일)를 두고, 조작은
그 현장의 `Vm`(ssh)이나 `Context`(`ctx.site`)를 받는다. 판정은 `judges`가 한다.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from harness import buildhost, golden, hyperv, judges, paths
from harness.context import TAIL_CHARS, Context, E2EError
from harness.judges import (
    ALARM_METRIC,
    PATCH_ALARM,
    PATCH_ALARM_BODY,
    PATCH_TAG,
    PATCH_TAG_BODY,
    SEED_DEVICE,
    SEED_TAG,
    as_int,
    db_of,
    judge_attempt,
    metric,
    updater_state,
)
from harness.vm import REMOTE_ERRORS, SSH_FAILED, Vm, VmError

REMOTE = "/home/pureiron/e2e"
SITE_SCRIPTS = ("crash_at.py", "probe.py", "site_api.py")
"""VM에 복사하는 스크립트(`tests/e2e/site/`)."""
STATE_DIR = "/var/lib/pureiron/state"
INCOMING = "/home/pureiron/incoming"
CONVERGE_S = 600
STAGE_TIMEOUT_S = 600
"""`stage`의 원격 명령 하나 시간 제한 기본값(`Vm.run` 기본과 같다 — run.py 경로는 그대로)."""
STAGE_TRIES = 30
DRAFT_REMOTE = f"{REMOTE}/draft.tar"
ENROLLMENT_REMOTE = f"{REMOTE}/enrollment.json"
E2E_ADMIN = "e2e-admin"
ADMIN_PASSWORD = f"{REMOTE}/e2e-admin.pw"
"""VM에서 만든 e2e admin 비밀번호(root 0600, 호스트로 나가지 않는다)."""
CENTRAL_URL = "https://10.77.0.1:28443/api/v1"
"""현장이 보는 중앙 기준 URL(D9) — `central.py`의 BIND·HOST·SNI·포트와 같다."""
CENTRAL_POLL_S = 10
"""중앙 `PUREIRON_CONTROL_POLL_S` = 현장 `PUREIRON_POLL_INTERVAL_S`(online = 수신 ≤ 3 x poll)."""
CA_TRUST = "/usr/local/share/ca-certificates/pureiron-central-root.crt"
UPDATER_ENV = "/etc/pureiron/updater.env"
SEED_SQL = (
    f"INSERT INTO devices (id, protocol) VALUES ('{SEED_DEVICE}', 'modbus_tcp') "
    "ON CONFLICT DO NOTHING; "
    f"INSERT INTO tags (path, device_id) VALUES ('{SEED_TAG}', '{SEED_DEVICE}') "
    "ON CONFLICT DO NOTHING; "
    "INSERT INTO tag_history (time, tag_id, value_num, quality) "
    f"SELECT ts, (SELECT id FROM tags WHERE path = '{SEED_TAG}'), extract(epoch FROM ts), 1 "
    "FROM generate_series(now() - interval '5 days', now() - interval '1 hour', "
    "interval '1 minute') ts ON CONFLICT DO NOTHING; "
    "SELECT count(compress_chunk(c, if_not_compressed => true)) "
    "FROM show_chunks('tag_history', older_than => interval '2 days') c;"
)


@dataclass(frozen=True)
class Site:
    """현장 VM 하나. 현장마다 상태 파일이 따로다(`Context`가 여기에 저장한다)."""

    site_id: str
    """fleet `site:`·enroll·번들 이름·VM 호스트 이름."""
    vm_name: str
    """Hyper-V VM 이름(체크포인트·리셋 대상)."""
    ip: str
    state_file: Path

    @property
    def vm_dir(self) -> Path:
        """차분 디스크·seed ISO 디렉터리."""
        return paths.E2E / "vms" / self.vm_name

    def host(self) -> Vm:
        return Vm(self.ip, paths.SSH_DIR / "id_ed25519")


def as_root(script: str) -> str:
    """원격 root 셸에서 `script` 전체를 실행한다(`cd` 등이 sudo 밖에서 돌지 않게)."""
    return f"sudo bash -c {shlex.quote(script)}"


def probe(host: Vm) -> dict[str, object]:
    return host.json(f"sudo python3 {REMOTE}/probe.py")


def compose_core() -> str:
    return (
        "$(docker ps -q --filter label=com.docker.compose.project=pureiron "
        "--filter label=com.docker.compose.service=core)"
    )


def boot_id(host: Vm) -> str:
    return host.run("cat /proc/sys/kernel/random/boot_id").stdout.strip()


def service_restarts(host: Vm) -> int:
    out = host.run("systemctl show -p NRestarts --value pureiron-updater").stdout
    return as_int(out.strip())


def updater_active(host: Vm) -> bool:
    out = host.run("systemctl is-active pureiron-updater", check=False).stdout
    return out.strip() == "active"


def seed_history(host: Vm) -> None:
    host.run(
        "cid=$(sudo docker ps -q --filter label=com.docker.compose.project=pureiron "
        "--filter label=com.docker.compose.service=site-db) && "
        f'sudo docker exec "$cid" psql -v ON_ERROR_STOP=1 -U pureiron -d pureiron -c "{SEED_SQL}"'
    )


Probe = Callable[[Vm], dict[str, object]]


def wait_ready(
    host: Vm,
    timeout_s: float,
    *,
    read: Probe = probe,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """ready가 될 때까지 probe를 다시 묻는다. probe 실패(ssh 255 `VmError`, 원격 명령 실패
    `RemoteCommandError`, 시간 초과)는 부팅 중의 일시 실패로 치고 다시 묻는다."""
    deadline = clock() + timeout_s
    snap: dict[str, object] = {}
    while clock() < deadline:
        try:
            snap = read(host)
        except (*REMOTE_ERRORS, subprocess.TimeoutExpired):
            sleep(5)
            continue
        if judges.ready(snap):
            return snap
        sleep(5)
    return snap


RUNBOOK_META = (
    "manifest.json manifest.sig release-cert.json release-cert.sig keys/root.pub "
    "keys/root.pem bootstrap/bootstrap-verify.sh"
)
"""runbook 2단계가 전용 디렉터리에 이름으로 푸는 설치 번들 메타데이터(`bootstrap-verify.sh` ⓪)."""


def install_runbook(
    host: Vm,
    install: Path,
    g1: Path,
    *,
    site_id: str,
    runbook_fpr: str,
    runbook_bootstrap: str,
    after_upload: Callable[[], object] | None = None,
    bundle_dir: str | None = None,
) -> float:
    """설치 번들 runbook(2단계 메타데이터 추출·지문 대조 → `install.sh --offline-time-confirmed`)
    → enroll → g1 apply → 서비스 시작. 걸린 초(업로드 뒤부터). 판정은 호출자가 한다.
    `after_upload`는 번들을 올린 직후·설치 전에 부른다(G2-5 설치 전 변조 탐침 — 올린 번들을 다시
    올리지 않는다). run.py는 넘기지 않는다. `bundle_dir`(VM 경로)을 주면 번들이 이미 거기 있다(USB
    대용 디스크 마운트, ★P4) — 올리지 않고 같은 명령을 그 경로에서 친다. 없으면 scp로
    `INCOMING`에."""
    if bundle_dir is None:
        bundle_dir = INCOMING
        for bundle in (install, g1):
            host.put(bundle, f"{INCOMING}/{bundle.name}")
    if after_upload is not None:
        after_upload()
    remote_install = f"{bundle_dir}/{install.name}"
    meta = RUNBOOK_META
    started = time.monotonic()
    # runbook 2단계는 root 셸에서 친다(/root는 700, bootstrap-verify ⓪은 추출물이
    # 실행 사용자 소유인지 본다).
    out = host.run(
        as_root(
            "install -d -m 700 /root/pib-meta && cd /root/pib-meta && "
            "tar --force-local --no-same-owner --no-same-permissions "
            f"-xf {remote_install} {meta} && sha256sum bootstrap/bootstrap-verify.sh"
        )
    ).stdout
    if not out.startswith(runbook_bootstrap):
        raise E2EError(f"bootstrap-verify.sh sha256 mismatch: {out.strip()}")
    host.run(
        as_root(
            "cd /root/pib-meta && bash bootstrap/bootstrap-verify.sh "
            f"--root-fpr {runbook_fpr} --bundle {remote_install} "
            "--dest /root/pureiron-install"
        ),
        timeout_s=900,
    )
    host.run(
        f"sudo bash /root/pureiron-install/install/install.sh --bundle {remote_install} "
        "--offline-time-confirmed",
        timeout_s=1800,
    )
    host.run(
        "echo PUREIRON_POLL_INTERVAL_S=10 | sudo tee -a /etc/pureiron/updater.env >/dev/null && "
        f"sudo pureiron-site enroll --site {site_id}"
    )
    host.run(f"sudo pureiron-site apply {bundle_dir}/{g1.name}", timeout_s=1800)
    host.run("sudo systemctl start pureiron-updater")
    return time.monotonic() - started


# ---------------------------------------------------------------- 외부 접속 카운터


def nft_counters(host: Vm, table: str = "e2e_audit") -> dict[str, int]:
    """nft 표 `inet <table>` 규칙의 comment → 카운터 패킷 수(기본 외부 접속 카운터 `e2e_audit`)."""
    out = host.run(f"sudo nft -j list table inet {table}").stdout
    counters: dict[str, int] = {}
    for entry in cast(list[dict[str, object]], json.loads(out)["nftables"]):
        rule = cast(dict[str, object] | None, entry.get("rule"))
        if rule is None:
            continue
        for expr in cast(list[dict[str, object]], rule.get("expr", [])):
            counter = cast(dict[str, int] | None, expr.get("counter"))
            if counter is not None:
                counters[str(rule.get("comment"))] = counter["packets"]
    return counters


def resolved_lookups(host: Vm, since: int, site_id: str) -> tuple[int, dict[str, int]]:
    """(지금 VM 시각, `since` 이후 resolved가 조회한 이름별 횟수). 0이면 부팅부터."""
    flag = f"--since @{since} " if since else "-b "
    out = host.run(
        f"date +%s; sudo journalctl -u systemd-resolved {flag}--no-pager -o cat", check=False
    ).stdout
    first, _, log = out.partition("\n")
    return as_int(first.strip()), judges.dns_names(log, site_id)


def nft_mark(ctx: Context, host: Vm) -> None:
    """마지막 표시 이후의 외부 접속 시도를 누적한다. 체크포인트 복원·전원 리셋은 카운터를
    되돌리므로 그 직전에 표시하고, 직후 기준을 c0 값·0으로 다시 잡는다. 그동안 resolved가
    조회한 이름도 모은다(출처 증거)."""
    now = nft_counters(host)
    ctx.nft_total = judges.nft_accumulate(ctx.nft_total, ctx.nft_last, now)
    ctx.nft_last = now
    ctx.nft_mark_at, names = resolved_lookups(host, ctx.nft_mark_at, ctx.site.site_id)
    for name, count in names.items():
        ctx.dns_lookups[name] = ctx.dns_lookups.get(name, 0) + count
    ctx.save()


def restore_c0(ctx: Context) -> None:
    nft_mark(ctx, ctx.site.host())
    buildhost.reclaim_host_memory()
    hyperv.restore(ctx.site.vm_name, "c0")
    ctx.nft_last = dict(ctx.nft_c0)  # 복원은 카운터(메모리)도 c0 시점으로 되돌린다
    ctx.save()
    ctx.site.host().wait_ssh()


# ---------------------------------------------------------------- 적용·수렴


def stage(
    host: Vm, pib: Path, *, check: bool = True, timeout_s: float = STAGE_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """번들을 VM에 올리고 `pureiron-site apply`(동기 — 시도 전체가 이 명령 안에서 끝난다). 원격
    명령 하나의 시간 제한은 `timeout_s`(기본 `Vm.run`과 같은 600초 — run.py 경로)."""
    host.put(pib, f"{INCOMING}/{pib.name}")
    result = busy_retry(
        host, f"sudo pureiron-site apply {INCOMING}/{pib.name}", timeout_s=timeout_s
    )
    if check and result.returncode != 0:
        raise E2EError(f"apply {pib.name} exited {result.returncode}: {result.stderr[-800:]}")
    return result


class Shell(Protocol):
    """VM 원격 셸 — `vm.Vm`이 만족한다(단위 시험은 가짜)."""

    def run(
        self, command: str, *, timeout_s: float = 600, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]: ...


BUSY_TRIES = 30
BUSY_EXIT = 2
"""`pureiron-site` 종료 코드 2 = 다른 Updater 실행 중(서비스 tick이 잠금을 잡고 있다)."""


def busy_retry(
    host: Shell,
    command: str,
    *,
    timeout_s: float = STAGE_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> subprocess.CompletedProcess[str]:
    """`pureiron-site` 명령(`check=False`) — exit 2(잠금)면 잠시 뒤 다시(최대 `BUSY_TRIES`번)."""
    result = subprocess.CompletedProcess[str]([], BUSY_EXIT, "", "busy")
    for _ in range(BUSY_TRIES):
        result = host.run(command, check=False, timeout_s=timeout_s)
        if result.returncode != BUSY_EXIT:
            break
        sleep(2)
    return result


def align_seq(host: Shell, site_id: str, next_seq: int) -> None:
    """현장 `report_seq` 카운터를 `next_seq`로 올린다(`pureiron-site enroll --next-seq` — 키는
    그대로, 단조 증가만. 벤더 rekey 응답 반영과 같은 제품 명령). G2-7 시험 정렬: c0 VM + c1 중앙
    조합에서 VM 카운터가 중앙 수위 아래라 보고가 이력으로만 남는 것을 막는다(실제 폐쇄망 현장은
    수위 0)."""
    host.run(f"sudo pureiron-site enroll --site {site_id} --next-seq {next_seq}")


def converge(
    host: Vm,
    gen: int,
    timeout_s: float = CONVERGE_S,
    *,
    read: Probe = probe,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, object], float]:
    """시도가 끝나(attempt 없음) `last_attempted_gen == gen`이고 ready가 될 때까지. probe 실패
    (`VmError`·`RemoteCommandError`·시간 초과)는 재시작·롤백 중의 일시 실패로 치고 다시 묻는다."""
    started = clock()
    snap: dict[str, object] = {}
    while clock() - started < timeout_s:
        try:
            snap = read(host)
        except (*REMOTE_ERRORS, subprocess.TimeoutExpired):
            sleep(5)
            continue
        state = updater_state(snap)
        done = state.get("attempt") is None and state.get("last_attempted_gen") == gen
        if done and state.get("last_result") in ("succeeded", "failed") and judges.ready(snap):
            return snap, clock() - started
        sleep(5)
    return snap, clock() - started


def bundle_sha12(pib: Path) -> str:
    """보고 파일 이름에 들어가는 번들 sha256 앞 12자."""
    with pib.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()[:12]


def report_for(host: Vm, pib: Path) -> dict[str, object]:
    """그 번들의 최근 보고. 파일 이름 `<started>-g<gen>-<sha256 12자>.json`에서 sha로 찾는다 —
    커밋 전 거부는 gen을 믿지 않아 `g unknown`이므로 gen으로는 못 찾는다."""
    sha12 = bundle_sha12(pib)
    out = host.run(
        f"sudo sh -c 'ls -t /var/lib/pureiron/state/reports/*-{sha12}.json "
        f"/var/lib/pureiron/state/reports/exported/*-{sha12}.json 2>/dev/null | head -1 | "
        "xargs -r cat'",
        check=False,
    ).stdout
    return cast(dict[str, object], json.loads(out)) if out.strip() else {}


def report_count(host: Vm, pib: Path) -> int | None:
    """그 번들(sha256 12자)의 보고 파일 수(`reports`·`reports/exported`). 못 읽으면 None —
    `report_for`의 빈 dict는 "보고 없음"과 "못 읽음"이 같아서, 같은 gen no-op(보고를 쓰지 않는다)을
    가를 때는 이 수가 0인지 본다(M6-3b 리뷰 L1)."""
    sha12 = bundle_sha12(pib)
    done = host.run(
        f"sudo sh -c 'ls /var/lib/pureiron/state/reports/*-{sha12}.json "
        f"/var/lib/pureiron/state/reports/exported/*-{sha12}.json 2>/dev/null | wc -l'",
        check=False,
    )
    text = done.stdout.strip()
    return int(text) if done.returncode == 0 and text.isdigit() else None


def apply_and_check(
    ctx: Context,
    name: str,
    gen: int,
    release: str,
    *,
    expect: str,
    prev: str,
    project_dir: str | None = None,
    expect_alarm: str | None = None,
    expect_code: str | None = None,
    check_metric: bool = False,
    extra: Callable[[dict[str, object]], dict[str, bool]] | None = None,
    note: dict[str, object] | None = None,
) -> dict[str, object]:
    """번들을 만들어 적용하고 끝난 시도를 판정해 `name`으로 기록한다. `extra`는 끝난 스냅샷에서
    더 볼 판정, `note`는 결과에 함께 남길 앞 단계 증거다. 실패하면 보고 `logs`도 남긴다."""
    host = ctx.site.host()
    pib = buildhost.build_site_bundle(
        ctx, gen, release, datetime.now(UTC) - timedelta(minutes=5), project_dir
    )
    applied_before = updater_state(probe(host)).get("applied_gen")
    started = time.monotonic()
    stage(host, pib, check=False)
    snap, waited = converge(host, gen)
    took = time.monotonic() - started
    state, report = updater_state(snap), report_for(host, pib)
    want = ctx.digests[release if expect == "succeeded" else prev]
    checks = judge_attempt(
        snap,
        report,
        expect=expect,
        want_digests=want,
        want_applied=gen if expect == "succeeded" else cast(int | None, applied_before),
        expect_alarm=expect_alarm,
        expect_code=expect_code,
        check_metric=check_metric,
    )
    rows = as_int(db_of(snap).get("tag_history_rows"))
    checks["rows_kept"] = rows >= ctx.seed_rows > 0
    if extra is not None:
        checks |= extra(snap)
    ok = all(checks.values())
    detail: dict[str, object] = {
        "gen": gen,
        "release": release,
        "result": state.get("last_result"),
        "code": report.get("code"),
        "seconds": round(took, 1),
        "checks": checks,
        "alarm": state.get("alarm"),
        "images_match": checks["images_match"],
        "epoch": db_of(snap).get("epoch"),
        "verify_s": report.get("verify_s"),
        "load_s": report.get("load_s"),
        "phase_durations_s": report.get("phase_durations_s"),
        "failed_manual_metric": metric(snap, ALARM_METRIC),
        "logs": None if ok else report.get("logs"),
        **(note or {}),
    }
    ctx.record(name, ok, **detail)
    host.run(f"sudo rm -f {INCOMING}/{pib.name}", check=False)
    return {**detail, "snapshot": snap, "converge_s": waited}


def stage_untouched(
    ctx: Context,
    name: str,
    host: Vm,
    pib: Path,
    *,
    want_exit: int,
    want_code: str | None,
    want: str,
) -> None:
    """적용되면 안 되는 번들: CLI 종료 코드·거부 코드, core `StartedAt`·실행 이미지 불변. 실패하면
    CLI stderr 끝과 보고 `logs`를 남긴다."""
    before = probe(host)
    result = stage(host, pib, check=False)
    after = probe(host)
    unchanged = judges.core_started_at(after) == judges.core_started_at(before)
    images_ok = judges.running_digests(after) == ctx.digests[want]
    report = report_for(host, pib)
    code = report.get("code")
    ok = result.returncode == want_exit and code == want_code and unchanged and images_ok
    ctx.record(
        name,
        ok,
        exit=result.returncode,
        code=code,
        core_started_at_unchanged=unchanged,
        images_match=images_ok,
        stderr=None if ok else result.stderr[-TAIL_CHARS:],
        logs=None if ok else report.get("logs"),
    )
    host.run(f"sudo rm -f {INCOMING}/{pib.name}", check=False)


def stage_for_window(
    apply: Callable[[], subprocess.CompletedProcess[str]],
    open_at: datetime,
    *,
    watcher_alive: Callable[[], bool],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    pause_s: float = 2,
) -> tuple[bool, dict[str, object]]:
    """창(`open_at`) 전에 CLI 스테이징을 끝냈는지와 그 진단 기록. 서비스 tick이 잠금을 잡고 있으면
    `apply`가 exit 2라 잠시 뒤 다시 한다. 판정: 마지막 시도가 exit 0이고 끝난 시각이 `open_at`
    전. 기록: 시도마다 rc·걸린 초·감시자 ssh가 아직 살아 있는지, 마지막 rc·시도 횟수·`open_at`
    까지 남은 초(음수 = 창 뒤), 판정이 거짓이면 stdout·stderr 끝부분."""
    tries: list[dict[str, object]] = []
    result = subprocess.CompletedProcess[str]([], 2, "", "")
    for _ in range(STAGE_TRIES):
        started = time.monotonic()
        result = apply()
        tries.append(
            {
                "rc": result.returncode,
                "s": round(time.monotonic() - started, 1),
                "watcher_alive": watcher_alive(),
            }
        )
        if result.returncode != 2:
            break
        time.sleep(pause_s)
    margin_s = (open_at - now()).total_seconds()
    ok = result.returncode == 0 and margin_s > 0
    info: dict[str, object] = {
        "rc": result.returncode,
        "tries": len(tries),
        "margin_s": round(margin_s, 1),
        "attempts": tries,
    }
    if not ok:
        info["stdout_tail"] = (result.stdout or "")[-TAIL_CHARS:]
        info["stderr_tail"] = (result.stderr or "")[-TAIL_CHARS:]
    return ok, info


# ---------------------------------------------------------------- M5 현장 PATCH·온라인 전환


def site_patch(
    host: Vm, *, alarm2: tuple[str, dict[str, object]] | None = None
) -> tuple[dict[str, object], dict[str, object]]:
    """e2e admin(CLI 전용 계정, VM에서 만든 비밀번호)을 만들고 web 경유로 태그·알람을 PATCH한다.
    (site_api 출력, 계정 생성 결과). `alarm2`(알람 id, 본문)를 주면 알람 PATCH를 하나 더 한다(G2-8
    PATCH 3회 — run.py는 넘기지 않는다)."""
    created = host.run(
        as_root(
            "umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(24))' "
            f"> {ADMIN_PASSWORD} && cid={compose_core()} && "
            f'docker exec -i "$cid" pureiron-core users add {E2E_ADMIN} --role admin --json '
            f"< {ADMIN_PASSWORD}"
        ),
        check=False,
    )
    admin: dict[str, object] = {
        "rc": created.returncode,
        "stdout": created.stdout[-TAIL_CHARS:],
        "stderr": created.stderr[-TAIL_CHARS:],
    }
    if created.returncode != 0:
        return {}, admin
    host.put(paths.REPO / "tests" / "e2e" / "site" / "site_api.py", f"{REMOTE}/site_api.py")
    extra = ""
    if alarm2 is not None:
        extra = f" --alarm2 {alarm2[0]} --alarm2-body {shlex.quote(json.dumps(alarm2[1]))}"
    try:
        out = host.json(
            f"sudo python3 {REMOTE}/site_api.py patch --username {E2E_ADMIN} "
            f"--password-file {ADMIN_PASSWORD} --tag {PATCH_TAG} "
            f"--tag-body {shlex.quote(json.dumps(PATCH_TAG_BODY))} --alarm {PATCH_ALARM} "
            f"--alarm-body {shlex.quote(json.dumps(PATCH_ALARM_BODY))}{extra}"
        )
    except (*REMOTE_ERRORS, ValueError) as exc:
        return {"error": str(exc)[-TAIL_CHARS:]}, admin
    return out, admin


def site_online(ctx: Context, host: Vm, ca: Path, ca_sha256: str) -> dict[str, object]:
    """VM을 온라인 현장으로: CA를 OS 신뢰 저장소에(D8, 지문은 VM에서 다시 잰다), `updater.env`에
    중앙 URL·poll 주기, VM enrollment로 등록(WSL CLI) → 현장 token을 secret 파일에(stdin으로만 —
    파일·로그·결과에 남기지 않는다), Updater 재시작. 결과 기록용 dict(비밀값 없음)."""
    host.put(ca, f"{REMOTE}/central-root.crt")
    installed = host.run(
        as_root(
            f"install -m 0644 {REMOTE}/central-root.crt {CA_TRUST} && "
            "update-ca-certificates >/dev/null && "
            f"openssl x509 -in {CA_TRUST} -noout -fingerprint -sha256"
        )
    ).stdout
    host.run(
        as_root(
            f"sed -i '/^PUREIRON_CONTROL_API=/d;/^PUREIRON_POLL_INTERVAL_S=/d' {UPDATER_ENV} && "
            f"printf 'PUREIRON_CONTROL_API=%s\\nPUREIRON_POLL_INTERVAL_S=%s\\n' "
            f"{CENTRAL_URL} {CENTRAL_POLL_S} >> {UPDATER_ENV} && "
            f"install -o pureiron -m 0644 {STATE_DIR}/enrollment.json {ENROLLMENT_REMOTE}"
        )
    )
    enrollment = ctx.work / "online" / "enrollment.json"
    host.get(ENROLLMENT_REMOTE, enrollment)
    registered = buildhost.central("register", "--enrollment", golden.wsl_path(enrollment))
    token = buildhost.site_token(ctx.site.site_id)
    host.run("sudo pureiron-site secret set control_api_token", input=token.strip() + "\n")
    del token
    host.run("sudo systemctl restart pureiron-updater")
    env = host.run(f"sudo grep -E '^PUREIRON_(CONTROL_API|POLL_INTERVAL_S)=' {UPDATER_ENV}").stdout
    return {
        "vm_ca_sha256": judges.ca_fingerprint(installed),
        "ca_sha256": ca_sha256,
        "registered": registered,
        "updater_env": env.strip().splitlines(),
    }


# ---------------------------------------------------------------- G2 시뮬레이터·실시간 값(M6-2)

SIM_CONTAINER = "e2e-simulator"
"""VM 안 modbus 시뮬레이터 컨테이너 — 제품 compose 프로젝트(`pureiron`) 밖이라 probe·Updater가
보지 않는다(E3)."""
SIM_PORT = 502
SIM_REMOTE = f"{REMOTE}/simulator.tar"
SIM_START_TIMEOUT_S = 600
E2E_VIEWER = "e2e-viewer"
VIEWER_PASSWORD = f"{REMOTE}/e2e-viewer.pw"
"""VM에서 만든 e2e viewer 비밀번호(root 0600, 호스트로 나가지 않는다)."""
E2E_ENGINEER = "e2e-engineer"
ENGINEER_PASSWORD = f"{REMOTE}/e2e-engineer.pw"
"""G2-W 계정(engineer — 선언 role operator 이상이라 쓸 수 있고, `GET /audit`(engineer 이상)도
읽는다)의 비밀번호(VM 안 root 0600)."""
LIVE_POLL_S = 10


def start_simulator(host: Vm, tar: Path, image: str) -> None:
    """시뮬레이터 이미지 tar를 VM에 적재하고 host network로 띄운다(`--restart unless-stopped` —
    재부팅·체크포인트 복원 뒤에도 살아난다). 포트 502(< 1024)를 열어야 해서 root로 돈다(이미지 기본
    사용자 10001은 host network에서 그 포트를 열 수 없다). 장비 주소는 seed의 dummy 인터페이스
    (`golden.seed_files(dummy=…)`)가 VM에 붙인다."""
    host.put(tar, SIM_REMOTE)
    host.run(
        as_root(
            f"docker load -q -i {SIM_REMOTE} && rm -f {SIM_REMOTE} && "
            f"{{ docker rm -f {SIM_CONTAINER} >/dev/null 2>&1 || true; }} && "
            f"docker run -d --name {SIM_CONTAINER} --restart unless-stopped --network host "
            f"--user 0:0 -e SIM_PORT={SIM_PORT} {image}"
        ),
        timeout_s=SIM_START_TIMEOUT_S,
    )


def add_user(host: Vm, username: str, role: str, password_file: str) -> dict[str, object]:
    """e2e 계정(VM에서 만든 비밀번호 — root 0600 파일, 호스트로 나가지 않는다)을 만든다. 결과
    기록용 dict."""
    created = host.run(
        as_root(
            "umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(24))' "
            f"> {password_file} && cid={compose_core()} && "
            f'docker exec -i "$cid" pureiron-core users add {username} --role {role} --json '
            f"< {password_file}"
        ),
        check=False,
    )
    return {
        "rc": created.returncode,
        "stdout": created.stdout[-TAIL_CHARS:],
        "stderr": created.stderr[-TAIL_CHARS:],
    }


def add_viewer(host: Vm) -> dict[str, object]:
    """e2e viewer(태그·장비 읽기 전용 계정)."""
    return add_user(host, E2E_VIEWER, "viewer", VIEWER_PASSWORD)


def live(host: Vm) -> dict[str, object]:
    """web 경유 viewer 로그인 → `GET /tags`·`GET /devices`(`site/site_api.py live`)."""
    return host.json(
        f"sudo python3 {REMOTE}/site_api.py live --username {E2E_VIEWER} "
        f"--password-file {VIEWER_PASSWORD}"
    )


def wait_live(
    read: Callable[[], dict[str, object]],
    judge: Callable[[dict[str, object]], dict[str, bool]],
    timeout_s: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    started: float | None = None,
) -> tuple[dict[str, object], dict[str, bool], float]:
    """`judge`가 모두 참이 될 때까지 `read`(보통 `live(host)`)를 다시 읽는다. (마지막 응답, 판정,
    `started`부터 걸린 초) — 시간이 다 되면 마지막 판정 그대로다. 읽기 실패(ssh·원격 명령·JSON)는
    빈 응답으로 친다. `started`(같은 `clock` 기준 시각, 기본은 지금)를 주면 시간 제한을 그때부터
    잰다(G2-1: c0부터 5분). 시간이 이미 지났어도 한 번은 읽는다."""
    started = clock() if started is None else started
    reply: dict[str, object] = {}
    checks = judge(reply)
    tried = False
    while clock() - started < timeout_s or not tried:
        tried = True
        try:
            reply = read()
        except (*REMOTE_ERRORS, ValueError, subprocess.TimeoutExpired):
            reply = {}
        checks = judge(reply)
        if all(checks.values()):
            break
        sleep(LIVE_POLL_S)
    return reply, checks, round(clock() - started, 1)


def write_tag(host: Vm, tag: str, value: float, reason: str) -> dict[str, object]:
    """web 경유 engineer 로그인 → `POST /writes`(prepare) → 200이면 `POST /writes/{id}/confirm` →
    이 사유의 감사 행(`site/site_api.py write`, G2-W)."""
    return host.json(
        f"sudo python3 {REMOTE}/site_api.py write --username {E2E_ENGINEER} "
        f"--password-file {ENGINEER_PASSWORD} --tag {shlex.quote(tag)} --value {value!r} "
        f"--reason {shlex.quote(reason)}"
    )


# ---------------------------------------------------------------- 중앙 응답 필터(G2-6 ①·G2-9)

FLEET_NFT = "e2e_fleet"
"""fleet 시나리오가 잠시 두는 nft 표 — 외부 접속 카운터(`e2e_audit`)와 따로다."""
CENTRAL_HOST = "10.77.0.1"
CENTRAL_PORT = 28443
"""중앙 Caddy 주소(`CENTRAL_URL`과 같다) — 필터가 이 출발지의 수신만 건드린다(ssh는 그대로)."""
CENTRAL_IN = "central_in"
"""중앙(10.77.0.1:28443) → VM 수신을 세는 규칙 comment."""
UPDATER_BUNDLES = "/var/lib/pureiron/bundles"


def nft_apply(host: Vm, script: str) -> None:
    """`nft -f -`(표 하나를 통째로 바꾸는 스크립트 — `fleet_scenarios.central_filter`)."""
    host.run("sudo nft -f -", input=script)


def central_in_bytes(host: Vm) -> int:
    """`e2e_fleet` 표의 `central_in` 카운터 바이트(IP 패킷 길이 합). 없으면 -1."""
    out = host.run(f"sudo nft -j list table inet {FLEET_NFT}").stdout
    for entry in cast(list[dict[str, object]], json.loads(out)["nftables"]):
        rule = cast(dict[str, object] | None, entry.get("rule"))
        if rule is None or rule.get("comment") != CENTRAL_IN:
            continue
        for expr in cast(list[dict[str, object]], rule.get("expr", [])):
            counter = cast(dict[str, int] | None, expr.get("counter"))
            if counter is not None:
                return counter["bytes"]
    return -1


def _stdout_or_ssh_error(host: Vm, command: str) -> str:
    """`check=False` 원격 명령의 stdout — 다만 ssh 자체 실패(255)는 `VmError`(infra)다. 그대로
    두면 "파일 없음·0줄"로 읽혀 제품 판정이 거짓이 된다(M6-4b-1 리뷰 L1)."""
    done = host.run(command, check=False)
    if done.returncode == SSH_FAILED:
        raise VmError(f"ssh {host.ip} failed (255): {command}: {done.stderr.strip()[-200:]}")
    return done.stdout.strip()


def part_bytes(host: Vm) -> int | None:
    """Updater가 받는 중인 번들(`bundles/poll-*.pib.part`) 크기. 없으면 None. ssh 실패는
    `VmError`."""
    out = _stdout_or_ssh_error(
        host, f"sudo sh -c 'stat -c %s {UPDATER_BUNDLES}/poll-*.pib.part 2>/dev/null | head -1'"
    )
    return int(out) if out.isdigit() else None


def poll_failures(host: Vm, since_epoch: float) -> int:
    """`since_epoch`(VM 시각) 뒤 Updater 로그의 `poll failed` 줄 수(끊긴 내려받기·중앙 무응답).
    ssh 실패는 `VmError`."""
    out = _stdout_or_ssh_error(
        host,
        f"sudo journalctl -u pureiron-updater --since @{int(since_epoch)} --no-pager -o cat "
        "| grep -c 'poll failed'",
    )
    return int(out) if out.isdigit() else 0
