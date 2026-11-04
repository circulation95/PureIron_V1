# pureiron-fleet

PureIron 현장 배포 상태(DesiredState)의 정본 repo(ADR-022). 현장마다 `sites/<site>.yaml` 하나가 "그 현장에 어떤 제품 release·프로젝트 버전을 어떤 창에 적용할지"를 정한다. main에 승인된 PR이 merge되면 publish 호스트가 서명된 번들(`.pib`)을 만들어 control-api에 게시하고, 현장 Updater가 그것을 받아 적용한다.

```
sites/<site>.yaml            활성 현장 파일(파일명 == site)
sites/.retired/<site>.yaml   퇴역 현장(활성 파일을 그대로 옮긴 것, 이후 수정·삭제·이름 재사용 금지)
allowed_approvers.txt        publish 승인자 login 목록
.github/workflows/           check-gen.yml(PR 검사), publish.yml(main → 게시) — PureIron_V1 reusable workflow 호출만
```

## 새 현장 순서 (등록 먼저)

1. **control-api에 현장 등록**(벤더 admin): 현장 Updater가 만든 enrollment로 `pureiron-control site register`. 등록 전에 게시하면 control-api가 404를 돌려주고 publish가 실패한다.
2. `sites/example.yaml`을 `sites/<site>.yaml`로 복사, `site`를 파일명과 같게, `generation: 1`.
3. PR → check-gen 통과 → 승인 → merge → publish가 gen 1을 게시.

미등록 상태로 merge했다면: 등록한 뒤 Actions → publish → **Run workflow**(workflow_dispatch, 입력 없음)로 재실행한다. 이미 게시된 현장(`max_published_gen == gen`)은 건너뛰므로 재실행은 안전하다.

## 바꾸기·되돌리기·퇴역

- 바꾸기: 그 현장 파일을 고치고 `generation`을 정확히 +1. check-gen이 `new == old + 1`, `new > 게시된 최대 gen`을 본다(`E_GEN`).
- 되돌리기: PureIron_V1에서 `uv run python deploy/fleet/revert.py <site> --to-gen N --repo <이 repo>` — 이력의 gen N 내용으로 gen 현재+1 파일·브랜치·PR을 만든다(일반 승인 경로). schema envelope 밖으로 되돌리면 경고가 나오고 현장이 `E_SCHEMA`로 거부한다(DB는 내려가지 않는다).
- 퇴역: `git mv sites/<site>.yaml sites/.retired/<site>.yaml`(내용 그대로). 파일 삭제는 `E_FLEET`. 퇴역한 이름은 다시 쓸 수 없다.
- 쓰기 스위치: `writes: { enabled: true }`(없으면 읽기 전용). Updater 0.2.0 이상만 적용한다. 0.1.0 시절 `updater.env`로 쓰기를 켠 현장은 Updater를 0.2.0으로 바꾸면 그 값이 무시되므로 **Updater 교체 전에** fleet 파일에 `writes.enabled: true`를 먼저 게시한다.
- 현장 드래프트 흡수 확인: publish는 현장 드래프트 상태를 모른다(현장 `E_DRAFT`가 최종 판정). 흡수한 프로젝트 태그를 올리기 전 벤더가 `pureiron-project gate --draft <draft.tar> …`로 확인한다.

## 승인 규칙 (publish의 `E_APPROVAL`)

publish는 main 커밋 sha를 GitHub API·git으로 다시 받아 아래를 **모두** 만족할 때만 서명한다. 하나라도 어기면 `E_APPROVAL`(사유 한 줄씩)로 실패하고 아무것도 게시하지 않는다.

1. 그 커밋이 **merge된 PR의 merge commit**이다(squash·merge·rebase 모두 가능, squash 권장). main 직접 push, PR 머리 커밋을 main에 직접 push한 경우는 거부.
2. 그 PR의 **최종 head 커밋**에 대한 승인이 1명 이상: 리뷰어의 마지막 상태가 APPROVED(뒤에 Request changes·dismiss가 있으면 무효), 승인한 커밋이 최종 head(승인 뒤 커밋을 더 올리면 다시 승인 필요), 리뷰어가 `allowed_approvers.txt`에 있고 PR 작성자가 아님.
3. `allowed_approvers.txt`는 **merge 직전 main**의 것을 읽는다 — PR이 목록에 사람을 넣어도 그 PR에는 효력이 없다(목록 변경 PR을 먼저 따로 승인·merge).
4. **승인 사슬**: merge 직전 main == publish 호스트가 기록한 마지막 승인 커밋. 승인 없이 들어온 커밋이 한 번 끼면 그 뒤 merge도 거부한다.

### `E_APPROVAL` 복구

- "승인 0건·오래된 승인·목록 밖": 이미 merge된 커밋은 다시 승인할 수 없다. 같은 내용을 새 PR(빈 커밋 등)로 올려 정상 승인 → merge.
- "직접 push"·"승인 사슬 끊김": (a) main을 마지막 승인 커밋으로 되돌리고(`git push --force-with-lease origin <마지막 승인 sha>:main`, Free에서 가능) 그 변경을 PR로 다시 올린다. 또는 (b) 사람이 직접 push된 내용을 검토한 뒤 publish 호스트에서 `publish.py trust-base --fleet-repo <owner>/<repo> --sha <검토한 main 커밋> --reason <사유>`로 기준을 다시 기록하고 publish를 재실행한다.
- 연속 merge로 앞 publish가 아직 안 돈 상태에서 뒤 커밋이 먼저 돌아 "사슬 끊김"이 나면: 앞 run이 끝난 뒤 publish를 workflow_dispatch로 재실행.
- 처음 한 번: 이 템플릿으로 repo를 만든 첫 커밋을 `trust-base`로 등록한다(없으면 "승인 사슬 기준이 없다").
- 승인 사슬 기록(`/srv/pureiron/publish/<owner>/<repo>/`)이 사라지면 사람이 main 이력을 검토한 뒤 `trust-base`로 다시 만든다. 주 1회 백업 대상에 넣는다.

## GitHub Free의 한계 (K14) — 왜 workflow 안에서 검사하나

GitHub Free private repo에는 branch protection·필수 리뷰·runner를 특정 workflow에 묶는 기능이 없다. 그래서 승인 판정은 publish 안에서 한다(위 규칙). 이 검사는 **실수와 절차 우회는 막지만, 이 repo에 write 권한이 있는 사람의 의도적 우회는 못 막는다**:

- `pull_request` 이벤트는 PR 브랜치의 workflow 파일을 그대로 돌린다 — PR이 workflow를 고치면 그 코드가 publish runner에서 돈다.
- write 권한자는 아무 브랜치에 `runs-on: publish` workflow를 push해 승인 없이 그 runner에서 코드를 돌릴 수 있다.

→ **runner 사용자가 읽을 수 있는 키·토큰은 이 repo write 권한자 전원이 읽을 수 있다.** 지금은 write 권한자가 벤더뿐이라 수용한다(PoC, ★F1).

### 서명 경계를 실제로 분리하는 조건 (★F1 전환)

다음 중 하나가 생기면 **그 전에** 서명 명령(`publish.py`)을 sudo 뒤 전용 사용자(예: `pureiron-signer`)로 옮긴다 — runner 사용자는 키·토큰 파일을 못 읽고 `sudo -u pureiron-signer …/publish.py run --fleet-repo … --sha …` 한 줄(sudoers에 그 명령만 허용)만 부른다. publish는 이미 sha 하나만 받아 나머지를 스스로 다시 받으므로 코드는 바뀌지 않는다.

- 외부인(고객 엔지니어·협력사)에게 이 repo write 권한을 준다.
- 서명 호스트를 개발 PC가 아닌 곳으로 옮긴다(운영 publish 호스트).

## publish 호스트 배치 (F7, 벤더)

publish·check-gen은 self-hosted runner(라벨 `publish`, 전용 사용자 `pureiron-publish` — sudo·docker 그룹 없음)에서 돈다. control-api가 그 호스트 안에 있어 GitHub-hosted runner는 닿지 못한다.

| 경로 | 내용 |
|---|---|
| `/etc/pureiron/keys/release.key` | release key(0600, `pureiron-publish` 소유) |
| `/etc/pureiron/keys/{release-cert.json, release-cert.sig, root.pub}` | release 인증서·root 공개키(번들 자체 검증) |
| `/etc/pureiron/publish/ci_token` | control-api ci 토큰(0600, `PUT /bundles`) |
| `/etc/pureiron/publish/viewer_token` | control-api viewer 토큰(0600, `GET /fleet` — ci는 fleet을 못 읽는다) |
| `/etc/pureiron/publish/app.pem` | GitHub App private key(0600, fleet·프로젝트 repo 읽기 토큰용) |
| `/etc/pureiron/publish/app_id` | (선택) App ID — workflow가 env `PUREIRON_PUBLISH_APP_ID`로 주면 불필요 |
| `/srv/pureiron/images/` | 이미지 store(`PUREIRON_IMAGE_STORE`, release별 `<release>/<svc>.tar`) |
| `/srv/pureiron/bundles/<site>/` | 만든 `.pib`(USB 반입용 사본) |
| `/srv/pureiron/metrics/` | 게시 기록 `publish-<site>-g<gen>.json` |
| `/srv/pureiron/publish/<owner>/<repo>/` | 승인 사슬 `last_approved`, `approved.log` |

모든 경로는 `publish.py` 인자·환경변수(`PUREIRON_PUBLISH_*`, `PUREIRON_CONTROL_API`)로 바꿀 수 있다(PureIron_V1 위키 runtime-config).

## 이 repo의 secret (Settings → Secrets and variables → Actions)

| secret | 내용 |
|---|---|
| `PUREIRON_CI_APP_ID` | PureIron CI GitHub App ID(private PureIron_V1 도구 checkout) |
| `PUREIRON_CI_APP_KEY` | 같은 App private key(PEM 전체) |
| `PUREIRON_FLEET_VIEWER_TOKEN` | control-api viewer 토큰(check-gen — 게시 권한 없음) |

## 도구 버전

`.github/workflows/*.yml`의 `uses: …@<ref>`가 PureIron_V1 도구 버전이다. 두 파일에 같은 ref를 쓰고, 운영 repo는 PureIron_V1 릴리스 태그로 고정한다. 실행 로그의 "PureIron_V1 도구 커밋"이 실제로 쓴 커밋이다.

## 오류 코드

| 코드 | 어디서 | 뜻 |
|---|---|---|
| `E_FLEET` | check-gen·publish | 계약 스키마·파일명·`.retired` 규칙 위반 |
| `E_GEN` | check-gen | generation 규칙(+1, 게시된 gen보다 큼, 미등록은 1만) 또는 control-api 조회 실패 |
| `E_APPROVAL` | publish | 승인 규칙 위반 — 서명하지 않음 |
| `E_PUBLISH` | publish | 현장별 빌드·게시 실패(미등록 404, gen 충돌 409 등) — 다른 현장은 계속 |
