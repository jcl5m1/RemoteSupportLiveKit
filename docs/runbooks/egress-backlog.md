# Runbook: Egress Backlog

## Symptoms

- `remote_support_egress_events_total{state="failed"}` spikes.
- Recordings remain in `starting` or `active` long after the call ended.
- GCS object count is lower than expected for completed sessions.

## Immediate checks

1. Look at recent egress webhook events:
   ```bash
   docker compose logs --tail 500 backend | grep egress
   ```
2. Check the `recordings` table for rows in non-terminal states:
   ```sql
   SELECT state, COUNT(*) FROM recordings
   WHERE created_at > now() - interval '1 hour'
   GROUP BY state;
   ```
3. Verify GCS credentials are valid:
   ```bash
   echo "$GCP_CREDENTIALS_B64" | base64 -d | jq .client_email
   ```

## Recovery

1. **Re-export the transcript** for affected sessions:
   ```bash
   curl -X POST -H "X-Service-Key: $SERVICE_API_KEY" \
        "$BACKEND_URL/v1/sessions/<session-id>/transcript/export"
   ```
2. **For failed egresses**, the webhook already recorded `state=failed` and the
   error message. If the failure was transient (GCS 5xx, LiveKit outage), no
   manual action is required; the next call will create new egresses.
3. **If egresses are stuck in `starting`/`active`**, the room may have been
   deleted without an `egress_ended` event. Mark them failed manually and
   investigate the missing webhook.

## Post-incident

- Tune the `egress failure rate > 5%` alert threshold if it was noisy.
- Review GCS bucket permissions and lifecycle rules.
