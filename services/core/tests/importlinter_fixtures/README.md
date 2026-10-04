# import-linter 음성 fixture

각 파일은 `pureiron_core` 임시 사본의 지정 위치(첫 줄 `# target:`)에 복사되어 둘째 줄 `# breaks:`에 `;`로 나열한 계약만 깨야 한다(나머지는 KEPT).
도메인이 `runtime`을 import하면 runtime을 거쳐 다른 도메인 내부로 이어지므로 (c)도 함께 깨지는 것이 맞다.
`.fixture` 확장자는 pyright·ruff·pytest 수집 대상에서 빠지게 하려는 것이다.
`tests/test_importlinter_negative.py`가 계약별로 lint-imports 실패(BROKEN)를 확인한다.
