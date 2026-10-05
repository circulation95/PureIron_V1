#!/usr/bin/env bash
# Updater venv tarball(PD-5): `updater-<ver>.tar.gz`(최상위 `<ver>/`).
#
#   bash deploy/build/build_updater.sh OUT_DIR
#
# 현장과 같은 절대 경로에서 만든다: ubuntu:24.04 컨테이너의 `/usr/bin/python3.12 -m venv
# /opt/pureiron/updater/<ver>` → uv.lock에서 export한 의존성(--require-hashes, --no-deps) + 이 저장소의
# bundle-tools·updater wheel 설치 → normalize_venv.py → 결정적 tar(이름 순, mtime =
# SOURCE_DATE_EPOCH, root 소유) + gzip -n. 호스트는 같은 경로에 풀고 `current` symlink를 건다
# (install.sh). venv의 python은 호스트 `/usr/bin/python3.12` symlink라 preflight가 그것을 확인한다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:?usage: build_updater.sh OUT_DIR}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
UBUNTU_IMAGE="${UBUNTU_IMAGE:-ubuntu:24.04}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct)}"
VERSION="$(cd "$ROOT" && uv run --no-sync python -c \
  'import tomllib; print(tomllib.load(open("services/updater/pyproject.toml", "rb"))["project"]["version"])')"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/wheels"
cd "$ROOT"
uv export --package pureiron-updater --frozen --no-dev --no-emit-workspace --no-editable \
  --format requirements-txt -o "$WORK/requirements.txt" >/dev/null
uv build --package pureiron-bundle-tools --wheel -o "$WORK/wheels" >/dev/null
uv build --package pureiron-updater --wheel -o "$WORK/wheels" >/dev/null
cp deploy/build/normalize_venv.py "$WORK/"

docker run --rm --platform linux/amd64 \
  -e VERSION="$VERSION" -e SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  -v "$WORK:/work" "$UBUNTU_IMAGE" bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1
apt-get update -qq
apt-get install -y -qq python3.12-venv >/dev/null
VENV=/opt/pureiron/updater/$VERSION
/usr/bin/python3.12 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --no-cache-dir --no-compile --no-deps --require-hashes \
  -r /work/requirements.txt
"$VENV/bin/pip" install --quiet --no-cache-dir --no-compile --no-deps --no-index /work/wheels/*.whl
"$VENV/bin/pureiron-site" --version
/usr/bin/python3.12 /work/normalize_venv.py "$VENV"
find "$VENV" -name __pycache__ -type d -prune -exec rm -rf {} +
tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
  -C /opt/pureiron/updater -cf - "$VERSION" | gzip -n -9 > "/work/updater-$VERSION.tar.gz"
chown "$HOST_UID:$HOST_GID" "/work/updater-$VERSION.tar.gz"
'
mv "$WORK/updater-$VERSION.tar.gz" "$OUT/"
echo "$OUT/updater-$VERSION.tar.gz"
