#!/usr/bin/env bash
# 설치 번들용 Docker deb 수집(빌드 시, 네트워크 필요). `.deb`는 저장소에 커밋하지 않는다.
#
#   bash deploy/install/debs/fetch_debs.sh OUT_DIR
#   DOCKER_CE_VERSION=5:28.3.3-1~ubuntu.24.04~noble bash deploy/install/debs/fetch_debs.sh OUT_DIR
#
# ubuntu:24.04 컨테이너에서 Docker 공식 apt 저장소(서명 키 지문 고정)를 등록하고 `apt-get download`로
# containerd.io·docker-ce·docker-ce-cli·docker-compose-plugin을 받는다 — apt가 저장소 서명(InRelease)과
# 패키지 해시를 확인한다. 결과: OUT_DIR/<패키지>.deb + debs.json({패키지: {version, arch, sha256}}).
# build_install_bundle.py가 debs.json과 파일 sha256·compose 최소 버전을 다시 확인한다.
#
# 버전 고정(선택): CONTAINERD_VERSION, DOCKER_CE_VERSION(docker-ce·docker-ce-cli 공통),
# COMPOSE_VERSION. 비우면 저장소 최신. UBUNTU_IMAGE로 기반 이미지를 바꿀 수 있다.
set -euo pipefail

OUT="${1:?usage: fetch_debs.sh OUT_DIR}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
UBUNTU_IMAGE="${UBUNTU_IMAGE:-ubuntu:24.04}"
# https://docs.docker.com/engine/install/ubuntu/ 의 저장소 키 지문
DOCKER_KEY_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88

docker run --rm --platform linux/amd64 \
  -e DOCKER_KEY_FPR="$DOCKER_KEY_FPR" \
  -e CONTAINERD_VERSION="${CONTAINERD_VERSION:-}" \
  -e DOCKER_CE_VERSION="${DOCKER_CE_VERSION:-}" \
  -e COMPOSE_VERSION="${COMPOSE_VERSION:-}" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  -v "$OUT:/out" "$UBUNTU_IMAGE" bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg >/dev/null
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.asc
got="$(gpg --show-keys --with-colons /tmp/docker.asc | awk -F: "/^fpr:/ {print \$10; exit}")"
[ "$got" = "$DOCKER_KEY_FPR" ] || { echo "docker apt key fingerprint $got != $DOCKER_KEY_FPR" >&2; exit 1; }
gpg --dearmor -o /etc/apt/keyrings/docker.gpg /tmp/docker.asc
. /etc/os-release
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
spec() { if [ -n "$2" ]; then echo "$1=$2"; else echo "$1"; fi; }
cd /tmp && mkdir debs && cd debs
apt-get download \
  "$(spec containerd.io "$CONTAINERD_VERSION")" \
  "$(spec docker-ce "$DOCKER_CE_VERSION")" \
  "$(spec docker-ce-cli "$DOCKER_CE_VERSION")" \
  "$(spec docker-compose-plugin "$COMPOSE_VERSION")"
echo "{" > /out/debs.json
first=1
for deb in *.deb; do
  pkg="$(dpkg-deb -f "$deb" Package)"
  ver="$(dpkg-deb -f "$deb" Version)"
  arch="$(dpkg-deb -f "$deb" Architecture)"
  cp "$deb" "/out/$pkg.deb"
  sha="$(sha256sum "$deb" | cut -d" " -f1)"
  [ "$first" = 1 ] || echo "," >> /out/debs.json
  first=0
  printf "  \"%s\": {\"version\": \"%s\", \"arch\": \"%s\", \"sha256\": \"sha256:%s\"}" "$pkg" "$ver" "$arch" "$sha" >> /out/debs.json
done
printf "\n}\n" >> /out/debs.json
chown "$HOST_UID:$HOST_GID" /out/*.deb /out/debs.json
'
cat "$OUT/debs.json"
