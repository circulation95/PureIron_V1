"""nightly FAULT 2종과 트리(M6-3a I3, H9) — 이름·버전·epoch 규칙(K5: NEXT epoch를 박지 않는다),
파생 digest 규칙(health는 collector만·migrate는 core만 NEXT와 다르다), 실패하는 migration 형식,
prune이 파생까지 같이 지우는지, NEXT·FAULT 트리가 G2 트리에서 나오는지."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from harness import buildhost, bundles, paths, trees
from harness.context import E2EError
from harness.release_pair import (
    DEFAULT,
    NIGHTLY_TREE_TAG,
    ReleasePair,
    fault_record_problems,
    nightly,
)

from pureiron_bundle.manifest import release_version_pattern
from pureiron_core.platform.migrate import NO_TRANSACTION_MARKER, load_catalog

SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
MIGRATIONS = paths.REPO / "services" / "core" / "migrations"
FLEET_SITE = paths.REPO / "packages" / "contracts" / "fleet-site.schema.json"


def pair(next_epoch: int = 5) -> ReleasePair:
    return nightly(
        {"r5": {"version": "2026.11.1", "schema": {"epoch": 5}}}, {"r5"}, SHA, next_epoch
    )


def test_nightly_pair_has_two_faults_named_after_next() -> None:
    p = pair()
    assert (p.next, p.fault, p.fault_migrate) == ("n1a2b3c4", "n1a2b3c4h", "n1a2b3c4m")
    assert dict(p.releases) == {
        "r5": "2026.11.1",
        "n1a2b3c4": "2026.11.2-nightly.1a2b3c4",
        "n1a2b3c4h": "2026.11.2-nightly.1a2b3c4.fh",
        "n1a2b3c4m": "2026.11.2-nightly.1a2b3c4.fm",
    }
    assert dict(p.epochs) == {"r5": 5, "n1a2b3c4": 5, "n1a2b3c4h": 5, "n1a2b3c4m": 6}


def test_fault_versions_pass_the_fleet_site_release_pattern() -> None:
    pattern = json.loads(FLEET_SITE.read_text(encoding="utf-8"))["$defs"]["release"]["pattern"]
    for name in (pair().fault, pair().fault_migrate):
        version = pair().releases[name]
        assert re.fullmatch(pattern, version), version
        assert release_version_pattern().fullmatch(version)


def test_fault_migrate_epoch_follows_next_when_head_adds_a_migration() -> None:
    """K5: HEAD가 migration을 더한 날(NEXT epoch 6) FAULT-m은 7이다."""
    raised = pair(next_epoch=6)
    assert (raised.epochs[raised.next], raised.epochs[raised.fault]) == (6, 6)
    assert raised.epochs[raised.fault_migrate] == 7


def test_default_pair_keeps_its_meaning() -> None:
    """run.py 쌍(`DEFAULT`)은 그대로 — 새 필드 `fault_migrate`는 비어 있다."""
    assert (DEFAULT.base, DEFAULT.next, DEFAULT.fault, DEFAULT.fault_migrate) == (
        "r4",
        "r5",
        "r5f",
        "",
    )
    assert dict(DEFAULT.epochs) == {"r4": 5, "r5": 5, "r5f": 5}


def test_nightly_trees_change_one_tag_description_per_release() -> None:
    p = pair()
    assert p.tree_tag == NIGHTLY_TREE_TAG
    descriptions = [p.tree_descriptions[n] for n in (p.next, p.fault, p.fault_migrate)]
    assert len(set(descriptions)) == 3


# ---------------------------------------------------------------- 파생 기록 규칙


DIGESTS = {"core": "sha256:c", "collector": "sha256:o", "web": "sha256:w", "site-db": "sha256:d"}


def record(name: str, version: str, epoch: int, **changed: str) -> dict[str, object]:
    images = {svc: {"config_digest": changed.get(svc, d)} for svc, d in DIGESTS.items()}
    return {
        "name": name,
        "version": version,
        "images": images,
        "schema": {"epoch": epoch, "app_expected": epoch, "app_max_compatible": epoch + 1},
    }


NEXT_RECORD = record("n1a2b3c4", "2026.11.2-nightly.1a2b3c4", 5)


def health(**changed: str) -> dict[str, object]:
    return record("n1a2b3c4h", "2026.11.2-nightly.1a2b3c4.fh", 5, **changed)


def migrate(epoch: int = 6, **changed: str) -> dict[str, object]:
    return record("n1a2b3c4m", "2026.11.2-nightly.1a2b3c4.fm", epoch, **changed)


def test_good_fault_records_have_no_problems() -> None:
    p = pair()
    assert fault_record_problems(p, p.fault, health(collector="sha256:x"), NEXT_RECORD) == []
    assert fault_record_problems(p, p.fault_migrate, migrate(core="sha256:y"), NEXT_RECORD) == []


@pytest.mark.parametrize(
    ("which", "rec", "problem"),
    [
        ("h", health(collector="sha256:x", core="sha256:other"), "core digest"),
        ("h", health(collector="sha256:x", web="sha256:other"), "web digest"),
        ("h", health(), "collector digest equals NEXT"),
        ("h", health(collector="sha256:x", **{"site-db": "sha256:z"}), "site-db digest"),
        ("m", migrate(core="sha256:y", collector="sha256:other"), "collector digest"),
        ("m", migrate(), "core digest equals NEXT"),
        ("m", migrate(epoch=5, core="sha256:y"), "schema"),
        ("m", migrate(epoch=7, core="sha256:y"), "schema"),
        ("h", {**health(collector="sha256:x"), "version": "2026.11.2-fault1"}, "version"),
    ],
)
def test_fault_records_that_break_the_derivation_rule_are_refused(
    which: str, rec: dict[str, object], problem: str
) -> None:
    p = pair()
    name = p.fault if which == "h" else p.fault_migrate
    problems = fault_record_problems(p, name, rec, NEXT_RECORD)
    assert any(problem in item for item in problems), problems


def test_prepare_refuses_fault_records_from_wsl_that_break_the_rule() -> None:
    p = pair()
    good = {p.fault: health(collector="sha256:x"), p.fault_migrate: migrate(core="sha256:y")}

    def wsl(records: dict[str, dict[str, object]]) -> Callable[..., str]:
        def run(script: str, *, timeout_s: float) -> str:
            assert f"nightly-faults --work {buildhost.NIGHTLY_WORK} --next n1a2b3c4" in script
            assert timeout_s == buildhost.FAULTS_TIMEOUT_S
            return "log\n" + json.dumps({"ok": True, "records": records}) + "\n"

        return run

    got = buildhost.nightly_faults(p, NEXT_RECORD, run_wsl=wsl(good))
    assert list(got) == [p.fault, p.fault_migrate]
    bad = {**good, p.fault: health(collector="sha256:x", core="sha256:other")}
    with pytest.raises(E2EError, match="core digest"):
        buildhost.nightly_faults(p, NEXT_RECORD, run_wsl=wsl(bad))


# ---------------------------------------------------------------- 실패하는 migration


def test_fault_migration_is_the_next_number_and_next_epoch_plus_one(tmp_path: Path) -> None:
    catalog = load_catalog(MIGRATIONS)
    filename, epoch, _ = bundles.fault_migration(MIGRATIONS)
    last = max(m.version for m in catalog.migrations)
    assert filename == f"{last + 1:04d}_e2e_fault_marker.sql"
    assert epoch == catalog.app_expected + 1
    built = bundles.write_fault_migrations(MIGRATIONS, tmp_path / "m")
    again = load_catalog(tmp_path / "m")
    assert built == again.app_expected == catalog.app_expected + 1
    assert again.migrations[-1].filename == filename
    assert again.migrations[-1].transactional  # 실패하면 표지 테이블도 같이 롤백돼야 한다


def test_fault_migration_follows_a_head_that_added_a_migration(tmp_path: Path) -> None:
    """K5: HEAD가 epoch 6(0014)을 더했으면 FAULT-m은 0015·epoch 7이다."""
    head = tmp_path / "head"
    shutil.copytree(MIGRATIONS, head)
    epoch = load_catalog(MIGRATIONS).app_expected + 1
    version = max(m.version for m in load_catalog(MIGRATIONS).migrations) + 1
    (head / f"{version:04d}_more.sql").write_text("SELECT 1;\n", encoding="utf-8")
    text = (head / "epochs.yaml").read_text(encoding="utf-8")
    (head / "epochs.yaml").write_text(f"{text}  {epoch}: {version:04d}_more.sql\n", "utf-8")
    filename, fault_epoch, _ = bundles.fault_migration(head)
    assert (filename, fault_epoch) == (f"{version + 1:04d}_e2e_fault_marker.sql", epoch + 1)
    assert bundles.write_fault_migrations(head, tmp_path / "m") == epoch + 1


def test_fault_migration_creates_the_marker_then_fails() -> None:
    sql = bundles.FAULT_MIGRATE_SQL
    assert not sql.startswith(NO_TRANSACTION_MARKER)
    assert sql.index("CREATE TABLE e2e_fault_marker") < sql.index("SELECT 1 / 0")
    dockerfile = bundles.FAULT_MIGRATE_DOCKERFILE.format(source="n1a2b3c4").splitlines()
    assert dockerfile[0] == "FROM pureiron/core:n1a2b3c4"
    assert "COPY migrations/ /opt/pureiron/migrations/" in dockerfile
    assert bundles.CORE_MIGRATIONS == "/opt/pureiron/migrations"


class Docker:
    """`nightly_faults`의 docker 호출 가짜 — `docker tag`는 digest를 그대로 두고 `docker build`한
    이미지만 새 digest를 받는다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.built: set[str] = set()

    def run(self, *cmd: str, cwd: Path = paths.REPO) -> None:
        del cwd
        self.calls.append(cmd)
        if cmd[:2] == ("docker", "build"):
            self.built.add(cmd[cmd.index("-t") + 1])

    def save(self, tag: str, tar: Path) -> dict[str, str]:
        tar.write_bytes(b"tar")
        svc = tag.split("/")[1].split(":", maxsplit=1)[0]
        digest = f"sha256:new-{tag}" if tag in self.built else DIGESTS[svc]
        return {"tag": tag, "config_digest": digest}


def test_nightly_faults_derive_both_records_in_the_nightly_work_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    (work / "releases").mkdir(parents=True)
    (work / "store" / "n1a2b3c4").mkdir(parents=True)
    (work / "store" / "n1a2b3c4" / "site-db.tar").write_bytes(b"db")
    (work / "releases" / "n1a2b3c4.json").write_text(
        json.dumps({**NEXT_RECORD, "commit": SHA}), encoding="utf-8"
    )
    docker = Docker()
    links: list[tuple[Path, Path]] = []
    monkeypatch.setattr(bundles, "_run", docker.run)
    monkeypatch.setattr(bundles, "_save", docker.save)

    def ensure(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def relink(link: Path, target: Path) -> None:
        links.append((link, target))

    monkeypatch.setattr(bundles, "ensure_image", ensure)
    monkeypatch.setattr(bundles, "relink", relink)

    def migrations(tag: str, dest: Path) -> Path:
        assert tag == "pureiron/core:n1a2b3c4"
        shutil.copytree(MIGRATIONS, dest)
        return dest

    result = bundles.nightly_faults(work, "n1a2b3c4", image_migrations=migrations)
    records = cast(dict[str, dict[str, object]], result["records"])
    assert list(records) == ["n1a2b3c4h", "n1a2b3c4m"]
    assert docker.built == {"pureiron/collector:n1a2b3c4h", "pureiron/core:n1a2b3c4m"}
    epoch = load_catalog(MIGRATIONS).app_expected
    assert records["n1a2b3c4m"]["schema"] == {
        "epoch": epoch + 1,
        "app_expected": epoch + 1,
        "app_max_compatible": epoch + 2,
    }
    assert records["n1a2b3c4h"]["schema"] == NEXT_RECORD["schema"]
    for name in records:
        saved = json.loads((work / "releases" / f"{name}.json").read_text("utf-8"))
        assert saved == records[name]
        assert sorted(p.name for p in (work / "store" / name).iterdir()) == [
            "collector.tar",
            "core.tar",
            "web.tar",
        ]
    assert [link.parent.name for link, _ in links] == ["n1a2b3c4h", "n1a2b3c4m"]
    assert not (work / "fault-build").exists()
    tags = [c for c in docker.calls if c[:2] == ("docker", "tag")]
    assert ("docker", "tag", "pureiron/core:n1a2b3c4", "pureiron/core:n1a2b3c4h") in tags
    assert ("docker", "tag", "pureiron/collector:n1a2b3c4", "pureiron/collector:n1a2b3c4m") in tags


# ---------------------------------------------------------------- prune


def test_prune_removes_the_faults_with_their_next() -> None:
    names = {
        "n1111111": 1.0,
        "n1111111h": 1.1,
        "n1111111m": 1.2,
        "n2222222": 2.0,
        "n2222222h": 2.1,
        "n3333333": 3.0,
        "n3333333m": 3.1,
        "n4444444": 4.0,
        "n4444444h": 4.1,
        "n4444444m": 4.2,
    }
    assert bundles.nightly_prune(names, "n4444444") == [
        "n1111111",
        "n1111111h",
        "n1111111m",
    ]
    assert bundles.nightly_prune(names, "n4444444", keep=1) == [
        "n1111111",
        "n1111111h",
        "n1111111m",
        "n2222222",
        "n2222222h",
        "n3333333",
        "n3333333m",
    ]
    assert bundles.nightly_group("n4444444h") == bundles.nightly_group("n4444444m") == "n4444444"


# ---------------------------------------------------------------- 트리


def test_next_and_fault_trees_come_from_the_g2_tree(tmp_path: Path) -> None:
    p = pair()
    g2_tree = trees.g2_tree(tmp_path / "tree")
    base = trees.tree_expect(g2_tree, p)
    assert trees.g2_tag_paths() <= base.tags
    seen = {base.content_hash}
    for name in (p.next, p.fault, p.fault_migrate):
        tree = bundles.release_tree(tmp_path / "trees" / name, name, p, source=g2_tree)
        want = trees.tree_expect(tree, p)
        assert want.tags == base.tags  # 100 시뮬레이터 태그 포함
        assert want.tree_tag_description == p.tree_descriptions[name]
        assert want.tree_tag_description != base.tree_tag_description
        seen.add(want.content_hash)
    assert len(seen) == 4
