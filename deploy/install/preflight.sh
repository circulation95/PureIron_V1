#!/usr/bin/env bash
# 설치 전 호스트 점검(plan M2 설치 번들 3단계). install.sh가 부르며 단독 실행도 된다.
#
#   bash install/preflight.sh [--offline-time-confirmed] [--root /var/lib/pureiron] [--min-free-gib 50]
#
# 점검: Ubuntu 24.04·x86_64, /usr/bin/python3.12, --root 여유 공간, 포트 80/443 미사용,
# NTP 동기화(폐쇄망은 운영자가 시계를 확인했다는 --offline-time-confirmed), Docker deb 의존
# 패키지(iptables·libseccomp2) 있음, 충돌 패키지(docker.io·containerd·runc·podman-docker) 없음.
# 모두 출력하고 하나라도 실패면 종료 코드 1.
#
# 테스트용 덮어쓰기: PREFLIGHT_OS_RELEASE(기본 /etc/os-release), PREFLIGHT_PYTHON(기본
# /usr/bin/python3.12). 나머지는 PATH의 uname·df·ss·timedatectl·dpkg-query를 쓴다.
set -uo pipefail

ROOT=/var/lib/pureiron
MIN_FREE_GIB=50
OFFLINE_TIME=0
OS_RELEASE="${PREFLIGHT_OS_RELEASE:-/etc/os-release}"
PYTHON="${PREFLIGHT_PYTHON:-/usr/bin/python3.12}"
FAILED=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --offline-time-confirmed) OFFLINE_TIME=1; shift ;;
    --root) ROOT="$2"; shift 2 ;;
    --min-free-gib) MIN_FREE_GIB="$2"; shift 2 ;;
    *) echo "usage: preflight.sh [--offline-time-confirmed] [--root DIR] [--min-free-gib N]" >&2; exit 2 ;;
  esac
done

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; }

# OS
os_id="$(. "$OS_RELEASE" 2>/dev/null && printf '%s %s' "${ID:-}" "${VERSION_ID:-}")"
if [ "$os_id" = "ubuntu 24.04" ]; then pass "os: Ubuntu 24.04"; else fail "os: need Ubuntu 24.04, found '${os_id}'"; fi

arch="$(uname -m)"
if [ "$arch" = x86_64 ]; then pass "arch: x86_64"; else fail "arch: need x86_64, found $arch"; fi

# Updater venv는 /usr/bin/python3.12 절대 경로에 묶여 있다(PD-5).
if [ -x "$PYTHON" ] && "$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
  pass "python: $PYTHON"
else
  fail "python: $PYTHON (3.12) missing"
fi

# 여유 공간: --root가 아직 없으면 있는 가장 가까운 상위 디렉터리
probe="$ROOT"
while [ ! -d "$probe" ] && [ "$probe" != / ]; do probe="$(dirname "$probe")"; done
avail="$(df -B1 --output=avail "$probe" 2>/dev/null | tail -n 1 | tr -d ' ')"
need=$((MIN_FREE_GIB * 1024 * 1024 * 1024))
if [[ "$avail" =~ ^[0-9]+$ ]] && [ "$avail" -ge "$need" ]; then
  pass "disk: $((avail / 1024 / 1024 / 1024)) GiB free at $probe (need $MIN_FREE_GIB)"
else
  fail "disk: ${avail:-?} bytes free at $probe, need $MIN_FREE_GIB GiB"
fi

# 포트 80/443(web) — 이미 다른 서비스가 듣고 있으면 첫 적용이 실패한다.
busy="$(ss -Htln 2>/dev/null | awk '{print $4}' | grep -E ':(80|443)$' || true)"
if [ -z "$busy" ]; then pass "ports: 80/443 free"; else fail "ports: in use: $(echo "$busy" | tr '\n' ' ')"; fi

# 시계: cert 유효기간·적용 창 판정이 시계에 기대므로 동기화가 필요하다.
ntp="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
if [ "$ntp" = yes ]; then
  pass "time: NTP synchronized"
elif [ "$OFFLINE_TIME" = 1 ]; then
  pass "time: NTP not synchronized — operator confirmed the clock (offline site): $(date -u +%FT%TZ)"
else
  fail "time: NTP not synchronized — fix NTP, or check the clock and pass --offline-time-confirmed"
fi

installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]; }
for pkg in iptables libseccomp2; do
  if installed "$pkg"; then pass "package: $pkg"; else fail "package: $pkg missing (Docker deb dependency)"; fi
done
for pkg in docker.io containerd runc podman-docker; do
  if installed "$pkg"; then fail "package: $pkg conflicts with the bundled Docker (remove it first)"; fi
done

if [ "$FAILED" = 0 ]; then echo "preflight: OK"; else echo "preflight: FAILED"; fi
exit "$FAILED"
