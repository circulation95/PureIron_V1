# pureiron/web — 운영 화면(nginx 정적 + /api 프록시). 빌드 컨텍스트: repo 루트.
ARG NODE_IMAGE=node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402
ARG NGINX_IMAGE=nginx:1.30-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94

FROM ${NODE_IMAGE} AS build
WORKDIR /web
COPY services/web/ ./
# npm은 받기에 실패한 optional 의존성(rolldown 네이티브 binding 등)을 조용히 건너뛴다. 설치와 빌드를
# 한 층에 두어, 그렇게 빠진 설치가 성공한 층으로 캐시되지 않고 build_images.sh 재시도가 다시 받게 한다.
RUN npm ci --no-audit --no-fund && npm run build

FROM ${NGINX_IMAGE}
COPY services/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 8080
