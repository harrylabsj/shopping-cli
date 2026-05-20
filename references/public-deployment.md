# Local API Deployment Notes

Hosted deployment is deferred for the MVP. For local demos, run the API with SQLite:

```bash
pip install -e '.[api]'
export SHOPPING_ADMIN_TOKEN='replace-with-a-long-random-secret'
export SHOPPING_CHANNEL_TOKENS='telegram:replace-with-channel-secret'
python3 scripts/shopping_api.py --db /data/shopping-cli.sqlite --host 0.0.0.0 --port 8765
```

Docker Compose runs the same API service and stores SQLite data in a volume:

```bash
docker compose --env-file marketplace.example.env up --build
```

`SHOPPING_ADMIN_TOKEN` is required for API merchant onboarding. Channel ingress through `/channels/messages` is disabled unless `SHOPPING_CHANNEL_TOKENS` or `SHOPPING_CHANNEL_TOKEN` is configured.

Before any public launch, add TLS, identity, authorization policy, audit logs, backups, monitoring, abuse handling, and formal merchant confirmation workflows.
