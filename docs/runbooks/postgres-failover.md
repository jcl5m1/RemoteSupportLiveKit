# Runbook: Postgres Failover

## Symptoms

- `readyz` reports `postgres` error.
- All API endpoints return 500 or 503.
- Agent workers cannot load session state.

## Immediate checks

1. Is the database reachable from the backend host?
   ```bash
   psql "$DATABASE_URL" -c "SELECT 1"
   ```
2. Are connections exhausted?
   ```sql
   SELECT count(*) FROM pg_stat_activity;
   ```
3. Check disk space and replication lag if running a standby.

## Recovery

1. **If the primary is down**, promote the standby or restart the managed
   Postgres instance.
2. **Update `DATABASE_URL`** in the backend and agent environments to point to
   the new primary.
3. **Restart backend and agent services** so connection pools are recreated:
   ```bash
   docker compose restart backend agent
   ```
4. **Verify `/readyz`** returns healthy.

## Post-incident

- Confirm no sessions were corrupted; `sessions` and `recordings` rows should be
  consistent.
- Review connection pool sizing and idle timeouts.
