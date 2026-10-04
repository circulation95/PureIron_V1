"""서명 번들(.pib) 생성·검증 공용 라이브러리(ADR-023, PD-17).

CI(서명)·Updater(검증)·control-api(게시 검사)가 같은 코드 한 벌을 쓴다(P3). 검증 진입점은
`pureiron_bundle.verify.verify_bundle` 하나다.
"""

__version__ = "0.1.0"
