"""실행 상태(`Context`) — 결과 기록과 상태 파일 저장. 한 현장(`Site`)에 하나다.

예외는 둘로 가른다(G2 실패 분류 E8): `InfraError`는 실행 환경(preflight, ssh·WSL·Hyper-V 호출, 시간
여유 부족) 탓이라 nightly가 1회 다시 돌리고, `E2EError`·단언 실패는 제품 결함(`product`)이라 다시
돌리지 않는다. `InfraError`는 `E2EError`의 하위 클래스가 아니다 — `except E2EError`가 인프라 실패를
제품 실패로 삼키지 않게.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # site.py가 이 모듈을 import한다
    from harness.release_pair import ReleasePair
    from harness.site import Site

from harness import paths

TAIL_CHARS = 800
"""결과에 남기는 출력 끝부분 길이."""


class E2EError(RuntimeError):
    pass


class InfraError(RuntimeError):
    """실행 환경 실패(`infra`) — 제품 판정과 무관하다. 원인 예외는 `from`으로 남긴다."""


def log(message: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S} {message}", flush=True)


@dataclass
class Context:
    """`site`·`pair`는 실행 설정이라 상태 파일에 쓰지 않는다(나머지 필드가 상태 파일 내용이다)."""

    run_id: str
    artifacts: str
    install_pib: str = ""
    runbook_fpr: str = ""
    runbook_bootstrap: str = ""
    digests: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    next_gen: int = 2
    current: str = ""
    """지금 실행 중인 release. 비워 두면 `pair.base`(설치 g1)다."""
    results: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    bundles: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    nft_before: dict[str, int] = field(default_factory=dict[str, int])
    nft_total: dict[str, int] = field(default_factory=dict[str, int])
    """VM 부팅 이후 누적 외부 접속 시도 — 복원·리셋으로 카운터가 되돌아가도 잃지 않게
    구간마다 더한다."""
    nft_last: dict[str, int] = field(default_factory=dict[str, int])
    nft_c0: dict[str, int] = field(default_factory=dict[str, int])
    nft_mark_at: int = 0
    """마지막 표시의 VM 시각(epoch s) — resolved 로그를 이 뒤부터 읽는다."""
    dns_lookups: dict[str, int] = field(default_factory=dict[str, int])
    """resolved가 조회한 이름별 횟수(로컬 호스트 이름 제외) — dns_out 출처 증거."""
    seed_rows: int = 0
    central: dict[str, object] = field(default_factory=dict[str, object])
    """online 단계의 중앙 상태 요약(`central.py status` — 비밀값 없음: URL·CA 지문·토큰 경로)."""
    site: Site = field(kw_only=True)
    pair: ReleasePair = field(kw_only=True)

    def __post_init__(self) -> None:
        if not self.current:
            self.current = self.pair.base

    def state(self) -> dict[str, object]:
        """상태 파일 내용 — `site`·`pair`를 뺀 필드, 선언 순서대로."""
        return {
            f.name: getattr(self, f.name) for f in fields(self) if f.name not in ("site", "pair")
        }

    def save(self) -> None:
        path = self.site.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.state(), indent=2, ensure_ascii=False), "utf-8")

    @classmethod
    def load(cls, site: Site, pair: ReleasePair) -> Context:
        data = json.loads(site.state_file.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)} - {"site", "pair"}
        return cls(**{k: v for k, v in data.items() if k in known}, site=site, pair=pair)

    @property
    def out(self) -> Path:
        return Path(self.artifacts)

    @property
    def work(self) -> Path:
        """이 run·이 현장의 번들 작업 디렉터리(`<BUNDLES>/<run_id>/<site_id>`) — 현장 둘이 같은
        `run_id`를 써도 번들·트리·m5·online 파일이 겹치지 않는다(M6-1 리뷰 L4)."""
        return paths.BUNDLES / self.run_id / self.site.site_id

    def log(self, message: str) -> None:
        log(f"[{self.site.site_id}] {message}")

    def record(self, name: str, ok: bool, **detail: object) -> None:
        entry: dict[str, object] = {"name": name, "ok": ok, **detail}
        # 단계를 다시 돌리면(`run.py <step>`) 같은 항목은 마지막 결과로 바꾼다.
        self.results = [r for r in self.results if r["name"] != name] + [entry]
        self.save()
        print(
            f"[{self.site.site_id}] [{'PASS' if ok else 'FAIL'}] {name} "
            f"{json.dumps(detail, ensure_ascii=False)}",
            flush=True,
        )
