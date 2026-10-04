# pureiron/simulator — dev profile 전용 Modbus TCP 시뮬레이터. 빌드 컨텍스트: repo 루트.
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY deploy/simulator/requirements.lock /opt/simulator/requirements.lock
RUN pip install --no-compile --require-hashes -r /opt/simulator/requirements.lock
COPY deploy/simulator/simulator.py /opt/simulator/simulator.py
USER 10001:10001
EXPOSE 5020
ENTRYPOINT ["python", "/opt/simulator/simulator.py"]
