# pureiron-bundle-tools

`.pib` 서명 번들의 생성·검증 공용 라이브러리. CI(서명), Updater(검증), control-api(게시 검사)가 같은 코드를 쓴다(ADR-023).

- **범위: M2** — root → release cert → manifest 서명 체인, 검증 6단계 거부 코드(`E_SIG`…`E_DRAFT`), test key.
- M1에서는 uv workspace 멤버와 G1 게이트 대상 자리만 둔다.
