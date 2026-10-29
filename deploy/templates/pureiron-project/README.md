# pureiron-project-<id>

PureIron 고객 프로젝트 repo — 이 repo가 현장 정의의 정본이다(ADR-025). `project.yaml`의 `project_id`·`name`, `devices/`·`tags/`·`alarms/`의 예제를 현장 값으로 바꿔 쓴다. 비밀값은 파일에 쓰지 않고 `secret://<id>` 참조만 쓴다.

## 명령 (PureIron_V1 체크아웃에서)

```bash
uv run --package pureiron-project-tools pureiron-project validate <repo>        # 스키마 + 교차 참조 검증
uv run --package pureiron-project-tools pureiron-project fmt [--check] <repo>   # 정규형으로 다시 쓰기 / 불일치만 보고
uv run --package pureiron-project-tools pureiron-project hash <repo>            # content hash (sha256:…)
uv run --package pureiron-project-tools pureiron-project absorb <repo> --draft <draft.tar> --site <site_id> --site-key <site-enrollment.json>  # 현장 드래프트 회수
```

버전 이름은 태그 `v1`, `v2`, … 이다. 태그를 push하면 CI가 `project_version`으로 그 이름을 낸다.

## CI

`.github/workflows/validate.yml`이 PureIron_V1의 `validate-project.yml`을 호출한다. PureIron_V1이 private이라 이 repo의 Settings → Secrets and variables → Actions에 secret 2개가 필요하다(값은 벤더가 전달, 여기에 쓰지 않는다).

| secret | 내용 |
|---|---|
| `PUREIRON_CI_APP_ID` | PureIron CI GitHub App의 App ID |
| `PUREIRON_CI_APP_KEY` | 같은 App의 private key(PEM 전체) |

`gh secret set PUREIRON_CI_APP_ID` / `gh secret set PUREIRON_CI_APP_KEY < <key.pem>`으로도 넣을 수 있다.

이 repo에 write 권한이 있는 사람은 workflow를 고쳐 이 키를 꺼낼 수 있고, 키는 곧 PureIron_V1에 대한 CI App 권한 전체(코드 읽기 + PR·commit status 쓰기)다. 지금은 고객 repo가 모두 벤더 계정 소유이고 write 권한자가 벤더뿐이라 이를 수용한다. 외부인(고객 엔지니어 등)에게 이 repo write 권한을 주기 전에 벤더가 고객 repo 전용 읽기 전용 App(Contents read만)으로 secret을 교체하거나 검증 workflow를 공개 repo로 분리한다. 키 유출이 의심되면 벤더가 키를 재발급해 모든 고객 repo secret을 갱신한다.
