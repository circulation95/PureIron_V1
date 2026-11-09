# pureiron-fleet

PureIron 현장 배포 상태(DesiredState)의 정본 repo(ADR-022). 현장마다 `sites/<site>.yaml` 하나가 "그 현장에 어떤 제품 release·프로젝트 버전을 어떤 창에 적용할지"를 정한다. main에 승인된 PR이 merge되면 publish 호스트가 서명된 번들(`.pib`)을 만들어 control-api에 게시하고, 현장 Updater가 그것을 받아 적용한다.

```
sites/<site>.yaml            활성 현장 파일(파일명 == site)
sites/.retired/<site>.yaml   퇴역 현장(활성 파일을 그대로 옮긴 것, 이후 수정·삭제·이름 재사용 금지)
examples/site.yaml           현장 파일 견본(sites/ 밖 — check-gen·publish가 보지 않는다)
allowed_approvers.txt        publish 승인자 login 목록
.github/workflows/           check-gen.yml(PR 검사), publish.yml(main → 게시) — PureIron_V1 reusable workflow 호출만
```

## 새 현장 순서 (등록 먼저)

1. **control-api에 현장 등록**(벤더 admin): 현장 Updater가 만든 enrollment로 `pureiron-control site register`. 등록 전에 게시하면 control-api가 404를 돌려주고 publish가 실패한다.
2. `examples/site.yaml`을 `sites/<site>.yaml`로 복사, `site`를 파일명과 같게, `generation: 1`. 견본을 `sites/` 아래에 두지 않는다 — `sites/`의 활성 파일은 모두 게시 대상이라 미등록 견본이 매 publish를 빨강으로 만든다.
3. PR → check-gen 통과 → 승인(아래 "승인하는 법") → merge → publish가 gen 1을 게시.

미등록 상태로 merge했다면: 등록한 뒤 Actions → publish → **Run workflow**(workflow_dispatch, 입력 없음)로 재실행한다. publish는 매번 **main 머리 트리의 모든 활성 현장**을 control-api 게시 상태에 맞춘다(정합화) — 파일 gen > 게시된 최대 gen이면 게시, 같으면 최신(건너뜀), 작으면 역행 실패. 그래서 그 사이 다른 merge가 있었어도 main 머리로 재실행하면 되고, 이미 게시된 현장은 건너뛰므로 재실행은 안전하다. 옛 run의 **Re-run은 쓰지 않는다**(main 머리가 아니면 승인 사슬 끊김). 미등록 현장 파일이 main에 있는 동안은 뒤 merge의 publish도 계속 빨강이다(조용히 넘어가지 않는다). 일시 장애(네트워크·control-api)로 실패한 현장도 같은 방법으로 회복한다.

## 바꾸기·되돌리기·퇴역

- 바꾸기: 그 현장 파일을 고치고 `generation`을 정확히 +1. check-gen이 `new == old + 1`, `new > 게시된 최대 gen`을 본다(`E_GEN`). Free에서는 check-gen이 빨강이어도 merge할 수 있으므로 publish도 merge 뒤 `new == old + 1`을 다시 보고, 어긴 현장은 게시하지 않고 `E_GEN`으로 실패한다. **이 제외는 그 run만이다** — +1은 그 커밋의 변경에만 볼 수 있어서, 다음 merge는 정합화로 그 파일을 다시 본다: gen을 건너뛴 파일(1 → 3)은 다음 merge가 그대로 게시하고, gen을 안 올리고 내용만 바꾼 파일은 "게시본이 최신"으로 보고한다(main 내용 ≠ 게시본). 그래서 **빨강 `E_GEN` run을 보면 바로 gen을 올바르게 올린 PR로 고친다**(다음 merge까지 미루지 않는다).
- 되돌리기: PureIron_V1에서 `uv run python deploy/fleet/revert.py <site> --to-gen N --repo <이 repo>` — 이력의 gen N 내용으로 gen 현재+1 파일·브랜치·PR을 만든다(일반 승인 경로). schema envelope 밖으로 되돌리면 경고가 나오고 현장이 `E_SCHEMA`로 거부한다(DB는 내려가지 않는다).
- 퇴역: `git mv sites/<site>.yaml sites/.retired/<site>.yaml`(내용 그대로). 파일 삭제는 `E_FLEET`. 퇴역한 이름은 다시 쓸 수 없다 — 같은 이름의 활성 파일이 main에 있는 동안 publish는 매 run 그 파일을 `E_FLEET`로 빼고 빨강이다(파일을 지우는 PR로 고친다).
- 쓰기 스위치: `writes: { enabled: true }`(없으면 읽기 전용). Updater 0.2.0 이상만 적용한다. 0.1.0 시절 `updater.env`로 쓰기를 켠 현장은 Updater를 0.2.0으로 바꾸면 그 값이 무시되므로 **Updater 교체 전에** fleet 파일에 `writes.enabled: true`를 먼저 게시한다.
- 현장 드래프트 흡수 확인: publish는 현장 드래프트 상태를 모른다(현장 `E_DRAFT`가 최종 판정). 흡수한 프로젝트 태그를 올리기 전 벤더가 `pureiron-project gate --draft <draft.tar> …`로 확인한다.

## 승인하는 법 (승인자)

GitHub 화면에서 Approve를 골라도 **Submit review를 누르기 전에는 승인이 기록되지 않는다**. 리허설에서 승인했다고 알고 merge한 PR 두 건이 리뷰 기록 0건이었고 publish가 둘 다 `E_APPROVAL`로 막았다. 그래서 승인과 merge를 나눠서 확인한다.

1. PR → Files changed → **Review changes** → Approve 선택 → **Submit review**.
2. PR 대화 탭(Conversation)에 `<승인자> approved these changes`가 보이는지 확인한다. 보이지 않으면 승인이 없는 것이다 — 1부터 다시.
3. 그 뒤에 merge(Squash and merge).

**GitHub Free private repo는 승인 없는 merge 버튼을 막지 못한다**(branch protection 없음, K14). 승인 없이 merge하면 그 merge의 publish가 빨강(`E_APPROVAL … 유효 승인 0건`)이 되고 아무것도 게시되지 않는다. 사슬이 전진하지 않으므로 그다음 merge도 "승인 사슬 끊김"이 된다 — 아래 "`E_APPROVAL` 복구" (a)로 되돌리고 새 PR로 다시 승인받는다.

## 승인 규칙 (publish의 `E_APPROVAL`)

publish는 main 커밋 sha를 GitHub API·git으로 다시 받아 아래를 **모두** 만족할 때만 서명한다. 하나라도 어기면 `E_APPROVAL`(사유 한 줄씩)로 실패하고 아무것도 게시하지 않는다.

1. 그 커밋이 **merge된 PR의 merge commit**이다(squash·merge commit 가능, **squash 권장**. rebase merge는 판정은 하지만 드물게 ⑤로 거부되므로 쓰지 않는다 — 아래 ⑤). PR 없는 main 직접 push는 거부. PR 머리 커밋을 main에 fast-forward push하면 GitHub이 그 PR을 merged로 바꾸므로 이 규칙만으로는 버튼 merge와 구분되지 않는다(리허설 실측) — 승인 없는 머리는 2가, 승인본과 다른 내용은 5가 막고, **승인된 최종 head**의 fast-forward는 내용이 승인본과 같아 통과한다. 그래도 운영은 버튼 merge(squash)만 쓴다.
2. 그 PR의 **최종 head 커밋**에 대한 승인이 1명 이상: 리뷰어의 마지막 상태가 APPROVED(뒤에 Request changes·dismiss가 있으면 무효), 승인한 커밋이 최종 head(승인 뒤 커밋을 더 올리면 다시 승인 필요), 리뷰어가 `allowed_approvers.txt`에 있고 PR 작성자가 아님.
3. `allowed_approvers.txt`는 **merge 직전 main**의 것을 읽는다 — PR이 목록에 사람을 넣어도 그 PR에는 효력이 없다(목록 변경 PR을 먼저 따로 승인·merge).
4. **승인 사슬**: merge 직전 main == publish 호스트가 기록한 마지막 승인 커밋. 승인 없이 들어온 커밋이 한 번 끼면 그 뒤 merge도 거부한다.
5. **트리 동치**: merge 결과 트리 == merge 직전 main과 승인된 head를 충돌 없이 합친 트리(`git merge-tree --write-tree`, publish 호스트 git ≥ 2.38). GitHub 버튼의 squash·merge commit은 같은 3-way merge라 같다. **rebase merge는 드물게 ⑤로 거부된다** — PR 커밋을 하나씩 다시 쌓는 결과가 3-way merge와 다를 수 있다(예: PR 커밋1이 main에 이미 들어간 것과 같은 변경을 하고 커밋2가 그것을 되돌리면, rebase 결과는 main의 승인된 변경을 지운다). 거부되면 사슬이 끊겨 아래 복구가 필요하므로 **repo 설정에서 rebase merge를 끄고 squash만 허용한다**(Settings → General → Pull Requests: "Allow rebase merging" 끔, "Allow squash merging"만 켬). 로컬에서 PR을 merge해 내용을 덧붙이거나 충돌을 풀어 push한 커밋은 거부한다 — 충돌은 PR 브랜치에서 풀고 다시 승인받는다.

### `E_APPROVAL` 복구

거부된 커밋은 사슬을 전진시키지 않는다. 그래서 그 뒤에 새 PR을 그냥 올리면 "승인 사슬 끊김"이 된다. 사유("승인 0건·오래된 승인·목록 밖·트리 불일치·직접 push·승인 사슬 끊김")와 상관없이 복구는 둘 중 하나다.

- (a) **되돌리고 다시 승인**: main을 마지막 승인 커밋(`approved.log` 마지막 줄 또는 오류의 "마지막 승인 …")으로 되돌린다(`git push --force-with-lease origin <마지막 승인 sha>:main`, Free에서 가능). 그다음 원래 PR 브랜치로 **새 PR**을 열어(merge된 PR은 다시 열 수 없다) 정상 승인 → merge. 직접 push된 내용은 PR 브랜치로 옮겨 같은 방법으로 올린다.
  - **PR 브랜치는 이미 지워져 있다**: 이 repo는 merge 때 브랜치를 자동 삭제한다(`delete_branch_on_merge`). 거부된 merge의 PR 머리 커밋(PR 화면의 마지막 커밋 sha)으로 다시 만든다. squash merge·브랜치 삭제 뒤라 그 커밋은 보통 로컬 clone에 없으므로 먼저 받는다: `git fetch origin pull/<PR 번호>/head` → `git push origin <PR 머리 sha>:refs/heads/<브랜치>`(받지 않으면 `src refspec … does not match`로 실패) → 그 브랜치로 새 PR. 되돌린 main과 충돌이 없으면 내용은 그대로다.
  - **되돌리는 force push도 publish를 띄운다**(main push). 결과는 되돌린 대상에 따라 다르다. PR로 만들어진 승인 커밋으로 되돌리면 초록(`게시 0, 건너뜀 1` 등 — 같은 sha 재실행이라 게시할 것이 없다). PR이 없는 커밋(이 템플릿의 첫 커밋, trust-base로만 기준이 된 커밋)으로 되돌리면 빨강(`E_APPROVAL 직접 push — … merged된 PR이 없다`)이지만 **무해하다** — 사슬도 게시도 바뀌지 않는다. 다음 승인 merge가 초록이면 복구된 것이다.
- (b) **사람이 검토하고 기준으로 기록**: 거부된 main 커밋의 내용을 사람이 검토한 뒤 publish 호스트에서 `publish.py trust-base --fleet-repo <owner>/<repo> --sha <검토한 main 커밋> --reason <사유>`. 그 커밋 자체의 publish는 다시 돌려도 ①·②·⑤로 거부되므로, **다음 승인 merge**(내용 없는 PR이어도 된다)가 정합화로 그 커밋의 현장 변경까지 게시한다 — gen을 다시 올릴 필요는 없다.
- 연속 merge로 앞 publish가 아직 안 돈 상태에서 뒤 커밋이 먼저 돌아 "사슬 끊김"이 나면: 앞 run이 끝난 뒤 publish를 workflow_dispatch로 재실행.
- 처음 한 번: 이 템플릿으로 repo를 만든 첫 커밋을 `trust-base`로 등록한다(없으면 "승인 사슬 기준이 없다").
- 승인 사슬 기록(`/srv/pureiron/publish/<owner>/<repo>/`)이 사라지거나 깨지면(전원 차단·수기 오타 — "not a commit sha") 사람이 main 이력을 검토한 뒤 `trust-base`로 다시 만든다(깨진 값은 `previous=<unreadable>`로 남기고 덮어쓴다). 주 1회 백업 대상에 넣는다.
- **publish runner는 하나만** 등록한다(라벨 `publish`). 둘이면 상태 디렉터리 잠금(`lock`)이 사슬 기록을 직렬화하지만 merge 순서대로 돈다는 보장이 없어 "사슬 끊김"이 잦아진다.

## GitHub Free의 한계 (K14) — 왜 workflow 안에서 검사하나

GitHub Free private repo에는 branch protection·필수 리뷰·runner를 특정 workflow에 묶는 기능이 없다. 그래서 승인 판정은 publish 안에서 한다(위 규칙). 이 검사는 **실수와 절차 우회는 막지만, 이 repo에 write 권한이 있는 사람의 의도적 우회는 못 막는다**:

- `pull_request` 이벤트는 PR 브랜치의 workflow 파일을 그대로 돌린다 — PR이 workflow를 고치면 그 코드가 publish runner에서 돈다.
- write 권한자는 아무 브랜치에 `runs-on: publish` workflow를 push해 승인 없이 그 runner에서 코드를 돌릴 수 있다.

→ **runner 사용자가 읽을 수 있는 키·토큰은 이 repo write 권한자 전원이 읽을 수 있다.** 지금은 write 권한자가 벤더뿐이라 수용한다(PoC, ★F1). 이 수용 범위를 지키려면:

- **fork PR workflow 실행은 끈다**(Settings → Actions → General → "Fork pull request workflows"는 꺼진 상태 유지). 켜면 read 권한자(쓰기 없음)의 fork PR도 check-gen으로 publish runner에서 코드를 돌려 키를 읽을 수 있다 — K14 범위 밖.
- **collaborator는 write 권한자만** 둔다(read 전용 collaborator를 두지 않는다).
- 리허설·운영 시작 때 위 두 설정을 확인하고 결과를 남긴다.

판정의 알려진 한계(문서화만 함):

- 승인자가 남의 PR에 직접 커밋을 push한 뒤 자기가 승인하면 그 커밋은 두 번째 사람의 검토 없이 승인된다(GitHub "most recent push" 규칙과 같은 문제). 지금은 승인자 = 벤더 1인이라 수용한다 — 승인자는 남의 PR 브랜치에 push하지 않는다.

### 서명 경계를 실제로 분리하는 조건 (★F1 전환)

다음 중 하나가 생기면 **그 전에** 서명을 전용 사용자(예: `pureiron-signer`)로 분리한다.

- 외부인(고객 엔지니어·협력사)에게 이 repo write 권한을 준다.
- 서명 호스트를 개발 PC가 아닌 곳으로 옮긴다(운영 publish 호스트).

지금의 `publish.py`를 그대로 `sudo -u pureiron-signer` 뒤로 옮기기만 해서는 **경계가 되지 않는다**. runner 사용자(= 공격자로 가정)가 인자 전부(`--github-api`·`--git-base`·`--state-dir`·`--keys-dir`·`--releases`·`--store` 등 — 가짜 승인 응답·위조 사슬·임의 이미지)와 환경변수, 그리고 실행되는 코드·venv(runner가 checkout·`uv sync`한 것)를 정하기 때문이다. 전환할 때 필요한 것:

1. **root 소유 설치본**: 고정 경로의 PureIron_V1 도구와 venv(root 소유, runner·signer 쓰기 불가). workflow는 runner checkout이 아니라 이 설치본을 부른다.
2. **signer 모드**: 보안 관련 설정(GitHub API·git base·승인 사슬 state·keys·releases·image store·project owner·허용 fleet repo·control-api·토큰 파일)을 **root 소유 설정 파일에서만** 읽고, argv는 `--fleet-repo`·`--sha`만 받으며 env는 무시한다. sudoers에는 sha 형식을 검증하는 래퍼 한 줄만 허용한다. (아직 구현하지 않았다 — PureIron_V1 백로그 `--signer-config`.)
3. **`trust-base`는 sudoers에서 뺀다** — 사람만 signer 사용자로 직접 실행한다.
4. 키·토큰 파일은 signer 사용자 소유 0600, runner 사용자는 못 읽는다.

## publish 호스트 배치 (F7, 벤더)

publish·check-gen은 self-hosted runner(라벨 `publish`, 전용 사용자 `pureiron-publish` — sudo·docker 그룹 없음)에서 돈다. control-api가 그 호스트 안에 있어 GitHub-hosted runner는 닿지 못한다.

| 경로 | 내용 |
|---|---|
| `/etc/pureiron/keys/release.key` | release key(0600, `pureiron-publish` 소유) |
| `/etc/pureiron/keys/{release-cert.json, release-cert.sig, root.pub}` | release 인증서·root 공개키(번들 자체 검증) |
| `/etc/pureiron/publish/ci_token` | control-api ci 토큰(0600, `PUT /bundles`) |
| `/etc/pureiron/publish/viewer_token` | control-api viewer 토큰(0600, `GET /fleet` — ci는 fleet을 못 읽는다) |
| `/etc/pureiron/publish/app.pem` | GitHub App private key(0600, fleet·프로젝트 repo 읽기 토큰용) |
| `/etc/pureiron/publish/app_id` | (선택) 숫자 App ID(Client ID 아님) — workflow가 env `PUREIRON_PUBLISH_APP_ID`로 주면 불필요 |
| `/srv/pureiron/images/` | 이미지 store(`PUREIRON_IMAGE_STORE`, release별 `<release>/<svc>.tar`) |
| `/srv/pureiron/bundles/<site>/` | 만든 `.pib`(USB 반입용 사본) |
| `/srv/pureiron/metrics/` | 게시 기록 `publish-<site>-g<gen>.json` |
| `/srv/pureiron/publish/<owner>/<repo>/` | 승인 사슬 `last_approved`, `approved.log`, 잠금 `lock`, 프로젝트 태그별 직전 게시 커밋 `project_commits.json`(태그 이동 경고) |

publish는 시작할 때 umask `027`을 건다 — 새로 만드는 사슬·번들·메트릭은 파일 0640·디렉터리 0750(`pureiron-publish` 그룹 멤버가 사슬 기준을 고칠 수 없다). 이 규칙 전에 만들어진 파일(리허설에서 0664)은 바뀌지 않으므로 publish 호스트에서 한 번 고친다:

```bash
sudo chmod -R g-w,o-rwx /srv/pureiron/publish /srv/pureiron/bundles /srv/pureiron/metrics
```

그래서 `/srv/pureiron/bundles`의 `.pib`(USB 반입용 사본, 파일 0640·디렉터리 0750)는 `pureiron-publish`와 그 그룹만 읽는다 — USB 사본을 꺼낼 운영자는 `pureiron-publish` 그룹에 넣거나(`sudo usermod -aG pureiron-publish <운영자>`, 다시 로그인) `sudo cp`로 꺼낸다.

같은 sha의 재실행(Re-run·workflow_dispatch)은 `approved.log`에 줄을 더하지 않는다. `trust-base`는 같은 sha여도 사유 줄을 남긴다.

모든 경로는 `publish.py` 인자·환경변수(`PUREIRON_PUBLISH_*`, `PUREIRON_CONTROL_API`)로 바꿀 수 있다(PureIron_V1 위키 runtime-config).

## 이 repo의 secret (Settings → Secrets and variables → Actions)

| secret | 내용 |
|---|---|
| `PUREIRON_CI_APP_ID` | PureIron CI GitHub App ID(private PureIron_V1 도구 checkout). **숫자 App ID만** 넣는다(App 설정 화면 "App ID" — `Iv23…`로 시작하는 Client ID가 아니다). publish가 이 값을 10진 숫자로 검사한다 |
| `PUREIRON_CI_APP_KEY` | 같은 App private key(PEM 전체) |
| `PUREIRON_FLEET_VIEWER_TOKEN` | control-api viewer 토큰(check-gen — 게시 권한 없음) |

## 도구 버전

`.github/workflows/*.yml`의 `uses: …@<ref>`가 PureIron_V1 도구 버전이다. 두 파일에 같은 ref를 쓰고, 운영 repo는 PureIron_V1 릴리스 태그로 고정한다(이 템플릿은 `@r5`). 실행 로그의 "PureIron_V1 도구 커밋"이 실제로 쓴 커밋이다.

- 도구 버전이 publish가 해석하는 제품 release 목록을 정한다 — publish는 도구 checkout의 `deploy/releases/<이름>.json`으로 현장 파일의 `product:`를 찾는다. 그래서 fleet 파일에 새 `product:`(예: r6의 달력 버전)를 쓰기 **전에** 그 기록이 들어 있는 태그로 ref를 올린다. 옛 ref로는 그 현장이 `E_PUBLISH`(`expected exactly one release record with version …, got 0`)로 실패한다.
- 태그 `r5`부터는 **그 release 기록(`deploy/releases/r5.json`)이 들어 있는 커밋**(PureIron_V1 마감 PR의 merge 커밋)에 찍는다. 기록의 `commit`(이미지를 빌드한 커밋)에는 그 기록 파일이 아직 없어서, 거기에 태그를 찍으면 이 repo가 그 release를 게시하지 못한다.
- ref를 바꾸는 것도 이 repo의 변경이다 — 일반 PR → check-gen → 승인 → merge로 올린다(직접 push하면 승인 사슬이 끊긴다).

## control-api 주소 (HTTPS + 사설 CA)

workflow의 `control_api` 입력이 check-gen·publish가 부르는 control-api 기준 URL이다(publish 호스트에서 닿는 주소). 입력을 주지 않으면 reusable workflow 기본값 `http://127.0.0.1:8100/api/v1`(리허설용 단독 control-api)이다. 중앙을 `deploy/central` compose(Caddy HTTPS 포트 하나)로 돌리면 두 workflow에 같은 값을 준다:

```yaml
    with:
      runs_on: '["self-hosted", "publish"]'
      control_api: https://<중앙 주소>:28443/api/v1
```

중앙 인증서는 PoC 동안 Caddy 사설 CA(`tls internal`)가 발급한다. publish·check-gen(Python `http.client`)은 기본 SSL 설정으로 **OS 신뢰 저장소**를 쓰므로, publish 호스트에 그 루트 CA를 한 번 넣는다(현장 Updater와 같은 규칙):

```bash
# 중앙에서 내보낸 루트 CA(PureIron_V1 deploy/central/README.md "TLS와 루트 CA 내보내기")
openssl x509 -in pureiron-central-root.crt -noout -fingerprint -sha256   # 중앙에서 따로 받은 지문과 대조
sudo install -m 0644 pureiron-central-root.crt /usr/local/share/ca-certificates/pureiron-central-root.crt
sudo update-ca-certificates
curl -fsS https://<중앙 주소>:28443/api/v1/healthz                           # --cacert 없이 200
```

`SSL_CERT_FILE`·`REQUESTS_CA_BUNDLE`로 CA를 지정하지 않는다 — 같은 프로세스의 GitHub 호출까지 신뢰 대상이 그 파일 하나로 바뀐다. 중앙 주소를 바꾸면 ref 변경과 같이 PR로 올리고, `control_api`를 바꾸는 merge의 publish는 이미 새 주소를 쓴다(workflow 파일은 merge된 main의 것). 공인 인증서(M6)로 가면 이 CA 절차는 없어진다.

## 오류 코드

| 코드 | 어디서 | 뜻 |
|---|---|---|
| `E_FLEET` | check-gen·publish | 계약 스키마·파일명·`.retired` 규칙 위반 |
| `E_GEN` | check-gen·publish | generation 규칙(+1, 게시된 gen보다 큼, 미등록은 1만) 또는 control-api 조회 실패. publish는 merge 뒤 +1만 다시 본다 |
| `E_APPROVAL` | publish | 승인 규칙(1~5) 위반·판정 입력 실패 — 서명하지 않음 |
| `E_PUBLISH` | publish | 현장별 빌드·게시 실패(미등록 404, gen 충돌 409 등) — 다른 현장은 계속 |
