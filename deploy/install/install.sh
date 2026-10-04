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
eval "$("$PY" - "$HERE/manifest.json" <<'PYEOF'
import json, re, shlex, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
assert m["kind"] == "install"
version = m["updater"]["version"]
assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
for key, value in (
    ("RELEASE", m["release"]),
    ("UPDATER_VERSION", version),
    ("UPDATER_TAR", m["updater"]["file"]),
    ("COMPOSE_MIN", m["docker"]["compose_min"]),
):
    print(f"{key}={shlex.quote(value)}")
PYEOF
)"
VENV="$OPT/$UPDATER_VERSION"

# ── 되돌리기 기록 ──────────────────────────────────────────────────────────────
CREATED=()          # 이번 실행이 새로 만든 파일·디렉터리(역순으로 지운다)
DEBS_INSTALLED=0
PREVIOUS_CURRENT=""
UNIT_ENABLED=0
DONE=0

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
  if [ "$DEBS_INSTALLED" = 1 ]; then
    dpkg -r docker-compose-plugin docker-ce docker-ce-cli containerd.io >&2
  fi
  local i
  for ((i = ${#CREATED[@]} - 1; i >= 0; i--)); do rm -rf -- "${CREATED[$i]}"; done
  systemctl daemon-reload >/dev/null 2>&1
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

version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" = "$2" ]; }

# ── 3·4 호스트·Docker ─────────────────────────────────────────────────────────
if [ "$UPDATER_ONLY" = 0 ]; then
  log "3 preflight"
  bash "$HERE/install/preflight.sh" --root "$STATE_ROOT" "${PREFLIGHT_ARGS[@]}"

  log "4 docker (classic image store, PD-19)"
  install_file "$HERE/install/daemon.json" "$DAEMON_JSON" 0644
  deb_files=()
  for pkg in "${DEBS[@]}"; do deb_files+=("$HERE/debs/$pkg.deb"); done
  DEBS_INSTALLED=1
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
  # 서명된 tarball의 최상위는 <ver>/ 하나여야 한다.
  if tar -tzf "$HERE/$UPDATER_TAR" | grep -qv "^$UPDATER_VERSION/"; then
    echo "install: $UPDATER_TAR has entries outside $UPDATER_VERSION/" >&2
    false
  fi
  rm -rf "$VENV"
  remember "$VENV"
  tar -xzf "$HERE/$UPDATER_TAR" -C "$OPT" --no-same-owner
  ln -sfn "$UPDATER_VERSION" "$OPT/current.new"
  mv -Tf "$OPT/current.new" "$OPT/current"
  [ -n "$PREVIOUS_CURRENT" ] || remember "$OPT/current"
fi
if [ ! -L "$CLI_LINK" ]; then
  mkdir -p "$(dirname "$CLI_LINK")"
  ln -s /opt/pureiron/updater/current/bin/pureiron-site "$CLI_LINK"
  remember "$CLI_LINK"
fi
install_file "$HERE/systemd/pureiron-updater.service" "$UNIT" 0644
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
site secret set db_password --generate --if-missing
site secret set updater_token --generate --if-missing

DONE=1
trap - ERR
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
