#!/usr/bin/env bash
# 현장 이미지 빌드: pureiron/{core,collector,web,simulator,core-test}:<release> + pureiron/site-db retag.
#
#   bash deploy/build/build_images.sh [svc ...]   # 기본 release = PUREIRON_RELEASE 또는 0.1.0-dev
#   NO_CACHE=1 bash deploy/build/build_images.sh  # 재현 빌드 검사용
#
# 재현성(G1-7): SOURCE_DATE_EPOCH = 마지막 커밋 시각, BuildKit rewrite-timestamp로 레이어 파일 시각 고정.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
RELEASE="${PUREIRON_RELEASE:-0.1.0-dev}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}"
export SOURCE_DATE_EPOCH
CACHE_FLAG=()
if [ "${NO_CACHE:-0}" = "1" ]; then
  CACHE_FLAG=(--no-cache)
fi

# 서비스 → Dockerfile (빌드 컨텍스트는 항상 repo 루트)
declare -A DOCKERFILES=(
  [core]=services/core/Dockerfile
  [core-test]=services/core/Dockerfile.test
  [collector]=services/collector/Dockerfile
  [web]=services/web/Dockerfile
  [simulator]=deploy/simulator/Dockerfile
)
if [ "$#" -gt 0 ]; then
  SERVICES=("$@")
else
  SERVICES=(core core-test collector web simulator)
fi

OUT_DIR="$ROOT/artifacts/images"
mkdir -p "$OUT_DIR"

# 전용 docker-container builder(BuildKit 버전 고정). 데몬의 image store 종류(classic/containerd)와
# 무관하게 같은 exporter로 tar를 만들고 docker load 한다 — 현장 번들도 같은 save/load 경로(ADR-023).
BUILDER=pureiron
BUILDKIT_IMAGE=moby/buildkit:v0.33.1@sha256:cec9f139f45e93c5c69c60f8b07cfad9f43f4ef6b6a6cd917527fea5ff2e3dea
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  docker buildx create --name "$BUILDER" --driver docker-container \
    --driver-opt "image=$BUILDKIT_IMAGE" >/dev/null
fi

for svc in "${SERVICES[@]}"; do
  dockerfile="${DOCKERFILES[$svc]:-}"
  if [ -z "$dockerfile" ]; then
    echo "build_images: unknown service $svc" >&2
    exit 1
  fi
  echo "== build pureiron/$svc:$RELEASE"
  docker buildx build --builder "$BUILDER" "${CACHE_FLAG[@]}" \
    --platform linux/amd64 \
    --provenance=false --sbom=false \
    --build-arg SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
    --output "type=docker,name=pureiron/$svc:$RELEASE,rewrite-timestamp=true,dest=$OUT_DIR/$svc.tar" \
    -f "$dockerfile" .
  docker load -i "$OUT_DIR/$svc.tar"
done

# site-db: 업스트림을 digest로 받아 pureiron/site-db:<version>으로 retag(PD-19).
lock_value() { sed -n "s/^$1=//p" deploy/releases/site-db.lock; }
version="$(lock_value version)"
upstream="$(lock_value upstream)"
digest="$(lock_value digest)"
repo="${upstream%%:*}"
if ! docker image inspect "pureiron/site-db:$version" >/dev/null 2>&1; then
  echo "== retag $upstream@$digest -> pureiron/site-db:$version"
  docker pull --platform linux/amd64 "$repo@$digest"
  docker tag "$repo@$digest" "pureiron/site-db:$version"
fi
