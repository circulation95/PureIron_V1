"""G2 스키마 자동 판정(M6-3a I4, D9·H8) — 기대 스키마는 WSL의 빈 DB에 NEXT를 migrate한 뒤 VM
probe와 같은 SQL로 읽고, 판정 ⓐ(업그레이드 뒤 == 기대)·ⓑ(c0 ⊂ 업그레이드 뒤)·ⓒ(새 설치 c0 ==
기대)는 이름 집합 5종을 정확히 비교한다."""

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import probe
import pytest
from g2_fakes import C0_GOOD, session
from harness import buildhost, bundles, g2, judges
from harness.context import E2EError
from harness.release_pair import nightly

SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
EXPECTED: dict[str, object] = {
    "epoch": "5",
    "tables": "alarm_journal,alarms,audit_log,devices,project_files,tag_history,tags,users",
    "columns": "devices.definition,devices.id,tags.definition,tags.path,users.name",
    "indexes": "audit_log_time_idx,sessions_user_id_idx",
    "caggs": "tag_history_1h,tag_history_1m",
    "realtime_caggs": "tag_history_1h,tag_history_1m",
}


def with_name(key: str, extra: str) -> dict[str, object]:
    return {**EXPECTED, key: f"{EXPECTED[key]},{extra}"}


def without_first(key: str) -> dict[str, object]:
    return {**EXPECTED, key: ",".join(str(EXPECTED[key]).split(",")[1:])}


def test_schema_sets_are_the_probe_keys_and_the_sql_lives_only_in_probe() -> None:
    assert judges.SCHEMA_SETS == probe.SCHEMA_KEYS
    assert ("epoch", *probe.SCHEMA_KEYS) == bundles.EXPECTED_KEYS
    source = inspect.getsource(bundles)
    for sql in (probe.DB_SQL[k] for k in bundles.EXPECTED_KEYS):
        assert sql not in source  # 기대 스키마도 probe.DB_SQL을 그대로 쓴다
    assert "information_schema" not in source


# ---------------------------------------------------------------- ⓐ·ⓒ 정확히 같음


def test_same_schema_passes_every_item() -> None:
    checks = judges.judge_schema_equal(dict(EXPECTED), EXPECTED)
    assert sorted(checks) == sorted([f"schema_{k}" for k in judges.SCHEMA_SETS] + ["schema_epoch"])
    assert all(checks.values())
    assert judges.schema_diff(dict(EXPECTED), EXPECTED) == {}


@pytest.mark.parametrize("key", judges.SCHEMA_SETS)
def test_an_extra_name_in_any_set_fails_equality(key: str) -> None:
    """⊇로 느슨해지면 안 된다 — 기대에 없는 이름 하나도 실패다."""
    checks = judges.judge_schema_equal(with_name(key, "zz_extra"), EXPECTED)
    assert [k for k, ok in checks.items() if not ok] == [f"schema_{key}"]
    diff = judges.schema_diff(with_name(key, "zz_extra"), EXPECTED)
    assert diff == {key: {"extra": ["zz_extra"], "missing": []}}


@pytest.mark.parametrize("key", judges.SCHEMA_SETS)
def test_a_missing_name_in_any_set_fails_equality(key: str) -> None:
    checks = judges.judge_schema_equal(without_first(key), EXPECTED)
    assert [k for k, ok in checks.items() if not ok] == [f"schema_{key}"]


def test_epoch_and_unreadable_expectations_fail() -> None:
    assert judges.judge_schema_equal({**EXPECTED, "epoch": "4"}, EXPECTED)["schema_epoch"] is False
    empty = judges.judge_schema_equal(dict(EXPECTED), {})
    assert not any(empty.values())
    broken = judges.judge_schema_equal({**EXPECTED, "tables": {"error": "x"}}, EXPECTED)
    assert broken["schema_tables"] is False


# ---------------------------------------------------------------- ⓑ c0 ⊂ 업그레이드 뒤


def test_upgrade_may_add_but_never_drop_names() -> None:
    assert all(judges.judge_schema_kept(EXPECTED, with_name("tables", "new_table")).values())


@pytest.mark.parametrize("key", judges.SCHEMA_SETS)
def test_a_name_dropped_by_the_upgrade_fails(key: str) -> None:
    checks = judges.judge_schema_kept(EXPECTED, without_first(key))
    assert [k for k, ok in checks.items() if not ok] == [f"kept_{key}"]


def test_unreadable_c0_schema_is_not_kept() -> None:
    assert not any(judges.judge_schema_kept({}, EXPECTED).values())


# ---------------------------------------------------------------- ⓒ G2-1 site-b


def install_check(tmp_path: Path, site_id: str, c0: dict[str, object]) -> dict[str, bool]:
    s = session(tmp_path)
    s.expected_schema = dict(EXPECTED)
    run = s.sites[site_id]
    run.c0_db = {**C0_GOOD, **c0}
    run.live = g2.LiveWatch({}, {"tags_good": True}, 1.0)
    run.ctx.digests = {run.release: {}}
    return g2.check_install(
        s, site_id, probe=lambda _: {"readyz": {"status": 200}}, mark=lambda ctx, host: None
    )


def test_fresh_install_site_b_c0_must_equal_the_expected_schema(tmp_path: Path) -> None:
    good = install_check(tmp_path, "site-b", EXPECTED)
    assert all(v for k, v in good.items() if k.startswith("schema_"))
    assert len([k for k in good if k.startswith("schema_")]) == 6
    bad = install_check(tmp_path, "site-b", with_name("indexes", "stray_idx"))
    assert bad["schema_indexes"] is False


def test_site_a_c0_is_base_and_not_compared_with_next(tmp_path: Path) -> None:
    checks = install_check(tmp_path, "site-a", with_name("tables", "base_only"))
    assert not [k for k in checks if k.startswith("schema_")]


# ---------------------------------------------------------------- WSL 기대 스키마


class Docker:
    """`bundles.expected_schema`의 docker 가짜 — 호출을 남기고 미리 정한 답을 낸다."""

    def __init__(
        self,
        *,
        migrate_rc: int = 0,
        ready_after: int = 1,
        left: tuple[str, ...] = (),
        nets: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.migrate_rc = migrate_rc
        self.ready_after = ready_after
        self.left = left
        self.nets = nets

    def __call__(self, args: Sequence[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        cmd = tuple(args)
        self.calls.append(cmd)
        out, rc = "", 0
        if "pg_isready" in cmd:
            self.ready_after -= 1
            rc = 0 if self.ready_after <= 0 else 2
        elif cmd[-2:] == ("migrate", "up"):
            rc = self.migrate_rc
            out = "migrate up: failed" if rc else "migrate up: applied 13"
        elif cmd[:2] == ("ps", "-a"):
            out = "\n".join(self.left)
        elif cmd[:2] == ("network", "ls"):
            out = "\n".join(self.nets)
        elif "psql" in cmd:
            key = next(k for k in bundles.EXPECTED_KEYS if probe.DB_SQL[k] == cmd[-1])
            out = f"{EXPECTED[key]}\n"
        return subprocess.CompletedProcess(list(cmd), rc, out, "")


def work_dir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    (work / "releases").mkdir(parents=True)
    record = {
        "images": {
            "core": {"tag": "pureiron/core:n1a2b3c4"},
            "site-db": {"tag": "pureiron/site-db:2.30.2-pg16"},
        }
    }
    (work / "releases" / "n1a2b3c4.json").write_text(json.dumps(record), "utf-8")
    return work


def run_schema(
    tmp_path: Path,
    docker: Docker,
    docker_ok: Callable[[], bool] = lambda: True,
    alive: Callable[[int], bool] = lambda pid: False,
) -> dict[str, object]:
    clock = iter(float(i) for i in range(10_000))
    return bundles.expected_schema(
        work_dir(tmp_path),
        "n1a2b3c4",
        tmp_path / "out" / "expected-schema.json",
        docker=docker,
        docker_ok=docker_ok,
        sleep=lambda _: None,
        clock=lambda: next(clock),
        alive=alive,
    )


def test_expected_schema_migrates_an_empty_db_reads_it_and_removes_it(tmp_path: Path) -> None:
    docker = Docker(ready_after=3)
    result = run_schema(tmp_path, docker)
    assert result["ok"] is True
    assert result["schema"] == EXPECTED
    saved = json.loads((tmp_path / "out" / "expected-schema.json").read_text("utf-8"))
    assert saved == EXPECTED
    verbs = [c[0] if c[0] != "network" else f"network {c[1]}" for c in docker.calls]
    assert verbs.index("network create") < verbs.index("run")
    migrate = next(c for c in docker.calls if c[-2:] == ("migrate", "up"))
    assert "pureiron/core:n1a2b3c4" in migrate
    assert any("PUREIRON_DB_DSN=postgresql://pureiron@e2e-schema-db-" in a for a in migrate)
    assert docker.calls[-2][:2] == ("rm", "-f")
    assert docker.calls[-1][:2] == ("network", "rm")
    for call in docker.calls:  # 호스트 포트를 열지 않는다(같은 PC의 CI가 25432·25433을 쓴다)
        assert "-p" not in call
        assert "--publish" not in call
    started = next(c for c in docker.calls if c[:2] == ("run", "-d"))
    assert "pureiron/site-db:2.30.2-pg16" in started
    assert started[4:6] == ("--label", bundles.SCHEMA_LABEL)


def test_start_cleanup_leaves_the_temporary_db_of_another_running_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3a 리뷰 L4·3b 리뷰 L5·변이: 시작 정리가 표지 붙은 것을 모두 지우면 같은 PC에서 동시에 도는
    다른 expected-schema의 DB를 지운다 — 이 배포판이 만들었고 만든 프로세스가 끝난 것(이름 끝
    배포판·pid)만 지운다. 다른 배포판의 pid는 여기서 볼 수 없어 남긴다."""
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    docker = Docker(
        left=(
            "e2e-schema-db-n1-Ubuntu-111",
            "e2e-schema-migrate-n1-Ubuntu-111",
            "e2e-schema-db-n2-Ubuntu-222",
            "e2e-schema-db-n3-Other-111",
            "x",
        ),
        nets=("e2e-schema-n1-Ubuntu-111", "e2e-schema-n2-Ubuntu-222", "e2e-schema-n3-Other-111"),
    )
    run_schema(tmp_path, docker, alive=lambda pid: pid == 222)
    removed = [c for c in docker.calls if c[:2] == ("rm", "-f")]
    assert removed[0] == (
        "rm",
        "-f",
        "e2e-schema-db-n1-Ubuntu-111",
        "e2e-schema-migrate-n1-Ubuntu-111",
    )
    assert ("network", "rm", "e2e-schema-n1-Ubuntu-111") in docker.calls
    for kept in ("n2-Ubuntu-222", "n3-Other-111"):
        assert not any(
            any(kept in a for a in c)
            for c in docker.calls
            if c[0] in ("rm", "network") and c[1] in ("-f", "rm")
        )


def test_temporary_names_carry_the_distro(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    assert bundles.distro_tag() == "Ubuntu_22.04"
    docker = Docker()
    run_schema(tmp_path, docker)
    started = next(c for c in docker.calls if c[:2] == ("run", "-d"))
    assert started[started.index("--name") + 1].startswith("e2e-schema-db-n1a2b3c4-Ubuntu_22.04-")


def test_the_migrate_client_is_named_and_removed_with_the_db(tmp_path: Path) -> None:
    """migrate 시간 초과 때 클라이언트 컨테이너만 남아 `network rm`이 실패하지 않게 이름을 붙여 같이
    지운다."""
    docker = Docker()
    run_schema(tmp_path, docker)
    migrate = next(c for c in docker.calls if c[-2:] == ("migrate", "up"))
    name = migrate[migrate.index("--name") + 1]
    assert name.startswith("e2e-schema-migrate-n1a2b3c4-")
    assert name in docker.calls[-2]


def test_next_that_does_not_migrate_is_a_product_result(tmp_path: Path) -> None:
    docker = Docker(migrate_rc=1)
    result = run_schema(tmp_path, docker)
    assert (result["ok"], result["stage"], result["rc"]) == (False, "migrate", 1)
    assert "migrate up: failed" in str(result["tail"])
    assert docker.calls[-1][:2] == ("network", "rm")
    assert not (tmp_path / "out" / "expected-schema.json").exists()


def test_docker_silence_and_a_db_that_never_answers_are_infra(tmp_path: Path) -> None:
    docker = Docker(migrate_rc=1)
    with pytest.raises(SystemExit) as raised:
        run_schema(tmp_path, docker, docker_ok=lambda: False)
    assert raised.value.code == bundles.INFRA_EXIT
    assert docker.calls[-1][:2] == ("network", "rm")
    never = Docker(ready_after=10**9)
    with pytest.raises(SystemExit) as raised:
        run_schema(tmp_path / "b", never)
    assert raised.value.code == bundles.INFRA_EXIT
    assert never.calls[-2][:2] == ("rm", "-f")


def test_windows_side_keeps_the_schema_and_classifies_failures(tmp_path: Path) -> None:
    pair = nightly({"r5": {"version": "2026.11.1", "schema": {"epoch": 5}}}, {"r5"}, SHA, 5)

    def wsl(reply: dict[str, object]) -> Callable[..., str]:
        def run(script: str, *, timeout_s: float) -> str:
            assert "expected-schema --work" in script
            assert "--next n1a2b3c4" in script
            assert timeout_s == buildhost.EXPECTED_SCHEMA_TIMEOUT_S
            return json.dumps(reply) + "\n"

        return run

    out = tmp_path / "expected-schema.json"
    got = buildhost.expected_schema(out, pair, run_wsl=wsl({"ok": True, "schema": EXPECTED}))
    assert got == EXPECTED
    with pytest.raises(buildhost.SchemaBuildError, match="does not migrate") as raised:
        buildhost.expected_schema(out, pair, run_wsl=wsl({"ok": False, "stage": "migrate"}))
    assert isinstance(raised.value, E2EError)
    with pytest.raises(E2EError, match="epoch"):
        buildhost.expected_schema(
            out, pair, run_wsl=wsl({"ok": True, "schema": {**EXPECTED, "epoch": "6"}})
        )
