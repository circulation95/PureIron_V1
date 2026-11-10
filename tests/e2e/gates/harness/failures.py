"""G2 실패 분류(E8) — 예외 종류로 `infra`(실행 환경)와 `product`(제품 결함)를 가른다.

`infra`: `InfraError`(preflight·시간 여유 부족 등 하네스가 직접 내는 것)와 ssh(`VmError`)·Hyper-V
(`HyperVError`)·WSL(`WslError`) 호출 실패, 외부 명령 시간 초과. 그 밖의 예외·단언 실패는 `product`다
— HEAD 빌드 실패(`buildhost.HeadBuildError`)도 `product`다(D7). nightly는 `infra`만 있을 때 실패한
시험을 1회 다시 돌리고, `product`는 다시 돌리지 않는다.
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
