"""G2-5 거부(M6-3b J1) — 거부 재료(가짜 스키마 기록 epoch + 2·인증받지 않은 release key)·변조 도구,
거부 판정 6항목, 흐름(c0 거부 6건 → E_CERT 재료를 g2보다 먼저 → NEXT g2 → E_CERT·E_GEN), 설치 전
변조
탐침(사본만 바꾸고 runbook을 따라간 뒤 지운다, site-b만)을 VM·WSL 없이 고정한다."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import tamper
from g2_fakes import digests, scenario_session, snap, tree_expect
from harness import buildhost, bundles, g2, judges, preinstall, rejects, scenarios
from harness.buildhost import RejectMaterials
from harness.context import Context, E2EError, InfraError
from harness.release_pair import ReleasePair
from harness.vm import RemoteCommandError, Vm, VmError

from pureiron_bundle import keys, trust
from pureiron_bundle.errors import BundleRejectedError, RejectCode

NOW = datetime(2026, 11, 15, 18, 17, tzinfo=UTC)
RUNBOOK_SHA = "b" * 64


# ---------------------------------------------------------------- 변조 도구(site/tamper.py)


def make_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_flip_changes_one_byte_in_the_middle_of_the_matching_member(tmp_path: Path) -> None:
    pib = tmp_path / "x.pib"
    payload = bytes(range(200))
    make_tar(pib, {"manifest.json": b"{}", "images/core.tar.gz": payload, "z": b"tail"})
    before = pib.read_bytes()
    out = tamper.flip(pib, "images/core.tar*")
    after = pib.read_bytes()
    diff = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert diff == [out["offset"]]
    assert out["member"] == "images/core.tar.gz"
    assert out["size"] == 200
    with tarfile.open(pib) as tar:  # 헤더·크기는 그대로, 데이터 가운데 한 바이트만
        member = tar.getmember("images/core.tar.gz")
        data = tar.extractfile(member)
        assert data is not None
        got = data.read()
    assert got[100] == payload[100] ^ 0xFF
    assert got[:100] == payload[:100]
    assert got[101:] == payload[101:]


def test_flip_without_a_matching_member_changes_nothing(tmp_path: Path) -> None:
    pib = tmp_path / "x.pib"
    make_tar(pib, {"manifest.json": b"{}"})
    before = pib.read_bytes()
    with pytest.raises(tamper.TamperError):
        tamper.flip(pib, "debs/*.deb")
    assert pib.read_bytes() == before
    assert tamper.main([str(pib), "debs/*.deb"]) == 1


# ---------------------------------------------------------------- 거부 재료(WSL bundles.py)

NEXT_RECORD: dict[str, object] = {
    "name": "n1a2b3c4",
    "release": "n1a2b3c4",
    "version": "2026.11.2-nightly.1a2b3c4",
    "commit": "1a2b3c4" + "0" * 33,
    "images": {
        svc: {"tag": f"pureiron/{svc}:n1a2b3c4", "config_digest": f"sha256:{svc}"}
        for svc in ("core", "collector", "web", "site-db")
    },
    "schema": {"epoch": 5, "app_expected": 5, "app_max_compatible": 6},
}


def test_schema_jump_record_keeps_next_images_and_jumps_two_epochs() -> None:
    """변이: epoch + 1이면 현장(NEXT epoch)이 MIGRATE로 받아들여 E_SCHEMA를 못 본다."""
    record = bundles.schema_jump_record(NEXT_RECORD)
    assert record["schema"] == {"epoch": 7, "app_expected": 7, "app_max_compatible": 8}
    assert record["images"] == NEXT_RECORD["images"]
    assert record["name"] == "n1a2b3c4s"
    assert record["version"] == "2026.11.2-nightly.1a2b3c4.fs"
    plain = bundles.schema_jump_record({**NEXT_RECORD, "version": "2026.11.1"})
    assert plain["version"] == "2026.11.1-fs"  # 접미사 없는 버전도 fleet 형식 안


def copy_link(link: Path, target: Path) -> None:
    shutil.copyfile(target, link)


def test_reject_materials_sign_with_a_key_the_real_root_did_not_certify(tmp_path: Path) -> None:
    work = tmp_path / "w"
    (work / "releases").mkdir(parents=True)
    (work / "releases" / "n1a2b3c4.json").write_text(json.dumps(NEXT_RECORD), "utf-8")
    store = work / "store" / "n1a2b3c4"
    store.mkdir(parents=True)
    for svc in ("core", "collector", "web", "site-db"):
        (store / f"{svc}.tar").write_bytes(svc.encode())
    (work / bundles.REJECTS / "stale").mkdir(parents=True)  # 지난 재료는 지운다
    out = bundles.reject_materials(work, "n1a2b3c4", now=NOW, link=copy_link)
    rogue = cast(dict[str, object], out["rogue"])
    cert_dir = Path(str(rogue["cert_dir"]))
    cert_json, cert_sig = (
        (cert_dir / "release-cert.json").read_bytes(),
        (cert_dir / "release-cert.sig").read_bytes(),
    )
    rogue_root = keys.read_public_key(Path(str(rogue["root_pub"])))
    cert = trust.verify_cert(rogue_root, cert_json, cert_sig)  # 빌더 자체 검증은 통과
    release_seed = keys.read_private_key(Path(str(rogue["release_key"])))
    assert cert.release_pubkey == keys.public_key(release_seed)
    real_root = keys.public_key(keys.generate_seed())
    with pytest.raises(BundleRejectedError) as rejected:  # 현장의 진짜 root로는 E_SIG
        trust.verify_cert(real_root, cert_json, cert_sig)
    assert rejected.value.code is RejectCode.E_SIG
    assert cert.not_before < NOW < cert.not_after
    assert not (work / bundles.REJECTS / "stale").exists()
    schema = cast(dict[str, object], out["schema"])
    assert schema["epoch"] == 7
    record = json.loads((Path(str(schema["releases"])) / "n1a2b3c4s.json").read_text("utf-8"))
    assert record["version"] == schema["version"] == "2026.11.2-nightly.1a2b3c4.fs"
    jumped = Path(str(schema["store"])) / "n1a2b3c4s"
    assert sorted(p.name for p in jumped.iterdir()) == [
        "collector.tar",
        "core.tar",
        "site-db.tar",
        "web.tar",
    ]
    assert (jumped / "core.tar").read_bytes() == b"core"


def materials_line(epoch: int) -> str:
    return json.dumps(
        {
            "ok": True,
            "rogue": {
                "release_key": "/w/r/release.key",
                "cert_dir": "/w/r",
                "root_pub": "/w/r/root.pub",
            },
            "schema": {"version": "v.fs", "epoch": epoch, "releases": "/w/rel", "store": "/w/st"},
        }
    )


def test_buildhost_materials_refuse_a_record_that_is_not_two_epochs_ahead(tmp_path: Path) -> None:
    pair = scenario_session(tmp_path).pair
    scripts: list[str] = []

    def run_wsl(script: str, **_: object) -> str:
        scripts.append(script)
        return materials_line(pair.epochs[pair.next] + 2)

    mats = buildhost.reject_materials(pair, run_wsl=run_wsl)
    assert f"reject-materials --work {buildhost.NIGHTLY_WORK} --next {pair.next}" in scripts[0]
    assert mats.signing_options() == (
        *("--release-key", "/w/r/release.key", "--cert-dir", "/w/r"),
        *("--root-pub", "/w/r/root.pub"),
    )
    assert mats.schema_options() == ("--releases", "/w/rel", "--store", "/w/st")

    def one_epoch(script: str, **_: object) -> str:
        return materials_line(pair.epochs[pair.next] + 1)

    with pytest.raises(E2EError, match="epoch"):
        buildhost.reject_materials(pair, run_wsl=one_epoch)


def test_build_variant_names_the_bundle_by_label_and_passes_options(tmp_path: Path) -> None:
    session = scenario_session(tmp_path)
    ctx = session.sites["site-b"].ctx
    scripts: list[str] = []

    def run_wsl(script: str, **_: object) -> str:
        scripts.append(script)
        out = ctx.work / "variants"
        out.mkdir(parents=True, exist_ok=True)
        (out / "pureiron-site-x-g2.pib").write_bytes(b"pib")
        return ""

    pib = buildhost.build_variant(
        ctx,
        2,
        "2026.11.2",
        NOW,
        "/mnt/c/tree",
        label="other-site",
        site_id="site-x",
        options=("--min-updater", "1.0.0"),
        run_wsl=run_wsl,
    )
    assert pib.name == "pureiron-site-x-g2-other-site.pib"
    assert pib.is_file()
    assert "--site site-x --project-dir '/mnt/c/tree' --min-updater 1.0.0" in scripts[0]
    assert f"site --work {buildhost.NIGHTLY_WORK}" in scripts[0]


def test_min_updater_is_above_the_installed_one() -> None:
    assert rejects.above("0.2.0\n") == "1.0.0"
    with pytest.raises(E2EError):
        rejects.above("pureiron-site")


# ---------------------------------------------------------------- 거부 판정


def probe_snap(
    *, started: object = "t0", applied: object = 1, locked: bool = False
) -> dict[str, object]:
    return {
        "updater": {"applied_gen": applied},
        "containers": {
            "core": {"image_id": "c", "running": True, "started_at": started},
            "web": {"image_id": "w", "running": True},
        },
        "project": {"status": 200, "body": {"edit_locked": locked}},
    }


WANT = {"core": "c", "web": "w"}


def rejected(
    *,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    report: dict[str, object] | None = None,
    exit_code: int = 1,
) -> list[str]:
    checks = judges.judge_rejected(
        probe_snap() if before is None else before,
        probe_snap() if after is None else after,
        {"code": "E_SIG"} if report is None else report,
        exit_code=exit_code,
        want_exit=1,
        want_code="E_SIG",
        want_digests=WANT,
    )
    assert set(checks) == {
        "exit",
        "code",
        "core_not_restarted",
        "images_match",
        "edit_unlocked",
        "applied_gen_kept",
    }
    return scenarios.failed(checks)


def test_a_rejected_bundle_that_touched_nothing_passes_all_six() -> None:
    assert rejected() == []


@pytest.mark.parametrize(
    ("changes", "want"),
    [
        ({"exit_code": 0}, ["exit"]),
        ({"report": {"code": "E_DIGEST"}}, ["code"]),
        ({"report": {}}, ["code"]),  # 보고를 못 찾으면 거부 코드도 없다
        ({"after": probe_snap(started="t1")}, ["core_not_restarted"]),
        (
            {"before": probe_snap(started=None), "after": probe_snap(started=None)},
            ["core_not_restarted"],
        ),
        (
            {
                "after": {
                    **probe_snap(),
                    "containers": {"core": {"image_id": "x", "running": True, "started_at": "t0"}},
                }
            },
            ["images_match"],
        ),
        ({"after": probe_snap(locked=True)}, ["edit_unlocked"]),
        ({"after": probe_snap(applied=2)}, ["applied_gen_kept"]),
        (
            {"before": probe_snap(applied=None), "after": probe_snap(applied=None)},
            ["applied_gen_kept"],
        ),
    ],
)
def test_each_item_fails_on_its_own(changes: dict[str, object], want: list[str]) -> None:
    """변이: edit-lock·StartedAt·거부 코드 비교를 빼면 해당 줄이 통과로 바뀐다. 못 읽은 값(None)은
    거짓이다."""

    def snap_of(key: str) -> dict[str, object] | None:
        value = changes.get(key)
        return cast(dict[str, object], value) if isinstance(value, dict) else None

    exit_code = changes.get("exit_code", 1)
    assert (
        rejected(
            before=snap_of("before"),
            after=snap_of("after"),
            report=snap_of("report"),
            exit_code=exit_code if isinstance(exit_code, int) else 1,
        )
        == want
    )


def test_the_same_gen_no_op_wants_exit_0_and_no_report() -> None:
    checks = judges.judge_rejected(
        probe_snap(), probe_snap(), {}, exit_code=0, want_exit=0, want_code=None, want_digests=WANT
    )
    assert scenarios.failed(checks) == []


# ---------------------------------------------------------------- 흐름(site-b)

CODES = {
    "other-site": "E_SITE",
    "digest-tampered": "E_DIGEST",
    "rogue-key": "E_SIG",
    "schema-jump": "E_SCHEMA",
    "min-updater": "E_UPDATER",
    "same-gen": None,
    "before-g2": "E_CERT",
    "after-g2": "E_GEN",
}


def with_core(s: dict[str, object], release: str, started: str) -> dict[str, object]:
    """`s`의 실행 컨테이너를 `release` 이미지 + core `started_at`으로."""
    containers: dict[str, object] = {
        svc: {"image_id": d, "running": True, **({"started_at": started} if svc == "core" else {})}
        for svc, d in digests(release).items()
    }
    return {**s, "containers": containers}


class RejectFake:
    """site-b c0(NEXT g1)에서 거부 시나리오의 원격 가짜. 라벨마다 정해진 거부 코드로 끝나고 현장은
    그대로다(`touch`에 라벨을 넣으면 그 번들이 core를 다른 이미지로 재시작시킨다). NEXT g2 적용은
    NEXT 트리로 바꾼다."""

    def __init__(self, session: g2.Session) -> None:
        self.session = session
        pair = session.pair
        c0 = snap(tree_expect(session.tree), pair.next, applied=1, attempted=1, result="succeeded")
        self.current = with_core(c0, pair.next, "t0")
        self.events: list[str] = []
        self.reports: dict[str, dict[str, object]] = {}
        self.touch: set[str] = set()
        self.codes = dict(CODES)
        self.epoch = pair.epochs[pair.next] + 2

    def g2_snap(self) -> dict[str, object]:
        pair = self.session.pair
        after = snap(
            tree_expect(self.session.tree_of(pair.next)),
            pair.next,
            applied=2,
            attempted=2,
            result="succeeded",
        )
        return with_core(after, pair.next, "t1")

    def remote(self, build: rejects.Build | None = None) -> rejects.RejectRemote:
        def restore(run: g2.SiteRun) -> None:
            self.events.append(f"restore {run.ctx.site.site_id}")

        def build_g2(ctx: Context, gen: int, release: str, tree: Path) -> Path:
            self.events.append(f"apply build g{gen} {release} {tree.name}")
            return ctx.out / f"g{gen}-{release}.pib"

        def stage_g2(host: Vm, pib: Path) -> None:
            self.events.append(f"apply stage {pib.name}")
            self.current = self.g2_snap()

        def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
            return self.current, 1.5

        def build_variant(ctx: Context, variant: rejects.Variant) -> Path:
            self.events.append(f"build {variant.label} g{variant.gen} {variant.product}")
            return ctx.out / f"{variant.label}.pib"

        def tamper_copy(pib: Path) -> Path:
            self.events.append(f"tamper {pib.name}")
            return pib.with_name(f"{pib.stem}-tampered.pib")

        def stage(host: Vm, pib: Path) -> subprocess.CompletedProcess[str]:
            label = pib.stem
            self.events.append(f"stage {label}")
            code = self.codes[label]
            self.reports[pib.name] = {} if code is None else {"code": code}
            if label in self.touch:
                self.current = with_core(self.current, "other", "t9")
            return subprocess.CompletedProcess([], 0 if code is None else 1, "", "")

        def report_for(host: Vm, pib: Path) -> dict[str, object]:
            return self.reports.get(pib.name, {"code": None, "failed_manual": False})

        def materials(pair: ReleasePair) -> RejectMaterials:
            return RejectMaterials("/k", "/c", "/r", "v.fs", self.epoch, "/rel", "/st")

        base = scenarios.Remote(
            restore=restore,
            probe=lambda host: self.current,
            wait_ready=lambda host: self.current,
            build=build_g2,
            stage=stage_g2,
            converge=converge,
            report_for=report_for,
            drop=lambda host, pib: None,
            clock=lambda: 0.0,
        )
        return rejects.RejectRemote(
            base=base,
            materials=materials,
            build=build or build_variant,
            tamper=tamper_copy,
            stage=stage,
            updater_version=lambda host: "0.2.0",
        )


def test_rejections_stage_six_at_c0_then_build_e_cert_before_g2_and_e_gen_after(
    tmp_path: Path,
) -> None:
    session = scenario_session(tmp_path)
    fake = RejectFake(session)
    found = rejects.rejections(session, fake.remote())
    pair = session.pair
    v, nxt = pair.releases[pair.next], pair.next
    assert fake.events == [
        "restore site-b",
        "build other-site g2 " + v,
        "stage other-site",
        "build digest g2 " + v,
        "tamper digest.pib",
        "stage digest-tampered",
        "build rogue-key g2 " + v,
        "stage rogue-key",
        "build schema-jump g2 v.fs",
        "stage schema-jump",
        "build min-updater g2 " + v,
        "stage min-updater",
        "build same-gen g1 " + v,
        "stage same-gen",
        "build before-g2 g3 " + v,  # g2를 커밋하기 전에 만든다 — 생성 시각 역행
        f"apply build g2 {nxt} {nxt}",
        f"apply stage g2-{nxt}.pib",
        "build after-g2 g1 " + v,  # g2 뒤에 만든 g1 — cert는 통과, gen에서 거부
        "stage before-g2",
        "stage after-g2",
    ]
    assert list(found) == [
        *("E_SITE", "E_DIGEST", "E_SIG", "E_SCHEMA", "E_UPDATER", "same_gen"),
        *(rejects.NEXT_APPLY, "E_CERT", "E_GEN"),
    ]
    assert {key: scenarios.failed(checks) for key, checks in found.items()} == {
        key: [] for key in found
    }
    assert found["E_CERT"]["next_g2_applied"] is True
    saved = json.loads(session.sites["site-b"].ctx.site.state_file.read_text("utf-8"))
    assert [r["name"] for r in saved["results"]] == [rejects.item_name(key) for key in found]


def test_reject_materials_reach_the_right_bundles(tmp_path: Path) -> None:
    session = scenario_session(tmp_path)
    built: list[rejects.Variant] = []

    def build(ctx: Context, variant: rejects.Variant) -> Path:
        built.append(variant)
        return ctx.out / f"{variant.label}.pib"

    rejects.rejections(session, RejectFake(session).remote(build))
    by_label = {v.label: v for v in built}
    assert by_label["other-site"].site_id == "site-x"
    assert by_label["rogue-key"].options == (
        *("--release-key", "/k", "--cert-dir", "/c", "--root-pub", "/r"),
    )
    assert by_label["schema-jump"].options == ("--releases", "/rel", "--store", "/st")
    assert by_label["schema-jump"].product == "v.fs"
    assert by_label["min-updater"].options == ("--min-updater", "1.0.0")
    assert (by_label["same-gen"].gen, by_label["same-gen"].tree) == (1, session.tree)


def test_a_rejection_that_restarts_core_fails(tmp_path: Path) -> None:
    session = scenario_session(tmp_path)
    fake = RejectFake(session)
    fake.touch = {"same-gen"}
    found = rejects.rejections(session, fake.remote())
    assert scenarios.failed(found["same_gen"]) == ["core_not_restarted", "images_match"]


def test_a_rejection_with_another_code_fails_instead_of_passing(tmp_path: Path) -> None:
    """검증 순서 때문에 앞 단계에 걸린 재료(예: E_SCHEMA 재료가 E_DIGEST)는 거짓이다."""
    session = scenario_session(tmp_path)
    fake = RejectFake(session)
    fake.codes["schema-jump"] = "E_DIGEST"
    assert scenarios.failed(rejects.rejections(session, fake.remote())["E_SCHEMA"]) == ["code"]


# ---------------------------------------------------------------- 설치 전 변조(site-b 부팅 때)


class FakeShell:
    """설치 전 탐침의 원격 셸 가짜 — 명령 조각마다 정한 출력을 내고 명령을 남긴다."""

    def __init__(self, *, sha: str, bootstrap_rc: int = 1, fail_on: str = "") -> None:
        self.sha = sha
        self.bootstrap_rc = bootstrap_rc
        self.fail_on = fail_on
        self.commands: list[str] = []
        self.puts: list[str] = []

    def run(
        self, command: str, *, check: bool = True, timeout_s: float = 600
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.fail_on and self.fail_on in command:
            raise VmError("ssh 255")
        out, err, rc = "", "", 0
        if "tamper.py" in command:
            member = (
                "bootstrap/bootstrap-verify.sh" if "bootstrap" in command else "debs/docker-ce.deb"
            )
            out = json.dumps({"member": member, "offset": 9, "size": 10})
        elif "sha256sum" in command:
            out = f"{self.sha}  bootstrap/bootstrap-verify.sh\n"
        elif "--root-fpr" in command:
            rc, err = self.bootstrap_rc, "bootstrap-verify: FAIL: payload sha256 mismatch\n"
        elif "command -v docker" in command:
            out = "absent\n"
        elif "echo removed" in command:
            out = "removed\n"
        return subprocess.CompletedProcess([], rc, out, err)

    def put(self, local: Path, remote: str, *, timeout_s: float = 1800) -> None:
        self.puts.append(remote)


BOOK = preinstall.Runbook("/home/pureiron/incoming/install.pib", "f" * 64, RUNBOOK_SHA)


def test_tampered_script_stops_at_the_runbook_sha_check_and_is_not_run() -> None:
    shell = FakeShell(sha="c" * 64)
    found = preinstall.probe(shell, BOOK, judges.BOOTSTRAP_MEMBER)
    assert not any("--root-fpr" in c for c in shell.commands)
    copy = "cp /home/pureiron/incoming/install.pib /root/e2e-preinstall/bundle.pib"
    assert copy in shell.commands[0]
    assert "rm -rf /root/e2e-preinstall" in shell.commands[-1]
    assert scenarios.failed(judges.judge_script_tamper(found, RUNBOOK_SHA)) == []


def test_tampered_deb_runs_the_genuine_script_which_fails_on_the_payload() -> None:
    shell = FakeShell(sha=RUNBOOK_SHA)
    found = preinstall.probe(shell, BOOK, "debs/*.deb")
    assert any("--root-fpr " + "f" * 64 in c for c in shell.commands)
    assert scenarios.failed(judges.judge_deb_tamper(found, RUNBOOK_SHA)) == []
    shell = FakeShell(sha=RUNBOOK_SHA, bootstrap_rc=0)  # 검증이 통과하면 거짓
    found = preinstall.probe(shell, BOOK, "debs/*.deb")
    assert scenarios.failed(judges.judge_deb_tamper(found, RUNBOOK_SHA)) == ["bootstrap_fails"]


def probe_found(script: bool, **change: object) -> dict[str, object]:
    found: dict[str, object] = {
        "tamper": {"member": judges.BOOTSTRAP_MEMBER if script else "debs/x.deb"},
        "sha256": "c" * 64 if script else RUNBOOK_SHA,
        "docker": "absent",
        "removed": True,
    }
    if not script:
        found["bootstrap"] = {"rc": 1, "stderr": "FAIL: payload sha256 mismatch"}
    return {**found, **change}


@pytest.mark.parametrize(
    ("script", "change", "want"),
    [
        (True, {"docker": "present"}, ["docker_absent"]),
        (True, {"docker": ""}, ["docker_absent"]),
        (True, {"sha256": RUNBOOK_SHA}, ["runbook_stops"]),
        (True, {"sha256": ""}, ["runbook_stops"]),
        (True, {"removed": False}, ["probe_removed"]),
        (True, {"bootstrap": {"rc": 0}}, ["bootstrap_not_run"]),
        (True, {"tamper": {"member": "debs/x.deb"}}, ["tampered_copy"]),
        (False, {"docker": "present"}, ["docker_absent"]),
        (False, {"sha256": "c" * 64}, ["genuine_script"]),
        (False, {"bootstrap": {"rc": 1, "stderr": "root fingerprint"}}, ["bootstrap_fails"]),
    ],
)
def test_preinstall_judges_need_every_item(
    script: bool, change: dict[str, object], want: list[str]
) -> None:
    """변이: docker 미설치 확인을 빼면 docker가 설치된 탐침이 통과한다. 못 읽은 값은 거짓."""
    found = probe_found(script, **change)
    judge = judges.judge_script_tamper if script else judges.judge_deb_tamper
    assert scenarios.failed(judge(probe_found(script), RUNBOOK_SHA)) == []
    assert scenarios.failed(judge(found, RUNBOOK_SHA)) == want


def test_a_failing_probe_still_removes_its_directory_and_the_next_probe_runs() -> None:
    shell = FakeShell(sha=RUNBOOK_SHA, fail_on="sha256sum")
    found, errors = preinstall.run_probes(shell, BOOK)
    assert shell.puts == [preinstall.TAMPER_REMOTE]
    assert set(errors) == set(preinstall.PROBES)
    assert found == {}
    assert sum("echo removed" in c for c in shell.commands) == 2  # 실패해도 지운다


def test_only_site_b_gets_the_preinstall_hook(tmp_path: Path) -> None:
    session = scenario_session(tmp_path)
    calls: list[preinstall.Runbook] = []

    def probes(host: preinstall.Shell, book: preinstall.Runbook) -> preinstall.Found:
        calls.append(book)
        return {"tampered_bootstrap": {"x": 1}}, {"tampered_deb": E2EError("boom")}

    shell = FakeShell(sha=RUNBOOK_SHA)
    assert g2.preinstall_hook(session.sites["site-a"], shell, probes=probes) is None
    run = session.sites["site-b"]
    run.ctx.install_pib = str(tmp_path / "pureiron-install-2026.11.1.pib")
    run.ctx.runbook_fpr, run.ctx.runbook_bootstrap = "f" * 64, RUNBOOK_SHA
    hook = g2.preinstall_hook(run, shell, probes=probes)
    assert hook is not None
    hook()
    remote = "/home/pureiron/incoming/pureiron-install-2026.11.1.pib"
    assert calls == [preinstall.Runbook(remote, "f" * 64, RUNBOOK_SHA)]
    assert run.preinstall == {"tampered_bootstrap": {"x": 1}}
    assert set(run.preinstall_errors) == {"tampered_deb"}


def test_check_preinstall_judges_records_and_rewraps_errors_by_class(tmp_path: Path) -> None:
    session = scenario_session(tmp_path)
    run = session.sites["site-b"]
    run.ctx.runbook_bootstrap = RUNBOOK_SHA
    found = preinstall.probe(FakeShell(sha="c" * 64), BOOK, judges.BOOTSTRAP_MEMBER)
    run.preinstall = {"tampered_bootstrap": found}
    assert scenarios.failed(rejects.check_preinstall(session, "tampered_bootstrap")) == []
    saved = json.loads(run.ctx.site.state_file.read_text("utf-8"))
    assert saved["results"][-1]["name"] == "G2-5 tampered before install (tampered_bootstrap)"
    run.preinstall_errors = {"tampered_deb": VmError("ssh 255")}
    with pytest.raises(InfraError):
        rejects.check_preinstall(session, "tampered_deb")
    run.preinstall_errors = {"tampered_deb": RemoteCommandError("cp failed", 1)}
    with pytest.raises(E2EError):
        rejects.check_preinstall(session, "tampered_deb")
    run.preinstall_errors = {}
    run.error = InfraError("vm did not boot")
    with pytest.raises(InfraError, match="did not boot"):
        rejects.check_preinstall(session, "tampered_deb")
