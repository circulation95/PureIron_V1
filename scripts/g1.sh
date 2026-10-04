#!/usr/bin/env bash
# G1 게이트 러너(ADR-028). 로컬과 CI(.github/workflows/foundation-ci.yml)가 같은 job 이름·명령을 쓴다.
#
#   bash scripts/g1.sh <job> [<job> ...]
#   bash scripts/g1.sh all          # 항상 실행 job 전부(통합·조건부 job 제외)
#
# 항상 실행: lint-type import-linter contracts-diff contracts-drift migration-lint unit web
#            no-customer-branching integration-compose
# 조건부:    n-minus-1 repro-build  (CI가 경로·라벨·schedule로 판단해 호출)
# 보조:      contracts-gen(생성물 갱신), images(이미지 빌드)
#
# 환경변수: BASE_REF(기본 origin/master) — contracts-diff·migration-lint 비교 기준.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONUTF8=1
BASE_REF="${BASE_REF:-origin/master}"
PY_PACKAGES=(
  services/core
  services/collector
  services/updater
  services/control-api
  packages/project-tools
  packages/bundle-tools
  packages/contracts
)
SCHEMAS=(project manifest fleet-site)
OPENAPI_SERVICES=(core control-api)
GENERATED_PATHS=(
  packages/contracts/openapi
  services/core/src/pureiron_core/_gen
  services/collector/src/pureiron_collector/_gen
  services/web/src/api/schema.d.ts
)

log() { printf '\n== %s\n' "$*"; }
notice() { printf '::notice::%s\n' "$*"; }
fail() { printf '::error::%s\n' "$*" >&2; exit 1; }

tools_bin() { bash "$ROOT/scripts/tools.sh" | tail -1; }

job_lint_type() {
  log "lint-type"
  uv sync --all-packages --locked
  uv run ruff check services packages deploy
  uv run ruff format --check services packages deploy
  local p
  for p in "${PY_PACKAGES[@]}"; do
    uv run pyright -p "$p"
  done
  job_no_suppressions
}

job_no_suppressions() {
  log "no-suppressions (억제 주석·app.state 0건)"
  local hits
  hits="$(grep -rnE 'app\.state|type: ?ignore|pyright: ?ignore|# ?noqa|eslint-disable|as any|@ts-ignore|@ts-expect-error' \
    services packages deploy \
    --include='*.py' --include='*.pyi' --include='*.ts' --include='*.tsx' --include='*.js' --include='*.mjs' \
    --exclude-dir=_gen --exclude-dir=node_modules --exclude-dir=dist || true)"
  [ -z "$hits" ] || fail "suppression/app.state found:
$hits"
}

job_import_linter() {
  log "import-linter"
  (cd services/core && uv run lint-imports --no-cache)
  uv run pytest services/core/tests/test_importlinter_negative.py -q
}

job_unit() {
  log "unit"
  uv run pytest services packages -q
}

job_contracts_gen() {
  log "contracts-gen"
  uv run python packages/contracts/tools/export_openapi.py
  uv run python packages/contracts/tools/gen_proto.py
  if [ -d services/web ]; then
    (cd services/web && { [ -d node_modules ] || npm ci --no-audit --no-fund; } \
      && npx openapi-typescript ../../packages/contracts/openapi/core.json -o src/api/schema.d.ts)
  fi
}

job_contracts_drift() {
  job_contracts_gen
  log "contracts-drift"
  local dirty
  dirty="$(git status --porcelain -- "${GENERATED_PATHS[@]}")"
  if [ -n "$dirty" ]; then
    git --no-pager diff -- "${GENERATED_PATHS[@]}" | head -200
    fail "generated contracts drifted from sources (run: bash scripts/g1.sh contracts-gen):
$dirty"
  fi
}

base_has() { git cat-file -e "$BASE_REF:$1" 2>/dev/null; }

job_contracts_diff() {
  log "contracts-diff (base: $BASE_REF)"
  local bin
  bin="$(tools_bin)"
  "$bin/buf" lint packages/contracts
  git rev-parse --verify --quiet "$BASE_REF" >/dev/null || fail "base ref $BASE_REF not found (git fetch origin master)"
  local base_sha tmp
  base_sha="$(git rev-parse "$BASE_REF")"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  if base_has packages/contracts/collector.proto; then
    "$bin/buf" breaking packages/contracts --against ".git#ref=$base_sha,subdir=packages/contracts"
  else
    notice "collector.proto not on $BASE_REF yet — buf breaking skipped (bootstrap)"
  fi

  local s
  for s in "${OPENAPI_SERVICES[@]}"; do
    local f="packages/contracts/openapi/$s.json"
    if base_has "$f"; then
      git show "$BASE_REF:$f" > "$tmp/$s.json"
      "$bin/oasdiff" breaking "$tmp/$s.json" "$f" --fail-on ERR
    else
      notice "$f not on $BASE_REF yet — oasdiff skipped (bootstrap)"
    fi
  done

  for s in "${SCHEMAS[@]}"; do
    local f="packages/contracts/$s.schema.json"
    if base_has "$f"; then
      git show "$BASE_REF:$f" > "$tmp/$s.schema.json"
      uv run python packages/contracts/tools/schema_diff.py "$tmp/$s.schema.json" "$f"
    else
      notice "$f not on $BASE_REF yet — schema diff skipped (bootstrap)"
    fi
  done
}

job_migration_lint() {
  log "migration-lint (base: $BASE_REF)"
  uv run pureiron-core migrate lint services/core/migrations --base-ref "$BASE_REF"
}

job_no_customer_branching() {
  log "no-customer-branching"
  local hits
  hits="$(grep -rnE '(project_id|site_id)\s*==\s*["'"'"']' services/*/src packages/*/src \
    --exclude-dir=_gen --exclude-dir=node_modules || true)"
  [ -z "$hits" ] || fail "customer branching found:
$hits"
  local name
  while IFS= read -r name; do
    case "$name" in ''|'#'*) continue ;; esac
    hits="$(grep -rnIiF -- "$name" services/*/src packages/*/src --exclude-dir=_gen --exclude-dir=node_modules || true)"
    [ -z "$hits" ] || fail "customer name literal '$name' in product source:
$hits"
  done < deploy/lint/customer-names.txt
}

job_web() {
  log "web"
  (cd services/web && npm ci --no-audit --no-fund && npx tsc --noEmit && npx eslint . --max-warnings 0 && npm run build)
}

job_images() {
  log "images"
  bash deploy/build/build_images.sh
}

job_integration_compose() {
  log "integration-compose"
  bash deploy/build/build_images.sh
  uv run pytest services/core/tests/integration -m integration -q
}

job_n_minus_1() {
  log "n-minus-1"
  uv run python deploy/build/n_minus_1.py
}

job_repro_build() {
  log "repro-build"
  uv run python deploy/build/repro_build.py
}

run_job() {
  case "$1" in
    lint-type) job_lint_type ;;
    no-suppressions) job_no_suppressions ;;
    import-linter) job_import_linter ;;
    unit) job_unit ;;
    contracts-gen) job_contracts_gen ;;
    contracts-drift) job_contracts_drift ;;
    contracts-diff) job_contracts_diff ;;
    migration-lint) job_migration_lint ;;
    no-customer-branching) job_no_customer_branching ;;
    web) job_web ;;
    images) job_images ;;
    integration-compose) job_integration_compose ;;
    n-minus-1) job_n_minus_1 ;;
    repro-build) job_repro_build ;;
    all)
      local j
      for j in lint-type import-linter unit contracts-drift contracts-diff migration-lint \
               no-customer-branching web; do
        run_job "$j"
      done ;;
    *) fail "unknown job: $1" ;;
  esac
}

[ "$#" -gt 0 ] || { sed -n '2,13p' "$0"; exit 2; }
for job in "$@"; do
  run_job "$job"
done
