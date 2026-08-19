# Runbook: Agent Worker Down

## Symptoms

- `readyz` reports `livekit` healthy but calls proceed without the AI agent.
- `remote_support_ai_toggles_total` flatlines; no new `agent_events` rows.
- LiveKit Cloud dashboard shows zero agents connected for the project.

## Immediate checks

1. Is the agent process running?
   ```bash
   docker compose ps agent
   # or
   ps aux | grep "python -m agent.main"
   ```
2. Are there crash loops in logs?
   ```bash
   docker compose logs --tail 200 agent
   ```
3. Can the agent reach the backend?
   ```bash
   curl -H "X-Service-Key: $SERVICE_API_KEY" \
        "$BACKEND_URL/v1/sessions/<session-id>"
   ```

## Recovery

1. **Restart the worker.** The worker is stateless; the backend is the source of
   truth. Existing calls between humans are unaffected.
   ```bash
   docker compose restart agent
   ```
2. **Verify it reconnects.** Within 30 s a new agent job should dispatch for any
   active room that does not already have one.
3. **Check mode reconciliation.** The heartbeat will sync `ai_enabled` and
   `agent_mode` from Postgres within 30 s.

## Post-incident

- Capture the traceback and open an issue.
- If the root cause is an invalid model identifier (`moonshotai/kimi-k2.6`,
  `deepgram/nova-3:en`, `cartesia/sonic-3:en`), verify the LiveKit Cloud
  Inference catalog has not retired the ID.
