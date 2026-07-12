FROM python:3.13-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -e '.[api]' \
  && chmod +x /app/scripts/shopping.py /app/scripts/shopping_agent.py /app/scripts/shopping_api.py /app/scripts/verify.sh \
  && mkdir -p /data

VOLUME ["/data"]

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import json, urllib.request; data=json.loads(urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2).read()); raise SystemExit(0 if data.get('ok') else 1)"

CMD ["python3", "scripts/shopping_api.py"]
