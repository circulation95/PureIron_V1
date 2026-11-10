"""G2 실패 분류(E8) — 예외 종류로 `infra`(실행 환경)와 `product`(제품 결함)를 가른다.

`infra`는 **호출 자체**의 실패다: `InfraError`(preflight·시간 여유 부족·사본 동기화 등 하네스가 직접
내는 것), ssh·scp 호출 실패(`VmError` — 연결·인증·전송, 종료 코드 255), Hyper-V(`HyperVError`),
wsl.exe 호출 실패(`WslError` — bash가 뜨지 않음·wsl.exe 오류·스크립트가 알린 실행 환경 탓),
외부 명령 시간 초과(`subprocess.TimeoutExpired`).

`product`는 그 밖의 전부다: **호출된 제품 스크립트의 실패**(원격 명령의 0 아닌 종료
`vm.RemoteCommandError`, WSL 빌드·번들 스크립트의 0 아닌 종료 `golden.WslScriptError`), HEAD 빌드
실패(`buildhost.HeadBuildError`, D7), 단언 실패, 그 밖의 예외.

nightly는 `infra`만 있을 때 실패한 시험을 1회 다시 돌리고, `product`는 다시 돌리지 않는다. 시간
초과는 제품 멈춤일 수도 있어 infra로 가지만, 같은 시험이 2회 연속 infra로 실패하면 이슈가 열린다
(`report.repeated_infra`).
"""

from __future__ import annotations

import subprocess

from harness.context import InfraError
from harness.golden import WslError
from harness.hyperv import HyperVError
from harness.vm import VmError

INFRA = "infra"
PRODUCT = "product"
INFRA_ERRORS: tuple[type[BaseException], ...] = (
    InfraError,
    VmError,
    HyperVError,
    WslError,
    subprocess.TimeoutExpired,
)


def classify(exc: BaseException) -> str:
    return INFRA if isinstance(exc, INFRA_ERRORS) else PRODUCT
