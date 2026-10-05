"""Updater 상태기계(plan M2 phase 표, B4·B5·B11·B16).

    fetched → verified ─(창)→ committed → locked → backed_up → migrating → switching → checking
                                                                 │           │          │
                                       succeeded ←───────────────┼───────────┼──────────┘
                                                                 └─→ rolling_back → failed
                                                                                  └→ failed_manual

규칙:
- 저장된 phase = 지금 실행 중인 단계. 각 단계의 바깥 호출은 멱등이라 crash 뒤 다시 실행해도 된다.
- 서비스 영향 단계(`committed` 이후)는 창 안에서만 시작한다(최초 설치는 창 없음 — 멈출
  서비스가 없다).
- 커밋 전 종료(거부·적재 실패)는 generation을 기록하지 않고 번들 sha256만 건너뛴다 — 서명 실패
  번들의 generation은 믿을 수 없다. 커밋 후 종료는 `last_attempted_gen`·`last_result`로 남고
  같은 generation은 다시 시도하지 않는다(자동 재시도 없음).
- 모든 종료(`_finish`)는 편집 잠금을 먼저 푼다. core HTTP가 없으면 `pureiron-core project unlock`.
- 롤백은 앱 이미지·프로젝트만 직전으로 되돌린다. 스키마는 전진만, site-db digest는 바꾸지 않는다.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.manifest import Manifest
from pureiron_bundle.verify import (
    AttemptResult,
    DraftState,
    GenDecision,
    GenState,
    SchemaAction,
    VerifyContext,
    check_draft_gate,
    verify_bundle,
)
from pureiron_updater import gc, metrics
from pureiron_updater.backup import BackupError, make_backup, restore_project
from pureiron_updater.compose import (
    COMPOSE_PAYLOAD,
    check_config,
    read_manifest,
    remove_target,
    target_dir,
    write_target,
)
from pureiron_updater.images import check_running, load_images, pin_images
from pureiron_updater.ports import (
    Clock,
    CommandError,
    Compose,
    ComposeTarget,
    Core,
    CoreUnavailableError,
    Docker,
    LockConflictError,
    Readiness,
)
from pureiron_updater.settings import Settings
from pureiron_updater.state import (
    PRE_COMMIT,
    TERMINAL,
    Attempt,
    Phase,
    StateStore,
    UpdaterState,
    skip_bundle,
)
from pureiron_updater.window import WindowError, window_open

log = logging.getLogger(__name__)

GOOD_RATIO_TOLERANCE = 0.02
MAX_ROLLBACK_TRIES = 2
CHECK_INTERVAL_S = 2.0
E_LOAD = "E_LOAD"
E_BACKUP = "E_BACKUP"
E_MIGRATE = "E_MIGRATE"
E_SWITCH = "E_SWITCH"
E_HEALTH = "E_HEALTH"
E_SUPERSEDED = "E_SUPERSEDED"
E_BUNDLE = "E_BUNDLE"
_IMAGE_PAYLOAD = re.compile(r"images/([a-z][a-z0-9-]{0,31})\.tar(\.gz)?")


class AttemptInProgressError(Exception):
    """커밋된 시도가 진행 중이라 새 번들을 받을 수 없다(`recover` 먼저)."""


class _RollbackError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Deps:
    docker: Docker
    compose: Compose
    core: Core
    clock: Clock
    on_save: Callable[[UpdaterState], None] = lambda _state: None
    """상태를 저장할 때마다 부른다(테스트의 crash 주입 지점)."""


def health_ok(sample: Readiness, baseline: float | None) -> bool:
    """`checking` 통과 조건: ready + collector 세션, baseline > 0이면 GOOD 비율 ≥ baseline - 2%p."""
    if not (sample.ready and sample.collector):
        return False
    if not baseline:
        return True
    return sample.good_ratio is not None and sample.good_ratio >= baseline - GOOD_RATIO_TOLERANCE


def image_archives(manifest: Manifest, extract_dir: Path) -> dict[str, Path]:
    archives: dict[str, Path] = {}
    for name in manifest.files:
        match = _IMAGE_PAYLOAD.fullmatch(name)
        if match is not None:
            archives[match[1]] = extract_dir.joinpath(*name.split("/"))
    return archives


class Updater:
    def __init__(self, settings: Settings, deps: Deps, store: StateStore | None = None) -> None:
        self.settings = settings
        self.deps = deps
        self.store = store or StateStore(settings.state_dir)
        self.state = self.store.load()
        self.finished: list[Attempt] = []
        """이 프로세스에서 끝난 시도(CLI 종료 코드 판정용)."""

    # ── 상태 기록 ──────────────────────────────────────────────────────────

    def _save(self, state: UpdaterState) -> None:
        self.store.save(state)
        self.state = state
        self.deps.on_save(state)

    def _update(self, attempt: Attempt, **changes: object) -> Attempt:
        new = replace(attempt, **changes)
        self._save(replace(self.state, attempt=new))
        return new

    def _enter(self, attempt: Attempt, phase: Phase, **changes: object) -> Attempt:
        timeline = {**attempt.timeline, phase.value: self.deps.clock.now()}
        log.info("generation %s: %s", attempt.generation, phase.value)
        return self._update(attempt, phase=phase, timeline=timeline, **changes)

    def _target(self, generation: int) -> ComposeTarget:
        return target_dir(self.settings.compose_dir, generation)

    def _applied_target(self) -> ComposeTarget | None:
        gen = self.state.applied_gen
        return None if gen is None else self._target(gen)

    @staticmethod
    def _gen(attempt: Attempt) -> int:
        if attempt.generation is None:
            raise RuntimeError(f"attempt in {attempt.phase} without generation")
        return attempt.generation

    # ── 공개 진입점 ────────────────────────────────────────────────────────

    def submit(self, bundle: Path, sha256: str) -> None:
        """`bundles/` 아래 놓인 번들을 새 시도로 등록한다.

        대기 중(종료 전) 시도는 새 번들이 검증상 **더 높은 generation**일 때만 대체한다 — poll과
        같은 규칙이다. 더 낮거나 같은 generation(재빌드)이면 새 번들만 `E_GEN`으로 거부·보고한다.
        generation을 아직 모르는(검증 전) 시도는 그냥 대체한다.
        """
        current = self.state.attempt
        if current is not None and current.phase not in PRE_COMMIT | TERMINAL:
            raise AttemptInProgressError(
                f"generation {current.generation} is in {current.phase} (run recover first)"
            )
        if current is not None and current.phase in PRE_COMMIT:
            if current.bundle_sha256 == sha256:
                log.info("bundle %s is already waiting", sha256)
                return
            if current.generation is not None and not self._supersedes(current, bundle, sha256):
                return
            if Phase.COMMITTED.value in current.timeline:
                # 커밋된(pending) 시도는 결과를 남기고 잠금을 푼 뒤 대체한다.
                self._end(current, Phase.FAILED, E_SUPERSEDED, f"superseded by {sha256}")
            else:
                log.info("superseding waiting attempt (generation %s)", current.generation)
                self._cleanup(current, keep_bundle=Path(current.bundle) == bundle)
                self._save(replace(self.state, attempt=None))
        # 남은 것이 종료 phase면 잠금 해제·보고를 마무리한 뒤에만 새 시도를 받는다.
        if self.state.attempt is not None and self.advance().attempt is not None:
            raise AttemptInProgressError("previous attempt could not release the edit lock")
        attempt = Attempt(
            phase=Phase.FETCHED,
            bundle=str(bundle),
            bundle_sha256=sha256,
            extract_dir=str(bundle.with_name(bundle.name + ".d")),
            started_at=self.deps.clock.now(),
            timeline={Phase.FETCHED.value: self.deps.clock.now()},
        )
        self._save(replace(self.state, attempt=attempt))

    def _supersedes(self, current: Attempt, bundle: Path, sha256: str) -> bool:
        """새 번들을 부작용 없이(추출 없음) 검증해 `current`보다 높은 generation인지 판정한다."""
        try:
            result = verify_bundle(bundle, self._verify_context(self.state.applied_gen is None))
        except BundleRejectedError as exc:
            self._reject_unsubmitted(bundle, sha256, exc.code.value, exc.detail)
            return False
        generation = result.manifest.generation
        if generation > self._gen(current):
            return True
        # 더 낮은 generation·같은 generation 재빌드는 대기 시도를 그대로 두고 새 번들만 거부한다.
        self._reject_unsubmitted(
            bundle,
            sha256,
            RejectCode.E_GEN.value,
            f"generation {generation} does not supersede waiting generation {current.generation}",
        )
        return False

    def _reject_unsubmitted(self, bundle: Path, sha256: str, code: str, detail: str) -> None:
        """시도 슬롯을 건드리지 않고 새 번들만 거부·보고한다."""
        now = self.deps.clock.now()
        rejected = Attempt(
            phase=Phase.REJECTED,
            bundle=str(bundle),
            bundle_sha256=sha256,
            extract_dir="",
            started_at=now,
            failed_phase=Phase.VERIFIED,
            code=code,
            detail=detail,
            timeline={Phase.FETCHED.value: now, Phase.REJECTED.value: now},
        )
        log.error("bundle %s rejected (%s): %s", sha256, code, detail)
        metrics.write_report(self.settings.state_dir, rejected, now)
        bundle.unlink(missing_ok=True)
        self._save(skip_bundle(self.state, sha256))
        self.finished.append(rejected)

    def advance(self) -> UpdaterState:
        """시도를 진행할 수 있는 데까지 진행한다(창 대기·일시 장애·종료에서 멈춘다)."""
        steps: Mapping[Phase, Callable[[Attempt], bool]] = {
            Phase.FETCHED: self._verify,
            Phase.VERIFIED: self._commit,
            Phase.COMMITTED: self._lock,
            Phase.LOCKED: self._backup,
            Phase.BACKED_UP: lambda a: bool(self._enter(a, Phase.MIGRATING)),
            Phase.MIGRATING: self._migrate,
            Phase.SWITCHING: self._switch,
            Phase.CHECKING: self._check,
            Phase.ROLLING_BACK: self._rollback,
        }
        if self.state.attempt is not None and not self.deps.docker.available():
            # 재부팅 직후 등: 일시 장애를 실패로 분류하지 않고 다음 tick에 그대로 이어 간다.
            log.warning("docker daemon unavailable; retrying next tick")
            return self.state
        while (attempt := self.state.attempt) is not None:
            if not steps.get(attempt.phase, self._finish)(attempt):
                break
        return self.state

    def window_is_open(self, attempt: Attempt) -> bool:
        if attempt.first_install:
            return True
        try:
            manifest = read_manifest(self._target(self._gen(attempt)))
            return window_open(manifest.window, self.deps.clock.now())
        except (WindowError, OSError, BundleRejectedError) as exc:
            log.warning("window unreadable, treating as closed: %s", exc)
            return False

    def release_lock(self, attempt: Attempt) -> bool:
        """편집 잠금 해제. core HTTP가 없으면 site-db에서 직접(이미지 버전 무관). 풀렸으면 True."""
        if attempt.first_install:
            return True
        try:
            self.deps.core.release_lock()
            return True
        except LockConflictError:
            return True
        except CoreUnavailableError as exc:
            log.warning("edit-lock release over HTTP failed (%s); using site-db", exc)
        target = self._applied_target() or self._target(self._gen(attempt))
        try:
            self.deps.compose.release_lock_db(target)
            return True
        except CommandError as exc:
            log.error("edit-lock release failed: %s", exc)
            return False

    def reset_phase(self, attempt: Attempt, phase: Phase) -> Attempt:
        """recover 전용: 타임라인은 두고 저장된 phase만 바꾼다(다시 실행할 단계 지정)."""
        return self._update(attempt, phase=phase)

    def drop(self, attempt: Attempt) -> None:
        """끝낼 것이 없는 시도(번들 없음·이미 적용됨)를 지운다."""
        if attempt.lock_held:
            raise RuntimeError("an attempt holding the edit lock must finish, not be dropped")
        self._cleanup(attempt)
        self._save(replace(self.state, attempt=None))

    # ── phase 처리(True = 계속, False = 멈춤) ──────────────────────────────

    def _verify_context(self, first_install: bool) -> VerifyContext:
        state = self.state
        draft: DraftState | None = None
        if not first_install:
            try:
                draft = DraftState(*self.deps.core.project())
            except CoreUnavailableError as exc:
                log.warning("draft state unavailable at verify (rechecked under lock): %s", exc)
        return VerifyContext(
            root_pubkey=keys.read_public_key(self.settings.root_pubkey_file),
            site_id=self.settings.site_id,
            updater_version=self.settings.updater_version,
            trust=state.trust,
            generation=GenState(state.applied_gen, state.last_attempted_gen, state.last_result),
            schema_epoch=None if first_install else state.schema_epoch,
            draft=draft,
        )

    def _check_site_db(self, manifest: Manifest) -> None:
        applied = self._applied_target()
        if applied is None:
            return
        current = read_manifest(applied).product_release.images["site-db"]
        wanted = manifest.product_release.images["site-db"]
        if (current.config_digest, current.tag) != (wanted.config_digest, wanted.tag):
            raise BundleRejectedError(
                RejectCode.E_DIGEST,
                f"site-db {current.tag}@{current.config_digest} → "
                f"{wanted.tag}@{wanted.config_digest} needs a db-step release (not supported)",
            )

    def _prepare(self, manifest: Manifest, extract: Path) -> float:
        """검증된 manifest로 창 형식·site-db·compose 정의를 확인하고 이미지를 적재한다(load_s)."""
        window_open(manifest.window, self.deps.clock.now())
        self._check_site_db(manifest)
        target = self._target(manifest.generation)
        write_target(
            target,
            manifest,
            extract.joinpath(*COMPOSE_PAYLOAD.split("/")),
            self.settings.site_env(),
        )
        check_config(self.deps.compose.config_images(target), manifest)
        return load_images(
            self.deps.docker, manifest.product_release.images, image_archives(manifest, extract)
        )

    def _verify(self, attempt: Attempt) -> bool:
        bundle, extract = Path(attempt.bundle), Path(attempt.extract_dir)
        if not bundle.is_file():
            if Phase.COMMITTED.value in attempt.timeline:
                return self._end(attempt, Phase.FAILED, E_BUNDLE, f"{bundle} vanished")
            log.warning("bundle %s vanished; dropping attempt", bundle)
            self.drop(attempt)
            return False
        shutil.rmtree(extract, ignore_errors=True)
        first = self.state.applied_gen is None
        try:
            result = verify_bundle(bundle, self._verify_context(first), extract_to=extract)
        except BundleRejectedError as exc:
            return self._end(attempt, Phase.REJECTED, exc.code.value, exc.detail, Phase.VERIFIED)
        manifest = result.manifest
        if result.generation is GenDecision.NO_OP:
            log.info("generation %s is already applied", manifest.generation)
            self.drop(attempt)
            return False
        # 서명이 확인된 뒤라 generation을 기록해도 된다(보고·target 정리 기준).
        attempt = self._update(attempt, generation=manifest.generation)
        try:
            load_s = self._prepare(manifest, extract)
        except (BundleRejectedError, WindowError, CommandError, OSError) as exc:
            if isinstance(exc, BundleRejectedError):
                phase, code, detail = Phase.REJECTED, exc.code.value, exc.detail
            elif isinstance(exc, WindowError):
                phase, code, detail = Phase.REJECTED, RejectCode.E_FORMAT.value, str(exc)
            else:
                phase, code, detail = Phase.FAILED, E_LOAD, str(exc)
            return self._end(attempt, phase, code, detail, Phase.VERIFIED)
        schema = result.schema
        migrate_to = schema.target if schema and schema.action is SchemaAction.MIGRATE else None
        self._enter(
            attempt,
            Phase.VERIFIED,
            generation=manifest.generation,
            from_generation=self.state.applied_gen,
            first_install=first,
            migrate_to=migrate_to,
            trust_after=result.trust.state,
            verify_s=round(result.verify_s, 3),
            load_s=round(load_s, 3),
        )
        return True

    def _commit(self, attempt: Attempt) -> bool:
        if attempt.lock_held and self.release_lock(attempt):
            attempt = self._update(attempt, lock_held=False)
        if not self.window_is_open(attempt):
            return False
        gen = self._gen(attempt)
        timeline = {**attempt.timeline, Phase.COMMITTED.value: self.deps.clock.now()}
        committed = replace(attempt, phase=Phase.COMMITTED, timeline=timeline)
        self._save(
            replace(
                self.state,
                last_attempted_gen=gen,
                last_result=AttemptResult.PENDING,
                trust=attempt.trust_after or self.state.trust,
                attempt=committed,
            )
        )
        return True

    def back_to_verified(self, attempt: Attempt) -> Attempt:
        """서비스 영향 전 단계를 창 대기로 되돌린다(`last_result=pending` 유지, 다음 창 재개)."""
        released = self.release_lock(attempt)
        return self._update(attempt, phase=Phase.VERIFIED, lock_held=not released)

    def _acquire(self, generation: int) -> int:
        try:
            return self.deps.core.acquire_lock(generation)
        except LockConflictError:
            # 이전 시도의 updater 잠금(다른 generation)이 남았을 수 있다 — 우리 것만 풀고 한 번 더.
            self.deps.core.release_lock()
            return self.deps.core.acquire_lock(generation)

    def _lock(self, attempt: Attempt) -> bool:
        if attempt.first_install:
            self._enter(attempt, Phase.LOCKED)
            return True
        gen = self._gen(attempt)
        try:
            site_rev = self._acquire(gen)
        except (CoreUnavailableError, LockConflictError) as exc:
            log.warning("edit-lock unavailable, waiting for the next tick: %s", exc)
            self.back_to_verified(attempt)
            return False
        attempt = self._update(attempt, lock_held=True)
        try:
            _, base = self.deps.core.project()
            baseline = self.deps.core.readiness().good_ratio
        except CoreUnavailableError as exc:
            log.warning("core unavailable under lock, waiting for the next tick: %s", exc)
            self.back_to_verified(attempt)
            return False
        manifest = read_manifest(self._target(gen))
        try:
            check_draft_gate(
                self.settings.site_id, manifest.project.absorbs, DraftState(site_rev, base)
            )
        except BundleRejectedError as exc:
            return self._end(attempt, Phase.REJECTED, exc.code.value, exc.detail, Phase.LOCKED)
        self._enter(attempt, Phase.LOCKED, baseline_good_ratio=baseline)
        return True

    def _backup(self, attempt: Attempt) -> bool:
        current = self._applied_target()
        if current is not None:
            backup_dir = self.settings.backups_dir / f"g{self._gen(attempt)}"
            try:
                make_backup(self.deps.compose, current, backup_dir, self.settings.secrets_dir)
            except (CommandError, BackupError, OSError) as exc:
                return self._end(attempt, Phase.FAILED, E_BACKUP, str(exc), Phase.BACKED_UP)
        self._enter(attempt, Phase.BACKED_UP)
        return True

    def _migrate(self, attempt: Attempt) -> bool:
        target = self._target(self._gen(attempt))
        try:
            # 재부팅 직후 DB가 아직 준비 안 됐을 수 있다 — health까지 기다린다. 적용 중인 정의로
            # 띄워 site-db 재생성을 피한다(최초 설치는 새 정의로 여기서 처음 뜬다).
            self.deps.compose.up(self._applied_target() or target, ["site-db"])
            if attempt.migrate_to is not None:
                self.deps.compose.run_core(
                    target, ["migrate", "up", "--to", str(attempt.migrate_to)]
                )
        except CommandError as exc:
            self._fail_over(attempt, Phase.MIGRATING, E_MIGRATE, str(exc))
            return True
        epoch = self.state.schema_epoch if attempt.migrate_to is None else attempt.migrate_to
        timeline = {**attempt.timeline, Phase.SWITCHING.value: self.deps.clock.now()}
        switching = replace(attempt, phase=Phase.SWITCHING, timeline=timeline)
        self._save(replace(self.state, schema_epoch=epoch, attempt=switching))
        return True

    def _switch(self, attempt: Attempt) -> bool:
        target = self._target(self._gen(attempt))
        try:
            manifest = read_manifest(target)
            images = manifest.product_release.images
            pin_images(self.deps.docker, images)
            self.deps.compose.up(target)
            check_running(self.deps.compose.running_images(target), images)
            project = manifest.project
            self.deps.compose.run_core(
                target,
                [
                    *("project", "apply", "--project-id", project.project_id),
                    *("--version", project.version, "--content-hash", project.content_hash),
                ],
            )
        except (CommandError, BundleRejectedError, OSError) as exc:
            self._fail_over(attempt, Phase.SWITCHING, E_SWITCH, str(exc))
            return True
        self._enter(attempt, Phase.CHECKING)
        return True

    def _wait_ready(self, baseline: float | None) -> Readiness | None:
        """제한 시간 안에 `health_ok`면 None, 아니면 마지막 표본(없으면 not ready)."""
        clock = self.deps.clock
        deadline = clock.monotonic() + self.settings.check_timeout_s
        last = Readiness(ready=False, collector=False)
        while True:
            try:
                last = self.deps.core.readiness()
            except CoreUnavailableError:
                last = Readiness(ready=False, collector=False)
            if health_ok(last, baseline):
                return None
            if clock.monotonic() >= deadline:
                return last
            clock.sleep(CHECK_INTERVAL_S)

    def _check(self, attempt: Attempt) -> bool:
        failure = self._wait_ready(attempt.baseline_good_ratio)
        if failure is None:
            self._enter(attempt, Phase.SUCCEEDED)
        else:
            self._fail_over(attempt, Phase.CHECKING, E_HEALTH, f"not healthy in time: {failure}")
        return True

    def _fail_over(self, attempt: Attempt, phase: Phase, code: str, detail: str) -> None:
        log.error("generation %s failed in %s (%s): %s", attempt.generation, phase, code, detail)
        self._enter(attempt, Phase.ROLLING_BACK, failed_phase=phase, code=code, detail=detail)

    def _rollback(self, attempt: Attempt) -> bool:
        prev = self._applied_target()
        if prev is None:
            # 최초 설치: 되돌릴 직전 상태가 없다. 띄운 스택은 진단용으로 그대로 둔다.
            self._enter(attempt, Phase.FAILED)
            return True
        try:
            images = read_manifest(prev).product_release.images
            pin_images(self.deps.docker, images)
            self.deps.compose.up(prev)
            check_running(self.deps.compose.running_images(prev), images)
            backup_dir = self.settings.backups_dir / f"g{self._gen(attempt)}"
            restore_project(self.deps.compose, prev, backup_dir)
            failure = self._wait_ready(None)
            if failure is not None:
                raise _RollbackError(f"previous generation not ready: {failure}")
        except (CommandError, BundleRejectedError, BackupError, _RollbackError, OSError) as exc:
            tries = attempt.rollback_tries + 1
            detail = f"{attempt.detail}\nrollback #{tries}: {exc}"
            log.error("rollback attempt %d failed: %s", tries, exc)
            if tries >= MAX_ROLLBACK_TRIES:
                self._enter(attempt, Phase.FAILED_MANUAL, rollback_tries=tries, detail=detail)
            else:
                self._update(attempt, rollback_tries=tries, detail=detail)
            return True
        self._enter(attempt, Phase.FAILED)
        return True

    # ── 종료 ───────────────────────────────────────────────────────────────

    def _end(
        self,
        attempt: Attempt,
        phase: Phase,
        code: str,
        detail: str,
        failed_phase: Phase | None = None,
    ) -> bool:
        log.error("generation %s %s (%s): %s", attempt.generation, phase, code, detail)
        self._enter(attempt, phase, code=code, detail=detail, failed_phase=failed_phase)
        return True

    def _cleanup(self, attempt: Attempt, *, keep_bundle: bool = False) -> None:
        if attempt.extract_dir:
            shutil.rmtree(attempt.extract_dir, ignore_errors=True)
        if not keep_bundle:
            Path(attempt.bundle).unlink(missing_ok=True)
        committed = Phase.COMMITTED.value in attempt.timeline
        gen = attempt.generation
        if gen is not None and not committed and gen != self.state.applied_gen:
            remove_target(self._target(gen))

    def _finish(self, attempt: Attempt) -> bool:
        """종료 phase: 잠금 해제 → 보고·메트릭 → 상태 확정(attempt 비움). 각 단계 재실행 안전."""
        if attempt.lock_held:
            if not self.release_lock(attempt):
                return False  # 다음 tick·recover에서 다시 푼다
            attempt = self._update(attempt, lock_held=False)
        ended = self.deps.clock.now()
        metrics.write_report(self.settings.state_dir, attempt, ended)
        state = self.state
        if Phase.COMMITTED.value in attempt.timeline:
            result = metrics.result_of(attempt)
            state = replace(state, last_attempted_gen=attempt.generation, last_result=result)
            if attempt.phase is Phase.SUCCEEDED:
                state = replace(state, applied_gen=attempt.generation, alarm=None)
            elif attempt.phase is Phase.FAILED_MANUAL:
                state = replace(state, alarm=f"g{attempt.generation}: {attempt.code}")
        else:
            state = skip_bundle(state, attempt.bundle_sha256)
        self._cleanup(attempt)
        state = replace(state, attempt=None)
        self._save(state)
        self.finished.append(attempt)
        metrics.write_prom(self.settings.state_dir, state, attempt, ended)
        if attempt.phase is Phase.SUCCEEDED:
            self._collect_garbage(attempt)
        return False

    def _collect_garbage(self, attempt: Attempt) -> None:
        """정리 실패는 이미 확정된 결과에 영향을 주지 않는다(다음 성공 때 다시 시도)."""
        try:
            gc.collect(
                self.deps.docker,
                self.settings.compose_dir,
                self.settings.backups_dir,
                keep=(attempt.generation, attempt.from_generation),
            )
        except Exception:
            log.exception("gc after generation %s failed", attempt.generation)
