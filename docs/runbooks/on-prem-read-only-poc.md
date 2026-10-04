# On-prem read-only PoC runbook

## 1. Purpose

This runbook is the minimal single-host deployment path for a customer-site PureIron read-only PoC. It starts only:

- `timescaledb`
- `redis`
- `backend`
- `frontend`

The PoC-safe default is `PUREIRON_POC_READ_ONLY_MODE=true`. In that mode PureIron must monitor, alarm, search history, and answer questions without allowing operator/agent write-control actions.

## 2. Non-goals

Do not use this runbook to introduce these in P0:

- Kubernetes, Helm, Terraform, or cloud orchestration
- offline installer/bundle automation
- TLS termination or reverse tunnel automation
- customer-specific SI scripts
- simulator-first demo stack
- write/control enablement

If a customer needs any of those, treat it as a separate scoped task after the paid PoC is accepted.

## 3. Prerequisites

- One Linux host or Windows host with WSL2/Docker Desktop
- Docker Engine and Docker Compose
- outbound access for first image build/pull unless images are preloaded by the operator
- access from the operator LAN to the chosen PureIron UI port, default `3000`
- customer/SI network approval for any PLC/SCADA read-only collection routes

Recommended host baseline for a small PoC: 4 vCPU, 8 GB RAM, and 50+ GB disk for database/history growth.

## 4. Network and safety guidance

- Keep `PUREIRON_POC_READ_ONLY_MODE=true` for every customer-site PoC.
- The PoC compose file does not publish Postgres or Redis host ports. They are reachable only inside the Compose network.
- Publish only the frontend port to the operator LAN unless there is a documented reason to expose more.
- Use a customer/SI-approved collection path. Prefer a segmented monitoring host or firewall rules that allow only read-only protocol access from PureIron to the approved devices.
- Do not put customer secrets, LLM keys, or PLC credentials in tickets, screenshots, or committed files.

## 5. Configure

From the repository root:

```bash
cp .env.poc.example .env.poc
```

Edit `.env.poc`:

1. Replace `PUREIRON_POSTGRES_PASSWORD` with a strong local database password.
2. Keep `PUREIRON_DATABASE_URL` aligned with that password and the internal host `timescaledb`.
3. Replace `PUREIRON_JWT_SECRET_KEY` with at least 32 random characters.
4. Keep `PUREIRON_POC_READ_ONLY_MODE=true`.
5. Set `PUREIRON_CORS_ORIGINS` to the UI origin customers will use, for example `["http://192.168.10.20:3000"]`.
6. Leave LLM API keys empty unless the PoC explicitly includes Agent answers.

Do not commit `.env.poc`.

## 6. Deploy

Validate the Compose model before starting:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml config
```

Start the stack:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml up -d --build
```

Check containers:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml ps
```

Open the UI:

```text
http://<poc-host>:3000
```

Use the configured `PUREIRON_POC_HTTP_PORT` if it is not `3000`.

## 7. Verify

Run these checks after startup:

```bash
# API health through the frontend reverse proxy
curl -f http://localhost:${PUREIRON_POC_HTTP_PORT:-3000}/api/health

# Backend health from inside the Compose network
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml exec backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/health').read().decode())"
```

Confirm `/api/health` reports `poc_read_only_mode: true`.

Also verify:

- frontend login page loads from the operator LAN
- no `opcua-sim` or `modbus-sim` service is running in this PoC compose stack
- Postgres and Redis are not published to host ports
- any device route approved by the SI is read-only from PureIron's side

Before a customer review or final demo, run the [PoC browser smoke runbook](poc-browser-smoke.md) and keep the sanitized screenshots/CSV/notes in the customer's approved evidence location.

## 8. Operations, backup, and restart

View logs:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml logs -f backend frontend
```

Restart application services without deleting data:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml restart backend frontend
```

Stop the stack while keeping volumes:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml down
```

Back up database volume data with the customer's approved backup location:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml exec timescaledb \
  pg_dump -U pureiron -d pureiron > pureiron-poc-$(date +%Y%m%d).sql
```

### History retention and trend review window

PureIron applies a default **30-day retention policy** to the `tag_history` hypertable (migration `027_add_tag_history_retention.sql`). This keeps the full 2-week read-only PoC review window plus follow-up margin while bounding database/history disk growth. Trend/history queries auto-downsample by requested window (raw under 1 hour, 1-minute up to 24 hours, 5-minute up to 30 days), so a 2-week review stays responsive. The retention/query-window contract is in `docs/wiki/sot/data-contracts.md`.

For a paid PoC, agree on retention and backup ownership with the customer/SI before collecting production-adjacent history.

Inspect the active retention policy:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml exec timescaledb \
  psql -U pureiron -d pureiron -c \
  "SELECT hypertable_name, config FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention';"
```

Adjust the retention window for this PoC (example: 45 days), after customer/SI approval:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml exec timescaledb \
  psql -U pureiron -d pureiron -c \
  "SELECT remove_retention_policy('tag_history', if_exists => TRUE); \
   SELECT add_retention_policy('tag_history', INTERVAL '45 days');"
```

Retention is a database maintenance policy and is unaffected by `PUREIRON_POC_READ_ONLY_MODE`; history collection and review keep working in read-only mode.

## 9. Rollback

To stop PureIron without deleting data:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml down
```

To remove all PoC data after customer approval:

```bash
docker compose --env-file .env.poc -f docker/docker-compose.poc.yml down -v
```

Do not run `down -v` until the customer/SI confirms that exported data and screenshots are no longer needed.

## 10. Customer/SI handoff

Hand off only these artifacts:

- sanitized `.env.poc` values needed by the local operator, with secrets delivered through the customer's approved secret channel
- host/IP/port where the UI is reachable
- list of approved read-only device routes and protocols
- backup/retention decision
- rollback command and owner
- confirmation that `PUREIRON_POC_READ_ONLY_MODE=true` is active

Record any request for write/control, tunnel automation, TLS automation, or offline bundle work as a separate post-P0 scope item.
