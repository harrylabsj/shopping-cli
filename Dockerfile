ARG PYTHON_IMAGE=python:3.13.11-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY shopping_cli ./shopping_cli
RUN python -m pip install --no-cache-dir "build==1.3.0" \
    && python -m build --wheel --outdir /wheels \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels \
        "psutil>=5.9" "fastapi>=0.110" "pydantic>=2" "uvicorn>=0.27"

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
