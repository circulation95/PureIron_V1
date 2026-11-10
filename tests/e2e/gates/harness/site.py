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
from typing import cast

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
from harness.vm import Vm, VmError

REMOTE = "/home/pureiron/e2e"
SITE_SCRIPTS = ("crash_at.py", "probe.py", "site_api.py")
"""VM에 복사하는 스크립트(`tests/e2e/site/`)."""
STATE_DIR = "/var/lib/pureiron/state"
INCOMING = "/home/pureiron/incoming"
CONVERGE_S = 600
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


def wait_ready(host: Vm, timeout_s: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    snap: dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            snap = probe(host)
        except (VmError, subprocess.TimeoutExpired):
            time.sleep(5)
            continue
        if judges.ready(snap):
            return snap
        time.sleep(5)
    return snap


# ---------------------------------------------------------------- 외부 접속 카운터


def nft_counters(host: Vm) -> dict[str, int]:
    out = host.run("sudo nft -j list table inet e2e_audit").stdout
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


def stage(host: Vm, pib: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    host.put(pib, f"{INCOMING}/{pib.name}")
    result = subprocess.CompletedProcess[str]([], 2, "", "busy")
    for _ in range(30):  # 서비스 tick이 잠금을 잡고 있으면 exit 2 — 잠시 뒤 다시
        result = host.run(f"sudo pureiron-site apply {INCOMING}/{pib.name}", check=False)
        if result.returncode != 2:
            break
        time.sleep(2)
    if check and result.returncode != 0:
        raise E2EError(f"apply {pib.name} exited {result.returncode}: {result.stderr[-800:]}")
    return result


def converge(host: Vm, gen: int, timeout_s: float = CONVERGE_S) -> tuple[dict[str, object], float]:
    """시도가 끝나(attempt 없음) `last_attempted_gen == gen`이고 ready가 될 때까지."""
    started = time.monotonic()
    snap: dict[str, object] = {}
    while time.monotonic() - started < timeout_s:
        try:
            snap = probe(host)
        except (VmError, subprocess.TimeoutExpired):
            time.sleep(5)
            continue
        state = updater_state(snap)
        done = state.get("attempt") is None and state.get("last_attempted_gen") == gen
        if done and state.get("last_result") in ("succeeded", "failed") and judges.ready(snap):
            return snap, time.monotonic() - started
        time.sleep(5)
    return snap, time.monotonic() - started


def report_for(host: Vm, pib: Path) -> dict[str, object]:
    """그 번들의 최근 보고. 파일 이름 `<started>-g<gen>-<sha256 12자>.json`에서 sha로 찾는다 —
    커밋 전 거부는 gen을 믿지 않아 `g unknown`이므로 gen으로는 못 찾는다."""
    with pib.open("rb") as f:
        sha12 = hashlib.file_digest(f, "sha256").hexdigest()[:12]
    out = host.run(
        f"sudo sh -c 'ls -t /var/lib/pureiron/state/reports/*-{sha12}.json "
        f"/var/lib/pureiron/state/reports/exported/*-{sha12}.json 2>/dev/null | head -1 | "
        "xargs -r cat'",
        check=False,
    ).stdout
    return cast(dict[str, object], json.loads(out)) if out.strip() else {}


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


def site_patch(host: Vm) -> tuple[dict[str, object], dict[str, object]]:
    """e2e admin(CLI 전용 계정, VM에서 만든 비밀번호)을 만들고 web 경유로 태그·알람을 PATCH한다.
    (site_api 출력, 계정 생성 결과)."""
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
    try:
        out = host.json(
            f"sudo python3 {REMOTE}/site_api.py patch --username {E2E_ADMIN} "
            f"--password-file {ADMIN_PASSWORD} --tag {PATCH_TAG} "
            f"--tag-body {shlex.quote(json.dumps(PATCH_TAG_BODY))} --alarm {PATCH_ALARM} "
            f"--alarm-body {shlex.quote(json.dumps(PATCH_ALARM_BODY))}"
        )
    except (VmError, ValueError) as exc:
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
    enrollment = paths.BUNDLES / ctx.run_id / "online" / "enrollment.json"
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
