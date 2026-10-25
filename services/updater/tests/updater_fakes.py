"""Updater 단위 테스트용 fake 세계: docker 데몬 + compose 스택 + site-db + core HTTP + 시계.

`World`는 Updater 프로세스가 죽어도 남는 바깥 상태(이미지·실행 컨테이너·스키마·잠금·
project_state)다. 모든 부작용 호출은 끝난 **뒤** `World.op()`를 지나고, Updater 상태 저장도
`on_save`로 같은 카운터를 지난다. `crash_at=N`이면 N번째 지점에서 `Crash`(BaseException)를
던진다 — "N번째 효과까지 반영되고 그 다음은 없음"이라는 crash 주입이다.

번들은 bundle-tools로 실제 서명한 `.pib`(가짜 이미지 tar + 저장소의 실제 site compose 파일)다.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML

from pureiron_bundle import keys
from pureiron_bundle.images import config_digest_from_tar
from pureiron_bundle.manifest import DOCKER_PACKAGES, Json
from pureiron_bundle.sign import write_signed_bundle
from pureiron_bundle.trust import format_timestamp, issue_cert
from pureiron_bundle.verify import INSTALL_LAYOUT, ROOT_PEM_PAYLOAD, ROOT_PUB_PAYLOAD
from pureiron_updater.backup import PROJECT_TREE_EPOCH
from pureiron_updater.phases import Deps, Updater
from pureiron_updater.ports import (
    CommandError,
    ComposeTarget,
    CoreUnavailableError,
    LockConflictError,
    Readiness,
)
from pureiron_updater.settings import Settings
from pureiron_updater.state import UpdaterState

REPO = Path(__file__).resolve().parents[3]
SITE_COMPOSE = REPO / "deploy" / "compose" / "site.compose.yml"
ROOT_SEED = bytes(range(32))
RELEASE_SEED = bytes(range(32, 64))
NOT_BEFORE = datetime(2026, 9, 1, tzinfo=UTC)
T0 = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)
SERVICES = ("collector", "core", "site-db", "web")
SITE_DB_TAG = "pureiron/site-db:2.30.2-pg16"
# r4부터 core가 프로젝트 트리(`project_files`, epoch 5 = backup.PROJECT_TREE_EPOCH)를 안다.
EPOCH = {"r0": 1, "r1": 2, "r2": 3, "r3": 4, "r4": 5, "r5": 6}
VERSION = {
    "r0": "2026.10.0",
    "r1": "2026.11.0",
    "r2": "2026.12.0",
    "r3": "2027.1.0",
    "r4": "2027.2.0",
    "r5": "2027.3.0",
}
_VAR = re.compile(r"\$\{([A-Z_]+)(?::-([^}]*))?\}")


class Crash(BaseException):
    """주입된 crash(프로세스 강제 종료). except Exception에 잡히지 않는다."""


def digest_of(service: str, release: str) -> str:
    return "sha256:" + hashlib.sha256(f"{service}:{release}".encode()).hexdigest()


def tag_of(service: str, release: str) -> str:
    return SITE_DB_TAG if service == "site-db" else f"pureiron/{service}:{release}"


def image_tar(path: Path, service: str, release: str) -> None:
    """docker save 형식 가짜 이미지. config digest = `digest_of(service, release)`."""
    hexdigest = digest_of(service, release).removeprefix("sha256:")
    manifest = json.dumps(
        [{"Config": f"blobs/sha256/{hexdigest}", "RepoTags": [tag_of(service, release)]}]
    ).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))


class Kit:
    """서명된 테스트 번들 빌더."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.root_pub = tmp / "keys" / "root.pub"
        self.root_pub.parent.mkdir(parents=True, exist_ok=True)
        self.root_pub.write_bytes(keys.public_key(ROOT_SEED))
        self.cert = issue_cert(ROOT_SEED, keys.public_key(RELEASE_SEED), 1, NOT_BEFORE)
        self.count = 0

    def bundle(
        self,
        generation: int,
        release: str = "r0",
        *,
        site: str = "site-a",
        version: str = "v1",
        absorbs: Mapping[str, int] | None = None,
        window: Json | None = None,
        min_updater: str = "0.1.0",
        epoch: int | None = None,
        compose: bytes | None = None,
        site_db_release: str = "r0",
    ) -> Path:
        self.count += 1
        src = self.tmp / f"src{self.count}"
        payload: dict[str, Path] = {}
        images: dict[str, Json] = {}
        for svc in SERVICES:
            rel = site_db_release if svc == "site-db" else release
            name = f"images/{svc}.tar.gz"
            image_tar(src / name, svc, rel)
            payload[name] = src / name
            images[svc] = {"config_digest": digest_of(svc, rel), "tag": tag_of(svc, rel)}
        compose_file = src / "compose" / "site.compose.yml"
        compose_file.parent.mkdir(parents=True)
        compose_file.write_bytes(SITE_COMPOSE.read_bytes() if compose is None else compose)
        payload["compose/site.compose.yml"] = compose_file
        project = src / "project" / "project.tar"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_bytes(b"project-" + version.encode())
        payload["project/project.tar"] = project
        target = EPOCH[release] if epoch is None else epoch
        project_ref: dict[str, Json] = {
            "project_id": "acme",
            "version": version,
            "content_hash": "sha256:" + hashlib.sha256(version.encode()).hexdigest(),
        }
        if absorbs is not None:
            project_ref["absorbs"] = dict(absorbs)
        document: dict[str, Json] = {
            "format": 1,
            "kind": "site",
            "site_id": site,
            "generation": generation,
            "created_at": format_timestamp(T0 - timedelta(hours=1) + timedelta(minutes=self.count)),
            "product_release": {
                "version": VERSION[release],
                "min_updater": min_updater,
                "images": images,
                "schema": {
                    "target": target,
                    "app_expected": target,
                    "app_max_compatible": target + 1,
                    "compat_app_min": "2026.10.0",
                },
            },
            "project": project_ref,
            "window": window if window is not None else open_window(),
            "trust_update": None,
            "fleet_commit": f"abc{self.count:04x}",
        }
        out = self.tmp / "out" / f"{site}-g{generation}-{self.count}.pib"
        out.parent.mkdir(parents=True, exist_ok=True)
        write_signed_bundle(
            out,
            document,
            payload,
            release_seed=RELEASE_SEED,
            cert_json=self.cert[0],
            cert_sig=self.cert[1],
        )
        return out

    def install_bundle(self) -> Path:
        """서명된 최소 설치 번들(`kind: install`) — 내용은 자리표시자."""
        self.count += 1
        src = self.tmp / f"install{self.count}"
        root = keys.public_key(ROOT_SEED)
        contents: dict[str, bytes] = {name: f"# {name}\n".encode() for name in INSTALL_LAYOUT}
        contents[ROOT_PUB_PAYLOAD] = root
        contents[ROOT_PEM_PAYLOAD] = keys.export_pem(root).encode()
        contents["updater/updater-0.1.0.tar.gz"] = b"venv"
        contents.update({f"debs/{p}.deb": p.encode() for p in DOCKER_PACKAGES})
        payload: dict[str, Path] = {}
        for name, data in contents.items():
            path = src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            payload[name] = path
        document: dict[str, Json] = {
            "format": 1,
            "kind": "install",
            "release": "2026.10.0",
            "created_at": format_timestamp(T0),
            "updater": {"version": "0.1.0", "file": "updater/updater-0.1.0.tar.gz"},
            "docker": {
                "packages": dict.fromkeys(DOCKER_PACKAGES, "1.0-1"),
                "compose_min": "2.17.0",
            },
            "trust_update": None,
        }
        out = self.tmp / "out" / f"pureiron-install-{self.count}.pib"
        out.parent.mkdir(parents=True, exist_ok=True)
        write_signed_bundle(
            out,
            document,
            payload,
            release_seed=RELEASE_SEED,
            cert_json=self.cert[0],
            cert_sig=self.cert[1],
        )
        return out


def open_window(start: datetime = T0 - timedelta(hours=1), hours: float = 48) -> Json:
    end = start + timedelta(hours=hours)
    return {"once": {"start": start.isoformat(), "end": end.isoformat()}}


def closed_window() -> Json:
    return open_window(T0 + timedelta(days=30), 1)


class FakeClock:
    def __init__(self) -> None:
        self.t = T0
        self.mono = 0.0

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.mono += seconds
        self.t += timedelta(seconds=seconds)


class _Loader(Protocol):
    def load(self, stream: str) -> object: ...


@cache
def _compose_images(compose_text: str) -> tuple[tuple[str, str], ...]:
    """활성(profile 없는) 서비스 → image 원문. 순수 파이썬 YAML이 느려 내용별로 캐시한다."""
    loader = cast(_Loader, YAML(typ="safe", pure=True))
    data = cast(dict[str, object], loader.load(compose_text))
    services = cast(dict[str, dict[str, object]], data["services"])
    return tuple((n, str(s["image"])) for n, s in services.items() if "profiles" not in s)


def resolve_compose(target: ComposeTarget) -> dict[str, str]:
    """`docker compose config`의 활성 서비스·image 해석(`${VAR:-default}`만 흉내)."""
    env: dict[str, str] = {}
    for line in target.env_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        env[key] = value
    images = _compose_images(target.compose_file.read_text(encoding="utf-8"))
    return {
        name: _VAR.sub(lambda m: env.get(m[1]) or (m[2] or ""), image) for name, image in images
    }


@dataclass
class World:
    """Updater 밖에서 crash와 무관하게 유지되는 상태."""

    tags: dict[str, str] = field(default_factory=dict[str, str])
    ids: set[str] = field(default_factory=set[str])
    running: dict[str, str] = field(default_factory=dict[str, str])
    schema_epoch: int = 0
    lock: tuple[str, int] | None = None
    site_rev: int = 0
    base_site_rev: int = 0
    project: dict[str, str] = field(default_factory=dict[str, str])
    tree: bytes | None = None
    """마지막으로 적용·복원된 프로젝트 트리(project.tar 바이트) = 정의·project_files."""
    draft_extra: dict[str, bytes] = field(default_factory=dict[str, bytes])
    """`export --draft`가 쓰기 mount에 함께 남기는 파일(신뢰하지 않는 컨테이너 출력 주입)."""
    good_ratio: float | None = None
    # 결함 주입
    fail_migrate_ids: set[str] = field(default_factory=set[str])
    unhealthy_ids: set[str] = field(default_factory=set[str])
    fail_up_ids: set[str] = field(default_factory=set[str])
    core_http_down: bool = False
    docker_down: bool = False
    db_ready: bool = True
    """site-db 컨테이너가 떠 있어도 접속을 받는지(재부팅 직후 WAL 복구 중이면 False)."""
    # crash 주입
    ops: int = 0
    crash_at: int | None = None
    log: list[str] = field(default_factory=list[str])
    up_calls: int = 0

    def op(self, name: str) -> None:
        self.ops += 1
        self.log.append(name)
        if self.crash_at is not None and self.ops == self.crash_at:
            self.crash_at = None
            raise Crash(f"crash after #{self.ops} {name}")

    def reboot(self) -> None:
        """재부팅 직후: 데몬 정지·DB 복구 중·core HTTP 없음(컨테이너·이미지는 유지)."""
        self.docker_down = True
        self.db_ready = False
        self.core_http_down = True

    def daemon(self) -> None:
        if self.docker_down:
            raise CommandError("Cannot connect to the Docker daemon")

    def db(self) -> None:
        self.daemon()
        if "site-db" not in self.running or not self.db_ready:
            raise CommandError("site-db: connection refused")

    def resolve(self, ref: str) -> str | None:
        return self.tags.get(ref) or (ref if ref in self.ids else None)

    def snapshot(self) -> dict[str, object]:
        """수렴 비교용 바깥 상태."""
        return {
            "running": dict(self.running),
            "schema_epoch": self.schema_epoch,
            "lock": self.lock,
            "site_rev": self.site_rev,
            "base_site_rev": self.base_site_rev,
            "project": dict(self.project),
            "tree": self.tree,
        }


class FakeDocker:
    def __init__(self, world: World) -> None:
        self.w = world

    def image_id(self, ref: str) -> str | None:
        self.w.daemon()
        return self.w.resolve(ref)

    def available(self) -> bool:
        return not self.w.docker_down

    def load(self, archive: Path) -> None:
        digest = config_digest_from_tar(archive)
        with tarfile.open(archive) as tar:
            handle = tar.extractfile("manifest.json")
            assert handle is not None
            entry = cast(list[dict[str, list[str]]], json.loads(handle.read()))[0]
        self.w.ids.add(digest)
        for tag in entry["RepoTags"]:
            self.w.tags[tag] = digest
        self.w.op(f"load {archive.name}")

    def tag(self, source: str, target: str) -> None:
        resolved = self.w.resolve(source)
        if resolved is None:
            raise CommandError(f"No such image: {source}")
        self.w.tags[target] = resolved
        self.w.op(f"tag {target}")

    def tags(self, prefix: str) -> list[str]:
        self.w.daemon()
        return sorted(t for t in self.w.tags if t.startswith(prefix))

    def remove(self, ref: str) -> None:
        self.w.daemon()
        image = self.w.tags.get(ref)
        if image is None:
            raise CommandError(f"No such image: {ref}")
        others = [t for t, i in self.w.tags.items() if i == image and t != ref]
        if not others and image in self.w.running.values():
            raise CommandError(f"conflict: image {ref} is used by a running container")
        del self.w.tags[ref]
        if image not in self.w.tags.values():
            self.w.ids.discard(image)
        self.w.op(f"rmi {ref}")


class FakeCompose:
    def __init__(self, world: World) -> None:
        self.w = world
        self.calls: list[tuple[str, ...]] = []

    def config_images(self, target: ComposeTarget) -> Mapping[str, str]:
        return resolve_compose(target)

    def up(self, target: ComposeTarget, services: Sequence[str] = ()) -> None:
        self.calls.append(("up", target.directory.name, *services))
        self.w.daemon()
        self.w.up_calls += 1
        resolved = resolve_compose(target)
        names = list(services) or list(resolved)
        for name in names:
            image = self.w.resolve(resolved[name])
            if image is None:
                raise CommandError(
                    f"{name}: image {resolved[name]} not present (pull_policy never)"
                )
            if image in self.w.fail_up_ids:
                raise CommandError(f"{name}: container exited")
            self.w.running[name] = image
        if not services:
            for name in list(self.w.running):
                if name not in resolved:
                    del self.w.running[name]
        # `--wait`는 healthcheck까지 기다린다: DB는 접속 가능, core HTTP는 응답한다.
        if "site-db" in names:
            self.w.db_ready = True
        if "core" in names:
            self.w.core_http_down = False
        self.w.op(f"up {target.directory.name} {' '.join(services)}".strip())

    def running_images(self, target: ComposeTarget) -> Mapping[str, str]:
        return dict(self.w.running)

    def _core_image(self, target: ComposeTarget) -> str:
        image = self.w.resolve(resolve_compose(target)["core"])
        if image is None:
            raise CommandError("core image missing")
        self.w.db()
        return image

    def run_core(
        self,
        target: ComposeTarget,
        args: Sequence[str],
        *,
        mounts: Mapping[Path, str] | None = None,
        writable: Mapping[Path, str] | None = None,
    ) -> str:
        self.calls.append(("run_core", target.directory.name, *args))
        image = self._core_image(target)
        w = self.w
        out = ""
        host = {v: k for k, v in {**(mounts or {}), **(writable or {})}.items()}
        match list(args):
            case ["migrate", "up", "--to", epoch]:
                if image in w.fail_migrate_ids:
                    raise CommandError("migration 0042 failed")
                w.schema_epoch = max(w.schema_epoch, int(epoch))
            case ["project", "export"]:
                out = json.dumps(self._project_snapshot())
            case ["project", "export", "--with-state", "--out", path]:
                self._tree_image(image, args, writable_path=path in (writable or {}).values())
                (host[path] / "state.json").write_text(json.dumps(self._project_snapshot()))
                if w.tree is not None:
                    (host[path] / "project.tar").write_bytes(w.tree)
            case ["project", "export", "--draft", "--out", path]:
                self._tree_image(image, args, writable_path=path in (writable or {}).values())
                self._export_draft(host[path])
            case ["project", "apply", "--project-id", pid, "--version", ver, "--content-hash", h]:
                self._apply(pid, ver, h)
            case [
                "project",
                "apply",
                "--tar",
                path,
                "--project-id",
                pid,
                "--version",
                ver,
                "--content-hash",
                h,
            ]:
                self._tree_image(image, args)
                w.tree = host[path].read_bytes()
                self._apply(pid, ver, h)
            case ["project", "restore", "--from", path] if host[path].is_dir():
                self._tree_image(image, args)
                tree = host[path] / "project.tar"
                w.tree = tree.read_bytes() if tree.is_file() else w.tree
                self._restore(host[path] / "state.json")
            case ["project", "restore", "--from", path]:
                self._restore(host[path])
            case _:
                raise AssertionError(f"unexpected core command {args}")
        w.op("run_core " + " ".join(args[:2]))
        return out

    @staticmethod
    def _tree_image(image: str, args: Sequence[str], *, writable_path: bool = True) -> None:
        """트리 명령은 트리를 아는 core(r4+)만 안다 — Updater가 이미지에 맞는 명령을 고르는지."""
        release = next(r for r in EPOCH if digest_of("core", r) == image)
        if EPOCH[release] < PROJECT_TREE_EPOCH or not writable_path:
            raise AssertionError(f"core {release} does not support {list(args)}")

    def _export_draft(self, out: Path) -> None:
        w = self.w
        if w.tree is None:  # core: project_files가 비면 exit 4
            raise CommandError("no applied project tree (project_files is empty)")
        meta = {
            "site_rev": w.site_rev,
            "base_site_rev": w.base_site_rev,
            "content_hash": w.project["content_hash"],
        }
        (out / "draft-meta.json").write_text(json.dumps(meta))
        (out / "project.tar").write_bytes(w.tree)
        for name, data in w.draft_extra.items():
            (out / name).write_bytes(data)

    def _apply(self, project_id: str, version: str, content_hash: str) -> None:
        w = self.w
        w.project = {"project_id": project_id, "version": version, "content_hash": content_hash}
        w.base_site_rev = w.site_rev

    def _restore(self, state_file: Path) -> None:
        w = self.w
        snap = cast(dict[str, object], json.loads(state_file.read_text(encoding="utf-8")))
        w.project = {k: str(snap[k]) for k in ("project_id", "version", "content_hash")}
        w.site_rev = cast(int, snap["site_rev"])
        w.base_site_rev = cast(int, snap["base_site_rev"])
        owner, gen = snap["edit_lock_owner"], snap["edit_lock_gen"]
        w.lock = None if owner is None else (cast(str, owner), cast(int, gen))

    def _project_snapshot(self) -> dict[str, object]:
        w = self.w
        return {
            **w.project,
            "site_rev": w.site_rev,
            "base_site_rev": w.base_site_rev,
            "edit_lock_owner": None if w.lock is None else w.lock[0],
            "edit_lock_gen": None if w.lock is None else w.lock[1],
        }

    def dump_db(self, target: ComposeTarget, out: Path) -> None:
        self.w.db()
        out.write_bytes(b"PGDMP")
        self.w.op("pg_dump")

    def down(self, target: ComposeTarget) -> None:
        self.w.daemon()
        self.w.running.clear()
        self.w.op("down")

    def release_lock_db(self, target: ComposeTarget) -> None:
        self.calls.append(("release_lock_db", target.directory.name))
        self.w.db()
        if self.w.lock is not None and self.w.lock[0] == "updater":
            self.w.lock = None
        self.w.op("unlock sql")


class FakeCore:
    def __init__(self, world: World) -> None:
        self.w = world

    def _up(self) -> None:
        w = self.w
        if w.docker_down or w.core_http_down or "core" not in w.running or not w.db_ready:
            raise CoreUnavailableError("connection refused")

    def acquire_lock(self, generation: int) -> int:
        self._up()
        w = self.w
        if w.lock is not None and w.lock != ("updater", generation):
            raise LockConflictError(f"held by {w.lock}")
        w.lock = ("updater", generation)
        w.op("lock")
        return w.site_rev

    def release_lock(self) -> None:
        self._up()
        w = self.w
        if w.lock is not None and w.lock[0] != "updater":
            raise LockConflictError(f"held by {w.lock}")
        w.lock = None
        w.op("unlock")

    def project(self) -> tuple[int, int]:
        self._up()
        return self.w.site_rev, self.w.base_site_rev

    def readiness(self) -> Readiness:
        self._up()
        w = self.w
        core = w.running["core"]
        ready = core not in w.unhealthy_ids and w.schema_epoch >= 1 and "site-db" in w.running
        return Readiness(ready=ready, collector="collector" in w.running, good_ratio=w.good_ratio)


@dataclass
class Site:
    """테스트 하나의 현장: 설정 + 바깥 세계 + Updater 생성기."""

    root: Path
    kit: Kit
    world: World = field(default_factory=World)
    clock: FakeClock = field(default_factory=FakeClock)
    saves: list[UpdaterState] = field(default_factory=list[UpdaterState])

    @property
    def settings(self) -> Settings:
        return Settings(
            site_id="site-a",
            root=self.root,
            root_pubkey_file=self.kit.root_pub,
            core_url="http://core.invalid/api/v1",
            check_timeout_s=10.0,
        )

    def deps(self) -> Deps:
        def on_save(state: UpdaterState) -> None:
            self.saves.append(state)
            self.world.op(f"save {state.attempt.phase.value if state.attempt else 'idle'}")

        return Deps(
            docker=FakeDocker(self.world),
            compose=FakeCompose(self.world),
            core=FakeCore(self.world),
            clock=self.clock,
            on_save=on_save,
        )

    def updater(self) -> Updater:
        return Updater(self.settings, self.deps())

    def submit(self, pib: Path) -> Updater:
        """`pureiron-site apply`처럼 bundles/에 사본을 두고 등록."""
        updater = self.updater()
        bundles = self.settings.bundles_dir
        bundles.mkdir(parents=True, exist_ok=True)
        data = pib.read_bytes()
        sha = "sha256:" + hashlib.sha256(data).hexdigest()
        copy = bundles / f"apply-{sha[7:23]}.pib"
        copy.write_bytes(data)
        updater.submit(copy, sha)
        return updater

    def apply(self, pib: Path) -> UpdaterState:
        return self.submit(pib).advance()

    def running_release(self) -> dict[str, str]:
        return dict(self.world.running)


def release_ids(release: str, site_db_release: str = "r0") -> dict[str, str]:
    return {
        svc: digest_of(svc, site_db_release if svc == "site-db" else release) for svc in SERVICES
    }
