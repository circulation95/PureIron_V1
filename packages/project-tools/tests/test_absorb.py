"""`pureiron-project absorb`·`gate`(M5-3a K1·K2·K4·K5·K6): 필드 병합 계획, 검증 실패 1·충돌 2와
그때 repo 무변경, trailer·fingerprint 출력, 게이트 두 입력 형태와 3분기."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from pureiron_bundle import keys
from pureiron_project.absorb import TMP_SUFFIX
from pureiron_project.canonical import Json, dump, load
from pureiron_project.cli import main
from pureiron_project.merge import (
    ALARM_FIELDS,
    TAG_FIELDS,
    FieldChange,
    plan_merge,
)
from pureiron_project.project import load_project

from .absorb_fakes import draft, enrollment, project_tar, snapshot, tree_tar

ACME = Path(__file__).resolve().parents[1] / "fixtures" / "acme"
SITE = "site-a"
SEED = bytes(range(32))
TRAILER = "Absorbs-Draft: "


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(ACME, target)
    return target


def _edit(root: Path, rel: str, change: Callable[[Json], object]) -> None:
    path = root / rel
    data = load(path.read_text(encoding="utf-8"), rel)
    change(data)
    path.write_text(dump(data), encoding="utf-8")


def _item(data: Json, key: str, ident: str) -> dict[str, Json]:
    assert isinstance(data, list)
    for item in data:
        if isinstance(item, dict) and item.get(key) == ident:
            return item
    raise AssertionError(f"{ident} not found")


def _site_tree(tmp_path: Path, *edits: tuple[str, Callable[[Json], object]]) -> Path:
    """acme + 현장 PATCH(유효 트리 = 드래프트 project.tar의 내용)."""
    root = tmp_path / "site-tree"
    shutil.copytree(ACME, root)
    for rel, change in edits:
        _edit(root, rel, change)
    return root


def _patched(tmp_path: Path) -> Path:
    def alarms(data: Json) -> None:
        _item(data, "id", "temp-high")["setpoint"] = 85
        _item(data, "id", "temp-low").pop("deadband")  # 기본값으로(제거)

    def tags(data: Json) -> None:
        temp = _item(data, "path", "line1/temp")
        temp["eu"] = "K"
        temp["record"] = {"deadband": 1.0}

    return _site_tree(tmp_path, ("alarms/line1.yaml", alarms), ("tags/plc-1.yaml", tags))


def _write_inputs(
    tmp_path: Path, tree_root: Path, *, site_rev: int = 5, base_site_rev: int = 3
) -> tuple[Path, Path]:
    tree, content_hash = project_tar(tree_root)
    draft_file = tmp_path / "draft.tar"
    draft_file.write_bytes(
        draft(SEED, SITE, tree, content_hash, site_rev=site_rev, base_site_rev=base_site_rev)
    )
    key_file = tmp_path / "enrollment.json"
    key_file.write_bytes(enrollment(SEED, SITE))
    return draft_file, key_file


def _absorb(repo: Path, draft_file: Path, key_file: Path, *extra: str) -> int:
    args = ["absorb", "--draft", str(draft_file), "--site", SITE, "--site-key", str(key_file)]
    return main([*args, *extra, str(repo)])


# ── 병합 계획(순수 함수) ──────────────────────────────────────────────────────


def test_patch_fields_match_core_patch_whitelist() -> None:
    assert sorted(ALARM_FIELDS) == ["deadband", "on_delay_s", "priority", "setpoint"]
    assert sorted(TAG_FIELDS) == ["description", "eu", "record"]


def test_plan_merge_takes_patch_fields_and_keeps_everything_else() -> None:
    repo: dict[str, Json] = {
        "alarms/a.yaml": [
            {"id": "a1", "tag": "t/1", "type": "above", "setpoint": 1, "description": "repo"},
            {"id": "a2", "tag": "t/1", "type": "below", "setpoint": 0, "deadband": 2},
        ],
        "tags/d.yaml": [
            {"path": "t/1", "device": "d", "modbus": {"address": 9}, "eu": "C"},
            {"path": "t/new", "device": "d", "modbus": {"address": 10}},
        ],
    }
    draft_tree: dict[str, Json] = {
        "alarms/a.yaml": [
            {"id": "a1", "tag": "t/1", "type": "above", "setpoint": 7, "description": "old"},
            {"id": "a2", "tag": "t/1", "type": "below", "setpoint": 0},
        ],
        "tags/d.yaml": [{"path": "t/1", "device": "d", "modbus": {"address": 1}, "record": {}}],
    }
    plan = plan_merge(repo, draft_tree)
    assert plan.missing == ()
    assert plan.files == {
        "alarms/a.yaml": [
            {"id": "a1", "tag": "t/1", "type": "above", "setpoint": 7, "description": "repo"},
            {"id": "a2", "tag": "t/1", "type": "below", "setpoint": 0},
        ],
        "tags/d.yaml": [
            {"path": "t/1", "device": "d", "modbus": {"address": 9}, "record": {}},
            {"path": "t/new", "device": "d", "modbus": {"address": 10}},
        ],
    }
    assert plan.changes == (
        FieldChange("alarms/a.yaml", "alarm a1", "setpoint", 1, 7),
        FieldChange("alarms/a.yaml", "alarm a2", "deadband", 2, None),
        FieldChange("tags/d.yaml", "tag t/1", "eu", "C", None),
        FieldChange("tags/d.yaml", "tag t/1", "record", None, {}),
    )


def test_plan_merge_same_values_change_nothing_and_reports_missing_items() -> None:
    tree: dict[str, Json] = {"alarms/a.yaml": [{"id": "a1", "setpoint": 1}]}
    assert plan_merge(tree, tree).files == {}
    draft_tree: dict[str, Json] = {
        "alarms/a.yaml": [{"id": "a1", "setpoint": 1}, {"id": "gone", "setpoint": 2}],
        "tags/d.yaml": [{"path": "x/y"}],
    }
    plan = plan_merge(tree, draft_tree)
    assert plan.missing == ("alarm gone", "tag x/y")


def test_plan_merge_matches_tags_by_path_across_files() -> None:
    repo: dict[str, Json] = {"tags/new-device.yaml": [{"path": "t/1", "device": "new-device"}]}
    draft_tree: dict[str, Json] = {"tags/old.yaml": [{"path": "t/1", "device": "old", "eu": "m"}]}
    plan = plan_merge(repo, draft_tree)
    assert plan.files == {
        "tags/new-device.yaml": [{"path": "t/1", "device": "new-device", "eu": "m"}]
    }


# ── absorb CLI ────────────────────────────────────────────────────────────────


def test_absorb_merges_fields_records_absorbs_and_prints_trailer(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site_tree = _patched(tmp_path)
    draft_file, key_file = _write_inputs(tmp_path, site_tree)
    assert _absorb(repo, draft_file, key_file) == 0
    out = capsys.readouterr().out.splitlines()
    sha = hashlib.sha256(draft_file.read_bytes()).hexdigest()
    assert out[-1] == f"{TRAILER}{SITE} rev=5 sha256={sha}"
    assert out[0] == (
        f"site key: {SITE} {keys.fingerprint(keys.public_key(SEED))} "
        "(compare with the installation record)"
    )
    assert "alarms/line1.yaml: alarm temp-high setpoint: 80 -> 85" in out
    assert "alarms/line1.yaml: alarm temp-low deadband: 1.5 -> (absent)" in out
    assert 'tags/plc-1.yaml: tag line1/temp eu: "°C" -> "K"' in out
    record = 'record: {"deadband":0.5,"max_interval_s":60} -> {"deadband":1}'
    assert f"tags/plc-1.yaml: tag line1/temp {record}" in out
    assert f"absorbs[{SITE}]: 3 -> 5" in out
    assert [line for line in out if line.startswith("wrote ")] == [
        "wrote alarms/line1.yaml",
        "wrote project.yaml",
        "wrote tags/plc-1.yaml",
    ]
    # 회수 뒤 repo = 현장 유효 트리 + absorbs 5(그 밖의 내용은 같다)
    assert main(["fmt", "--check", str(repo)]) == 0
    _edit(site_tree, "project.yaml", lambda d: _set_absorbs(d, 5))
    assert load_project(repo).content_hash() == load_project(site_tree).content_hash()


def _set_absorbs(data: Json, rev: int) -> None:
    assert isinstance(data, dict)
    data["absorbs"] = {SITE: rev}


def test_absorb_keeps_repo_changes_made_after_the_bundle(repo: Path, tmp_path: Path) -> None:
    draft_file, key_file = _write_inputs(tmp_path, _patched(tmp_path))

    def ahead(data: Json) -> None:
        assert isinstance(data, list)
        _item(data, "path", "line1/temp")["modbus"] = {
            "address": 4,
            "data_type": "float32",
            "register_type": "holding",
        }
        modbus: Json = {"address": 20, "data_type": "uint16", "register_type": "holding"}
        data.append({"path": "line1/new", "device": "plc-1", "modbus": modbus})

    _edit(repo, "tags/plc-1.yaml", ahead)
    assert _absorb(repo, draft_file, key_file) == 0
    tags = load((repo / "tags" / "plc-1.yaml").read_text(encoding="utf-8"))
    temp = _item(tags, "path", "line1/temp")
    assert temp["modbus"] == {"address": 4, "data_type": "float32", "register_type": "holding"}
    assert (temp["eu"], temp["record"]) == ("K", {"deadband": 1.0})
    assert _item(tags, "path", "line1/new")["device"] == "plc-1"


def test_absorb_same_values_still_records_absorbs_and_notes_empty_draft(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, ACME, site_rev=4, base_site_rev=4)
    assert _absorb(repo, draft_file, key_file) == 0
    out = capsys.readouterr().out.splitlines()
    assert "note: the draft has no site changes (site_rev == base_site_rev)" in out
    assert [line for line in out if line.startswith("wrote ")] == ["wrote project.yaml"]
    project = load((repo / "project.yaml").read_text(encoding="utf-8"))
    assert isinstance(project, dict)
    assert project["absorbs"] == {SITE: 4}


def test_absorb_again_with_the_same_draft_writes_nothing(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, _patched(tmp_path))
    assert _absorb(repo, draft_file, key_file) == 0
    before = snapshot(repo)
    capsys.readouterr()
    assert _absorb(repo, draft_file, key_file) == 0
    out = capsys.readouterr().out.splitlines()
    assert snapshot(repo) == before
    assert not [line for line in out if line.startswith("wrote ")]
    assert out[-1].startswith(f"{TRAILER}{SITE} rev=5 ")


def test_absorb_rev_option_records_lower_revision(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, _patched(tmp_path))
    assert _absorb(repo, draft_file, key_file, "--rev", "4") == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith(f"{TRAILER}{SITE} rev=4 ")


def _rename_project(data: Json) -> None:
    assert isinstance(data, dict)
    data["project_id"] = "other"


@pytest.mark.parametrize(
    "case",
    [
        "draft-signed-by-other-key",
        "enrollment-of-other-key",
        "enrollment-of-other-site",
        "enrollment-tampered",
        "draft-envelope-tampered",
        "project-tar-tampered",
        "payload-hash-mismatch",
        "project-id-mismatch",
        "invalid-draft-tree",
        "invalid-repo",
        "unreadable-draft-file",
    ],
)
def test_absorb_verification_failures_exit_1_without_changes(
    case: str, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site_tree = _patched(tmp_path)
    draft_file, key_file = _write_inputs(tmp_path, site_tree)
    tree, content_hash = project_tar(site_tree)
    other = bytes(range(1, 33))
    if case == "draft-signed-by-other-key":
        draft_file.write_bytes(draft(other, SITE, tree, content_hash, site_rev=5, base_site_rev=3))
    elif case == "enrollment-of-other-key":
        key_file.write_bytes(enrollment(other, SITE))
    elif case == "enrollment-of-other-site":
        key_file.write_bytes(enrollment(SEED, "site-b"))
    elif case == "enrollment-tampered":
        doc = json.loads(key_file.read_bytes())
        doc["payload"]["public_key"] = keys.public_key(other).hex()
        key_file.write_text(json.dumps(doc), encoding="utf-8")
    elif case == "draft-envelope-tampered":
        draft_file.write_bytes(
            draft(SEED, SITE, tree, content_hash, site_rev=5, base_site_rev=3).replace(
                b'"site_rev": 5', b'"site_rev": 9'
            )
        )
    elif case == "project-tar-tampered":
        signed = draft(SEED, SITE, tree, content_hash, site_rev=5, base_site_rev=3)
        draft_file.write_bytes(signed.replace(b"setpoint: 85", b"setpoint: 86"))
    elif case == "payload-hash-mismatch":
        draft_file.write_bytes(
            draft(SEED, SITE, tree, "sha256:" + "0" * 64, site_rev=5, base_site_rev=3)
        )
    elif case == "project-id-mismatch":
        _edit(site_tree, "project.yaml", _rename_project)
        draft_file, key_file = _write_inputs(tmp_path, site_tree)
    elif case == "invalid-draft-tree":
        files = {"project.yaml": b"schema_version: 1\nproject_id: acme\n", "README.md": b"x"}
        bad = tree_tar(files)
        draft_file.write_bytes(draft(SEED, SITE, bad, content_hash, site_rev=5, base_site_rev=3))
    elif case == "invalid-repo":
        (repo / "devices" / "plc-1.yaml").write_text("id: wrong\n", encoding="utf-8")
    else:
        draft_file.unlink()
    before = snapshot(repo)
    assert _absorb(repo, draft_file, key_file) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert err.rstrip().endswith("(repo unchanged)")
    assert snapshot(repo) == before


def test_absorb_rejects_project_tar_with_link_entry(repo: Path, tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        link = tarfile.TarInfo("project.yaml")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    _, key_file = _write_inputs(tmp_path, ACME)
    draft_file = tmp_path / "link.tar"
    draft_file.write_bytes(
        draft(SEED, SITE, buffer.getvalue(), "sha256:x", site_rev=5, base_site_rev=3)
    )
    before = snapshot(repo)
    assert _absorb(repo, draft_file, key_file) == 1
    assert snapshot(repo) == before


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing-item", "draft items not in the repo"),
        ("rev-above-signed", "is above the signed site_rev 5"),
        ("absorbs-regression", "absorbs cannot go back"),
        ("rev-below-existing", "absorbs cannot go back"),
    ],
)
def test_absorb_conflicts_exit_2_without_changes(
    case: str, message: str, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, _patched(tmp_path))
    extra: list[str] = []
    if case == "missing-item":
        _edit(
            repo,
            "alarms/line1.yaml",
            lambda d: d.pop(0) if isinstance(d, list) else None,
        )
    elif case == "rev-above-signed":
        extra = ["--rev", "6"]
    elif case == "absorbs-regression":
        _edit(repo, "project.yaml", lambda d: _set_absorbs(d, 7))
    else:
        _edit(repo, "project.yaml", lambda d: _set_absorbs(d, 5))
        extra = ["--rev", "4"]
    before = snapshot(repo)
    assert _absorb(repo, draft_file, key_file, *extra) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("conflict: ")
    assert message in captured.err
    assert snapshot(repo) == before


# ── 쓰기 실패(두 단계 쓰기) ──────────────────────────────────────────────────

REPO_CHANGED = "(repo changed: run absorb again to finish, or restore with git)"


def _fail_on(monkeypatch: pytest.MonkeyPatch, method: str, name: str) -> None:
    """`Path.<method>`가 이름이 `name`인 파일에서 PermissionError(Windows 파일 잠금 등)를 낸다."""
    real = getattr(Path, method)

    def fake(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == name:
            raise PermissionError(13, "file is locked", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, fake)


def _tmp_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*" + TMP_SUFFIX))


@pytest.mark.parametrize(
    "failing",
    [
        pytest.param(("write_bytes", "plc-1.yaml" + TMP_SUFFIX), id="tmp-stage"),
        pytest.param(("replace", "line1.yaml" + TMP_SUFFIX), id="first-rename"),
    ],
)
def test_absorb_write_failure_before_any_rename_leaves_repo_unchanged(
    failing: tuple[str, str],
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, _patched(tmp_path))
    before = snapshot(repo)
    with monkeypatch.context() as patched:
        _fail_on(patched, *failing)
        assert _absorb(repo, draft_file, key_file) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: cannot write the project tree: ")
    assert err.rstrip().endswith("(repo unchanged)")
    assert _tmp_files(repo) == []
    assert snapshot(repo) == before


def test_absorb_rename_failure_reports_replaced_files_and_rerun_finishes(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, _patched(tmp_path))
    before = snapshot(repo)
    expected = tmp_path / "expected"
    shutil.copytree(repo, expected)
    assert _absorb(expected, draft_file, key_file) == 0
    with monkeypatch.context() as patched:
        _fail_on(patched, "replace", "plc-1.yaml" + TMP_SUFFIX)
        assert _absorb(repo, draft_file, key_file) == 1
    err = capsys.readouterr().err
    # project.yaml(absorbs)은 맨 마지막 — tags에서 멈추면 absorbs는 기록되지 않았다
    assert "already replaced: alarms/line1.yaml; absorbs not recorded" in err
    assert "(repo unchanged)" not in err
    assert (repo / "project.yaml").read_bytes() == before["project.yaml"]
    assert err.rstrip().endswith(REPO_CHANGED)
    assert _tmp_files(repo) == []
    assert load_project(repo).ok
    assert _absorb(repo, draft_file, key_file) == 0
    assert snapshot(repo) == snapshot(expected)


def test_absorb_write_failure_names_tmp_files_it_could_not_remove(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, _patched(tmp_path))
    with monkeypatch.context() as patched:
        _fail_on(patched, "replace", "line1.yaml" + TMP_SUFFIX)
        _fail_on(patched, "unlink", "plc-1.yaml" + TMP_SUFFIX)
        assert _absorb(repo, draft_file, key_file) == 1
    err = capsys.readouterr().err
    assert f"could not remove tags/plc-1.yaml{TMP_SUFFIX}" in err
    assert err.rstrip().endswith(REPO_CHANGED)
    assert _tmp_files(repo) == [f"tags/plc-1.yaml{TMP_SUFFIX}"]


def test_absorb_negative_rev_is_a_usage_error(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, _patched(tmp_path))
    before = snapshot(repo)
    with pytest.raises(SystemExit) as exited:
        _absorb(repo, draft_file, key_file, "--rev", "-1")
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: ")
    assert "must be >= 0, not -1" in captured.err
    assert snapshot(repo) == before


def test_absorb_missing_item_names_it(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, ACME)
    _edit(repo, "tags/plc-1.yaml", lambda d: d.pop(1) if isinstance(d, list) else None)
    assert _absorb(repo, draft_file, key_file) == 2
    assert "tag line1/running" in capsys.readouterr().err


def test_absorb_defaults_to_current_directory(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_file, key_file = _write_inputs(tmp_path, _patched(tmp_path))
    monkeypatch.chdir(repo)
    args = ["absorb", "--draft", str(draft_file), "--site", SITE, "--site-key", str(key_file)]
    assert main(args) == 0
    assert "setpoint: 85" in (repo / "alarms" / "line1.yaml").read_text(encoding="utf-8")


# ── gate ─────────────────────────────────────────────────────────────────────

type GateCase = tuple[int | None, int, int, bool]
"""(absorbs[site], site_rev, base_site_rev, 통과) — check_draft_gate 3분기(absorbs 있음 2가지)."""

GATE_CASES = [
    pytest.param((3, 3, 1, True), id="absorbed-up-to-site-rev"),
    pytest.param((3, 5, 3, False), id="drafts-after-absorbs"),
    pytest.param((None, 4, 4, True), id="no-absorbs-no-drafts"),
    pytest.param((None, 5, 4, False), id="no-absorbs-with-drafts"),
]


def _set_gate_absorbs(repo: Path, absorbed: int | None) -> None:
    def change(data: Json) -> None:
        assert isinstance(data, dict)
        if absorbed is None:
            data.pop("absorbs")
        else:
            data["absorbs"] = {SITE: absorbed}

    _edit(repo, "project.yaml", change)


@pytest.mark.parametrize("case", GATE_CASES)
def test_gate_with_revisions(
    case: GateCase, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    absorbed, site_rev, base, passes = case
    _set_gate_absorbs(repo, absorbed)
    args = ["gate", str(repo), "--site", SITE, "--site-rev", str(site_rev)]
    code = main([*args, "--base-site-rev", str(base)])
    captured = capsys.readouterr()
    if passes:
        assert code == 0
        assert captured.out.startswith(f"ok: site {SITE} site_rev={site_rev}")
    else:
        assert code == 1
        assert captured.err.startswith("error: E_DRAFT: ")


@pytest.mark.parametrize("case", GATE_CASES)
def test_gate_with_signed_draft(
    case: GateCase, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    absorbed, site_rev, base, passes = case
    _set_gate_absorbs(repo, absorbed)
    draft_file, key_file = _write_inputs(tmp_path, ACME, site_rev=site_rev, base_site_rev=base)
    args = ["gate", str(repo), "--site", SITE, "--draft", str(draft_file)]
    code = main([*args, "--site-key", str(key_file)])
    captured = capsys.readouterr()
    assert code == (0 if passes else 1)
    if passes:
        assert captured.out.startswith(f"site key: {SITE} ")
    else:
        assert captured.err.startswith("error: E_DRAFT: ")


def test_gate_rejects_unverified_draft(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    draft_file, _ = _write_inputs(tmp_path, ACME)
    other_key = tmp_path / "other.json"
    other_key.write_bytes(enrollment(bytes(range(1, 33)), SITE))
    args = ["gate", str(repo), "--site", SITE, "--draft", str(draft_file)]
    assert main([*args, "--site-key", str(other_key)]) == 1
    assert "signature does not match" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["--site-rev", "3"],
        ["--site-rev", "3", "--base-site-rev", "3", "--site-key", "k.json"],
        ["--draft", "d.tar"],
        ["--draft", "d.tar", "--site-key", "k.json", "--base-site-rev", "1"],
        ["--draft", "d.tar", "--site-rev", "3"],
        ["--site-rev", "-1", "--base-site-rev", "0"],
        ["--site-rev", "1", "--base-site-rev", "-1"],
        [],
    ],
)
def test_gate_usage_errors_exit_2(args: list[str], repo: Path) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["gate", str(repo), "--site", SITE, *args])
    assert exited.value.code == 2
