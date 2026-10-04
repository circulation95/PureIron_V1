#!/usr/bin/env bash
# 설치(plan M2 설치 번들 3~5a단계). bootstrap-verify.sh가 검증해 푼 디렉터리에서 root로 실행한다:
#
#   sudo bash <dest>/install/install.sh --bundle pureiron-install-<release>.pib \
#        [--offline-time-confirmed] [--updater-only]
#
# 3  preflight.sh
# 4  /etc/docker/daemon.json(PD-19 classic image store) → dpkg -i debs/*.deb → docker enable --now
#    → image store·compose 최소 버전 확인. daemon.json은 deb보다 먼저 둔다: 패키지 postinst가
#    dockerd를 처음 띄울 때 이미 classic store여야 한다(최근 Docker는 새 설치 기본이 containerd store).
# 5  Updater venv → /opt/pureiron/updater/<ver> + current symlink, root.pub → /etc/pureiron/root.pub,
#    updater.env 템플릿, systemd unit enable(start는 첫 apply 뒤 운영자가).
# 5a 설치된 Updater로 설치 번들 재검증(`pureiron-site bundle verify --kind install`, bundle-tools).
#    이후 secret db_password·updater_token을 없을 때만 생성.
# 어느 단계든 실패하면 이번 실행이 만든 것을 되돌린다(dpkg -r·venv·symlink·unit·키·daemon.json).
#
# --updater-only: Updater 교체(수동 유지보수, spec L57) — preflight·daemon.json·deb를 건너뛰고
#   5·5a만 한다. 실패하면 이전 current로 되돌린다. 서비스가 돌고 있었으면 재시작한다.
#
# PUREIRON_PREFIX(테스트 전용, 운영에서는 비움): /etc·/opt·/var/lib·/usr/local 앞에 붙는 루트.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
P="${PUREIRON_PREFIX:-}"
PY="${PUREIRON_PYTHON:-python3}"
BUNDLE=""
UPDATER_ONLY=0
PREFLIGHT_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bundle) BUNDLE="$2"; shift 2 ;;
    --offline-time-confirmed) PREFLIGHT_ARGS+=(--offline-time-confirmed); shift ;;
    --updater-only) UPDATER_ONLY=1; shift ;;
    *) echo "usage: install.sh --bundle <install.pib> [--offline-time-confirmed] [--updater-only]" >&2; exit 2 ;;
  esac
done
[ -n "$BUNDLE" ] && [ -f "$BUNDLE" ] || { echo "install: --bundle <install.pib> is required" >&2; exit 2; }
BUNDLE="$(cd "$(dirname "$BUNDLE")" && pwd)/$(basename "$BUNDLE")"
if [ -z "$P" ] && [ "$(id -u)" -ne 0 ]; then
  echo "install: run as root (sudo)" >&2
  exit 2
fi

ETC="$P/etc/pureiron"
OPT="$P/opt/pureiron/updater"
STATE_ROOT="$P/var/lib/pureiron"
UNIT="$P/etc/systemd/system/pureiron-updater.service"
DAEMON_JSON="$P/etc/docker/daemon.json"
CLI_LINK="$P/usr/local/bin/pureiron-site"
DEBS=(containerd.io docker-ce-cli docker-ce docker-compose-plugin)

log() { printf '== %s\n' "$*"; }

# manifest(이미 bootstrap-verify가 서명 확인) → 셸 변수
# python은 격리 모드(-I): 현재 디렉터리·환경 변수의 모듈 경로를 쓰지 않는다. 종료 코드를 먼저 본다.
MANIFEST_VARS="$("$PY" -I - "$HERE/manifest.json" <<'PYEOF'
import json, re, shlex, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
assert m["kind"] == "install"
version = m["updater"]["version"]
assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
assert m["updater"]["file"] == f"updater/updater-{version}.tar.gz"
assert m["updater"]["file"] in m["files"]
for name, value in m["docker"]["packages"].items():
    assert re.fullmatch(r"[a-z0-9][a-z0-9.+-]{1,63}", name), name
    assert re.fullmatch(r"[0-9A-Za-z.+~:-]{1,128}", value), value
for key, value in (
    ("RELEASE", m["release"]),
    ("UPDATER_VERSION", version),
    ("UPDATER_TAR", m["updater"]["file"]),
    ("COMPOSE_MIN", m["docker"]["compose_min"]),
):
    print(f"{key}={shlex.quote(value)}")
packages = " ".join(f"[{shlex.quote(k)}]={shlex.quote(v)}" for k, v in sorted(m["docker"]["packages"].items()))
print(f"declare -A DEB_VERSIONS=({packages})")
PYEOF
)" || { echo "install: cannot read $HERE/manifest.json" >&2; exit 1; }
eval "$MANIFEST_VARS"
VENV="$OPT/$UPDATER_VERSION"

# ── 되돌리기 기록 ──────────────────────────────────────────────────────────────
CREATED=()          # 이번 실행이 새로 만든 파일·디렉터리(역순으로 지운다)
RESTORE=()          # "백업:원래 경로" — 이번 실행이 바꾼 기존 파일(--updater-only의 unit)
NEW_DEBS=()         # 이번 실행 전에 없던 Docker 패키지(되돌릴 때 이것만 dpkg -r)
PREVIOUS_CURRENT=""
UNIT_ENABLED=0
DONE=0
BACKUP_DIR="$(mktemp -d)"

remember() { CREATED+=("$1"); }

rollback() {
  local status=$?
  [ "$DONE" = 1 ] && return
  set +e
  echo "install: FAILED (exit $status) — rolling back this run" >&2
  if [ "$UNIT_ENABLED" = 1 ]; then systemctl disable pureiron-updater >/dev/null 2>&1; fi
  if [ -n "$PREVIOUS_CURRENT" ]; then
    ln -sfn "$PREVIOUS_CURRENT" "$OPT/current.new" && mv -Tf "$OPT/current.new" "$OPT/current"
  fi
  if [ "${#NEW_DEBS[@]}" -gt 0 ]; then
    if [[ " ${NEW_DEBS[*]} " == *" docker-ce "* ]]; then systemctl disable --now docker >&2; fi
    local remove=() pkg
    for pkg in docker-compose-plugin docker-ce docker-ce-cli containerd.io; do
      if [[ " ${NEW_DEBS[*]} " == *" $pkg "* ]]; then remove+=("$pkg"); fi
    done
    dpkg -r "${remove[@]}" >&2
  fi
  local i entry
  for ((i = ${#CREATED[@]} - 1; i >= 0; i--)); do rm -rf -- "${CREATED[$i]}"; done
  for entry in "${RESTORE[@]}"; do cp -p -- "${entry%%:*}" "${entry#*:}"; done
  systemctl daemon-reload >/dev/null 2>&1
  rm -rf "$BACKUP_DIR"
  echo "install: rolled back" >&2
  exit "$status"
}
trap rollback ERR

install_file() {  # install_file <src> <dst> <mode> — 같은 내용이면 그대로, 다르면 실패
  local src="$1" dst="$2" mode="$3"
  if [ -e "$dst" ]; then
    cmp -s "$src" "$dst" && return 0
    echo "install: $dst exists with different content — refusing to replace it" >&2
    return 1
  fi
  mkdir -p "$(dirname "$dst")"
  install -m "$mode" "$src" "$dst"
  remember "$dst"
}

replace_file() {  # replace_file <src> <dst> <mode> — 다르면 백업하고 교체(되돌릴 때 복원)
  local src="$1" dst="$2" mode="$3"
  [ -e "$dst" ] || { install_file "$src" "$dst" "$mode"; return; }
  cmp -s "$src" "$dst" && return 0
  local backup
  backup="$BACKUP_DIR/$(basename "$dst").$RANDOM"
  cp -p -- "$dst" "$backup"
  RESTORE+=("$backup:$dst")
  install -m "$mode" "$src" "$dst"
}

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" = "$2" ]; }

deb_status() { dpkg-query -W -f='${Status} ${Version}' "$1" 2>/dev/null || true; }

# ── 3·4 호스트·Docker ─────────────────────────────────────────────────────────
if [ "$UPDATER_ONLY" = 0 ]; then
  log "3 preflight"
  bash "$HERE/install/preflight.sh" --root "$STATE_ROOT" "${PREFLIGHT_ARGS[@]}"

  log "4 docker (classic image store, PD-19)"
  # 이미 깔린 Docker 패키지는 번들과 같은 버전일 때만 둔다(재실행). 다르면 바꾸기 전에 멈춘다 —
  # 되돌릴 때 원래 버전을 복원할 deb가 없다.
  missing=()
  for pkg in "${DEBS[@]}"; do
    status="$(deb_status "$pkg")"
    case "$status" in
      "install ok installed ${DEB_VERSIONS[$pkg]}") ;;
      "install ok installed "*)
        echo "install: $pkg ${status##* } is already installed; the bundle has ${DEB_VERSIONS[$pkg]} — remove it first" >&2
        false ;;
      *) missing+=("$pkg") ;;
    esac
  done
  install_file "$HERE/install/daemon.json" "$DAEMON_JSON" 0644
  deb_files=()
  for pkg in "${DEBS[@]}"; do deb_files+=("$HERE/debs/$pkg.deb"); done
  NEW_DEBS=("${missing[@]}")  # 실제로 dpkg -i 하기 직전에만 기록(그 전에 멈추면 지울 것 없음)
  dpkg -i "${deb_files[@]}"
  systemctl enable --now docker
  driver="$(docker info --format '{{.Driver}}')"
  if [ "$driver" != overlay2 ]; then
    echo "install: docker storage driver is '$driver', need overlay2 (classic store)" >&2
    false
  fi
  compose="$(docker compose version --short)"
  compose="${compose#v}"
  if ! version_ge "$compose" "$COMPOSE_MIN"; then
    echo "install: docker compose $compose < required $COMPOSE_MIN" >&2
    false
  fi
  echo "docker: $driver, compose $compose"
fi

# ── 5 Updater ────────────────────────────────────────────────────────────────
log "5 updater $UPDATER_VERSION"
install_file "$HERE/keys/root.pub" "$ETC/root.pub" 0644
if [ ! -e "$ETC/updater.env" ]; then
  install -D -m 0644 "$HERE/install/updater.env" "$ETC/updater.env"
  remember "$ETC/updater.env"
fi
mkdir -p "$OPT"
if [ -L "$OPT/current" ]; then PREVIOUS_CURRENT="$(readlink "$OPT/current")"; fi
if [ "$PREVIOUS_CURRENT" != "$UPDATER_VERSION" ]; then
  # 서명된 tarball의 최상위는 <ver>/ 하나여야 한다(목록을 끝까지 받은 뒤 검사 — 파이프 조기
  # 종료(SIGPIPE)가 pipefail로 검사를 통과시키지 않게).
  entries="$(tar --force-local -tzf "$HERE/$UPDATER_TAR")"
  outside=""
  while IFS= read -r entry; do
    case "$entry" in "$UPDATER_VERSION"/*) ;; *) outside+="$entry " ;; esac
  done <<<"$entries"
  if [ -n "$outside" ]; then
    echo "install: $UPDATER_TAR has entries outside $UPDATER_VERSION/: $outside" >&2
    false
  fi
  rm -rf "$VENV"
  remember "$VENV"
  tar --force-local -xzf "$HERE/$UPDATER_TAR" -C "$OPT" --no-same-owner
  ln -sfn "$UPDATER_VERSION" "$OPT/current.new"
  mv -Tf "$OPT/current.new" "$OPT/current"
  [ -n "$PREVIOUS_CURRENT" ] || remember "$OPT/current"
fi
if [ ! -L "$CLI_LINK" ]; then
  mkdir -p "$(dirname "$CLI_LINK")"
  ln -s /opt/pureiron/updater/current/bin/pureiron-site "$CLI_LINK"
  remember "$CLI_LINK"
fi
if [ "$UPDATER_ONLY" = 1 ]; then
  # Updater 교체는 새 릴리스의 unit으로 바꾼다(실패 시 복원). 신뢰 루트(root.pub)는 바꾸지 않는다.
  replace_file "$HERE/systemd/pureiron-updater.service" "$UNIT" 0644
else
  install_file "$HERE/systemd/pureiron-updater.service" "$UNIT" 0644
fi
systemctl daemon-reload
if ! systemctl is-enabled --quiet pureiron-updater; then
  systemctl enable pureiron-updater
  UNIT_ENABLED=1
fi

# ── 5a 설치된 Updater(bundle-tools)로 재검증 ─────────────────────────────────
log "5a re-verify with the installed updater"
site() {
  PUREIRON_ROOT="$STATE_ROOT" PUREIRON_ROOT_PUB="$ETC/root.pub" PUREIRON_ENV_FILE="$ETC/updater.env" \
    "$OPT/current/bin/pureiron-site" "$@"
}
site bundle verify "$BUNDLE" --kind install
for name in db_password updater_token; do
  secret="$STATE_ROOT/state/secrets/$name"
  [ -e "$secret" ] || remember "$secret"
  site secret set "$name" --generate --if-missing
done

DONE=1
trap - ERR
rm -rf "$BACKUP_DIR"
if [ "$UPDATER_ONLY" = 1 ] && systemctl is-active --quiet pureiron-updater; then
  systemctl restart pureiron-updater
fi
cat <<EOF
install: OK — release $RELEASE, updater $UPDATER_VERSION
next:
  sudo pureiron-site enroll --site <site id> --out <usb>/enroll-<site id>.json
  (online site) sudo pureiron-site secret set control_api_token < token.txt
  sudo pureiron-site apply pureiron-<site id>-g<gen>.pib
  sudo systemctl start pureiron-updater
EOF
