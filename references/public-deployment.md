# Marketplace Deployment Guide

`shopping-cli` can run the Marketplace API as a standalone consultation service for a small public pilot. This release is still SQLite-backed and consultation-only: it does not process payments, create orders, reserve stock, handle refunds, dispatch couriers, or claim delivery success.

## Local container deployment

Create a private deployment env file from `marketplace.example.env` and replace every `replace-with-*` value with a long random secret before starting the service. Do not pass the example file itself to Compose: it only contains placeholders, and a real env file holds live secrets.

```bash
cp marketplace.example.env .env
# edit .env and replace every replace-with-* value with a long random secret
docker compose --env-file .env up --build
```

`.env` (and any `.env.*` variant) is excluded by `.gitignore` and `.dockerignore`; only the `marketplace.example.env` template is meant to be committed.

The compose file binds the API to `127.0.0.1` by default. To expose it through a reverse proxy on the host, set `SHOPPING_API_BIND=0.0.0.0` intentionally and terminate TLS at Nginx, Caddy, SLB, or another edge proxy.

## Required production configuration

Set these values for a public pilot:

```bash
SHOPPING_DEPLOYMENT_PROFILE=production
SHOPPING_API_HOST=0.0.0.0
SHOPPING_API_PORT=8765
SHOPPING_DB=/data/shopping-cli.sqlite
SHOPPING_PUBLIC_BASE_URL=https://marketplace.example.com
SHOPPING_ADMIN_TOKEN=<at-least-32-utf8-bytes>
SHOPPING_BUYER_BOOTSTRAP_TOKEN=<at-least-32-utf8-bytes>
SHOPPING_CHANNEL_TOKENS=telegram:<long-random-secret>,whatsapp:<long-random-secret>
SHOPPING_MAX_REQUEST_BODY_BYTES=1048576
SHOPPING_BUYER_TOKEN_TTL_SECONDS=86400
SHOPPING_MERCHANT_TOKEN_TTL_SECONDS=2592000
```

`SHOPPING_ADMIN_TOKEN` protects merchant onboarding. `SHOPPING_BUYER_BOOTSTRAP_TOKEN` protects buyer conversation creation. In production both are required, must not use a known placeholder, and must contain at least 32 UTF-8 bytes. Channel tokens are optional; channel ingress through `/channels/messages` fails closed unless `SHOPPING_CHANNEL_TOKENS` or `SHOPPING_CHANNEL_TOKEN` is configured.

The API rejects request bodies larger than 1 MiB by default and applies JSON depth/collection/text limits. Buyer credentials expire after one day and are revoked when their conversation closes; merchant credentials expire after 30 days by default. Override these defaults only through the documented environment variables. Remote Agent/LLM API clients require HTTPS; `SHOPPING_ALLOW_INSECURE_HTTP=true` is reserved for a trusted internal network such as the Compose service network.

## Health check

Use `/health` for container and load-balancer health checks:

```bash
curl -fsS http://127.0.0.1:8765/health
```

The response reports non-secret deployment status, including database connectivity and whether required tokens are configured and strong enough. In `production`, placeholder, missing, or short required tokens make `ok` false. The CLI and the dedicated API launcher run the same production preflight.

## Alibaba Cloud pilot shape

A small pilot can run on Alibaba Cloud with:

- ECS or a single container host for the API.
- A persistent disk mounted at `/data` for SQLite.
- SLB or Nginx/Caddy for HTTPS termination.
- Security groups that expose only HTTPS publicly.
- SLS or host log collection for API/container logs.
- Scheduled backups of `/data/shopping-cli.sqlite`.
- Secret storage outside the image and repo.

Do not run multiple API replicas against the same SQLite file. If you need horizontal scaling, failover, multi-region availability, or high write concurrency, migrate the database layer first.

## RDS/Postgres boundary

This release does not support Postgres or RDS. `SHOPPING_DATABASE_URL=postgres://...` fails fast so operators do not accidentally deploy with an unsupported database. RDS support requires a separate migration plan for SQL dialects, schema migrations, connection pooling, transaction semantics, and integration tests.

## Public launch checklist

Before opening the marketplace beyond a controlled pilot:

- HTTPS is mandatory.
- Rotate every placeholder token and store secrets outside source control.
- Back up SQLite regularly and test restore.
- Capture API logs and audit events for merchant/buyer support.
- Put rate limiting or abuse controls at the edge.
- Document merchant human-review operations.
- Keep the product boundary clear: consultation only, no payments, no order creation, no stock reservation, no refunds, no delivery claims.
