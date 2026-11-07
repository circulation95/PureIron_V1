# pureiron/control-api — 중앙 control-api(M4-3 G5). 빌드 컨텍스트: repo 루트.
# services/core/Dockerfile과 같은 재현 빌드(G1-7): 베이스 이미지 digest 고정, uv.lock --locked, 바이트코드 미생성,
# apt 미사용, 사용자 계정 파일 미수정(숫자 UID). 타임스탬프는 build_images.sh가 SOURCE_DATE_EPOCH로 고정한다.
# 마이그레이션은 이미지에 넣되 기동 때 실행하지 않는다(불변 규칙) — 배포 절차가
# `docker compose run --rm control-api migrate up`을 먼저 실행한다(deploy/central/README.md).
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/pureiron/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
# uv.lock 그대로 설치 후, uv가 남기는 설치 시각 메타데이터(uv_cache.json)를 지운다(재현 빌드).
# 데이터 디렉터리(번들·드래프트)는 빈 디렉터리로 만들어 두고 최종 층에 숫자 UID 소유로 복사한다 — named volume이
# 처음 붙을 때 이 소유권을 이어받는다(최종 층에 RUN·chown 없음).
RUN --mount=type=bind,source=.,target=/src,rw \
    cd /src && uv sync --locked --no-dev --no-editable --package pureiron-control-api \
      --python /usr/local/bin/python3.12 \
    && python3 /src/deploy/build/normalize_venv.py /opt/pureiron/venv \
    && mkdir -p /opt/pureiron/data

FROM ${PYTHON_IMAGE}
COPY --from=build /opt/pureiron/venv /opt/pureiron/venv
COPY --from=build --chown=10001:10001 /opt/pureiron/data /var/lib/pureiron-control
COPY services/control-api/migrations /opt/pureiron/migrations
ENV PATH=/opt/pureiron/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUREIRON_CONTROL_HTTP_HOST=0.0.0.0 \
    PUREIRON_CONTROL_DATA_DIR=/var/lib/pureiron-control \
    PUREIRON_CONTROL_MIGRATIONS_DIR=/opt/pureiron/migrations
USER 10001:10001
EXPOSE 8100
ENTRYPOINT ["pureiron-control"]
CMD ["serve"]
