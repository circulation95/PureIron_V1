# pureiron/collector — 장비 통신 서비스(무상태, DB 무접근). 빌드 컨텍스트: repo 루트.
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
# uv.lock 그대로 설치 후, 설치 시각 메타데이터(uv_cache.json)를 지운다(재현 빌드).
RUN --mount=type=bind,source=.,target=/src,rw \
    cd /src && uv sync --locked --no-dev --no-editable --package pureiron-collector \
      --python /usr/local/bin/python3.12 \
    && python3 /src/deploy/build/normalize_venv.py /opt/pureiron/venv

FROM ${PYTHON_IMAGE}
COPY --from=build /opt/pureiron/venv /opt/pureiron/venv
ENV PATH=/opt/pureiron/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["pureiron-collector"]
