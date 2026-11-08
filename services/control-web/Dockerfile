# pureiron/control-web — 중앙 현장 현황 화면(M4-3 G5): node 빌드 → Caddy(정적 + basic auth + /ui/fleet·/api/v1 프록시).
# 빌드 컨텍스트: repo 루트. Caddyfile은 이미지에 넣지 않고 deploy/central에서 마운트한다(/etc/caddy/Caddyfile) —
# 이미지 기본 Caddyfile(업스트림 견본)로 뜨지 않게 compose가 항상 마운트한다.
ARG NODE_IMAGE=node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402
ARG CADDY_IMAGE=caddy:2.11.7-alpine@sha256:e70b164da81ef698b3ad19af799ea6086ce6d9e48ed4079eb9fe26f6fbf14ee5

FROM ${NODE_IMAGE} AS build
WORKDIR /web
COPY services/control-web/ ./
# 설치와 빌드를 한 층에 둔다(services/web/Dockerfile과 같은 이유: 빠진 optional 의존성이 캐시되지 않게).
RUN npm ci --no-audit --no-fund && npm run build

FROM ${CADDY_IMAGE}
COPY --from=build /web/dist /srv/control-web
EXPOSE 8443
