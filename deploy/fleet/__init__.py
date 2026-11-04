"""fleet repo 도구(M4-2, ADR-022, foundation PD-23 ②③④⑤).

파일 규칙·check-gen·승인 판정·publish·revert.

판정(`fleetfiles`·`check_gen.check`·`approval.decide`·`revert.revert_content`)은 순수 함수다.
GitHub REST·git은 `github.py`, control-api HTTP·서명·게시는 `publish.py` 가장자리에만 있다.

CLI는 스크립트로 실행한다(`uv run python deploy/fleet/publish.py run …`) — 각 진입 스크립트가
`deploy/`를 `sys.path` 앞에 넣어 `fleet.*`를 패키지로 읽는다.
"""
