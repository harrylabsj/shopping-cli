ARG PYTHON_IMAGE=python:3.13.11-slim-bookworm@sha256:20080e807bfc404f8450b185cf0fc95d553462673598549613735f70a5b4d5d0

FROM ${PYTHON_IMAGE} AS builder
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY shopping_cli ./shopping_cli
RUN python -m pip install --no-cache-dir "build==1.3.0" "uv==0.10.6" \
    && uv export --locked --no-dev --extra api --no-emit-project --format requirements.txt > /tmp/requirements.txt \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels --require-hashes -r /tmp/requirements.txt \
    && python -m build --wheel --outdir /wheels \
    && rm -f /tmp/requirements.txt

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHOPPING_DB=/data/shopping-cli.sqlite \
    SHOPPING_API_HOST=0.0.0.0 \
    SHOPPING_API_PORT=8765

RUN groupadd --gid 10001 shopping \
    && useradd --uid 10001 --gid shopping --create-home --shell /usr/sbin/nologin shopping \
    && install -d -o shopping -g shopping /data
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels "shopping-cli[api]==3.0.1" \
    && find /wheels -type f -delete

USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import json, urllib.request; data=json.loads(urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2).read()); raise SystemExit(0 if data.get('ok') else 1)"

CMD ["shopping-cli-api", "--db", "/data/shopping-cli.sqlite", "--host", "0.0.0.0", "--port", "8765"]
