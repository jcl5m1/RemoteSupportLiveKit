#!/usr/bin/env bash
# Build and deploy the backend to the production GCP VM (remote-support-vm).
#
# The image is built on the VM itself (it is linux/amd64; dev hosts may be
# arm64 without emulation), from a tarball of backend/ + infra/Dockerfile.backend,
# then only the backend container is recreated. Postgres, Caddy, and the agent
# workers are left running.
#
# Also ensures backend.env on the VM has the open-call settings:
#   OPEN_CALLS_ENABLED=true, OPEN_CALL_RECORDING_ENABLED=true, PUBLIC_BASE_PATH=/livekit
#
# Usage: scripts/deploy-backend-gcp.sh [backend|agent]   (default: backend)
#   GOOGLE_MAPS_API_KEY / GOOGLE_MAPS_MAP_ID in the environment are written to
#   the VM's backend.env too (only when set).
#   agent: same flow for the support agent worker (agent/ + infra/Dockerfile.agent),
#          recreating only the `agent` container; no env changes.
# Requires: gcloud logged in with access to project hermes-458420.
set -euo pipefail

COMPONENT=${1:-backend}
case "$COMPONENT" in
  backend|agent) ;;
  *) echo "usage: $0 [backend|agent]" >&2; exit 2 ;;
esac

PROJECT=hermes-458420
ZONE=us-central1-a
VM=remote-support-vm
IMAGE=us-central1-docker.pkg.dev/hermes-458420/remote-support/$COMPONENT:latest
PUBLIC_URL=https://remotesupport.lgitech.net

GCLOUD=${GCLOUD:-$(command -v gcloud || echo "$HOME/google-cloud-sdk/bin/gcloud")}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TARBALL=$(mktemp --suffix=.tgz)
trap 'rm -f "$TARBALL"' EXIT

gssh() { "$GCLOUD" compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --command "$1"; }

echo "==> Packaging $COMPONENT source"
tar -C "$ROOT" -czf "$TARBALL" \
  --exclude="$COMPONENT/.venv" --exclude="$COMPONENT/.env" --exclude='__pycache__' \
  --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='.pytest_cache' \
  --exclude='*.egg-info' \
  "$COMPONENT" "infra/Dockerfile.$COMPONENT"

echo "==> Copying to $VM"
"$GCLOUD" compute scp "$TARBALL" "$VM:/tmp/rs-src.tgz" --project "$PROJECT" --zone "$ZONE"

echo "==> Building image and restarting $COMPONENT on $VM"
# Optional settings passed through from the deploying shell, e.g.
#   GOOGLE_MAPS_API_KEY=AIza... scripts/deploy-backend-gcp.sh
EXTRA_ENV=""
for name in GOOGLE_MAPS_API_KEY GOOGLE_MAPS_MAP_ID; do
  if [ -n "${!name:-}" ]; then
    case "${!name}" in *[!A-Za-z0-9_-]*) echo "$name has unexpected characters" >&2; exit 1 ;; esac
    EXTRA_ENV="$EXTRA_ENV $name=${!name}"
  fi
done

gssh "$(cat <<EOF
set -euo pipefail
DEPLOY_DIR=\$(dirname "\$(sudo find /home /root -maxdepth 3 -name docker-compose.prod.yml -path '*remote-support*' | head -1)")
[ -n "\$DEPLOY_DIR" ] && [ "\$DEPLOY_DIR" != . ] || { echo "deploy dir not found" >&2; exit 1; }
echo "deploy dir: \$DEPLOY_DIR"
rm -rf /tmp/rs-src && mkdir -p /tmp/rs-src && tar -C /tmp/rs-src -xzf /tmp/rs-src.tgz
# Keep the running image and env for rollback:
#   sudo docker tag ${IMAGE%:*}:rollback $IMAGE && cp $COMPONENT.env.rollback $COMPONENT.env
#   sudo docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate $COMPONENT
sudo docker tag $IMAGE ${IMAGE%:*}:rollback
sudo cp "\$DEPLOY_DIR/$COMPONENT.env" "\$DEPLOY_DIR/$COMPONENT.env.rollback"
sudo docker build -q -t $IMAGE -f /tmp/rs-src/infra/Dockerfile.$COMPONENT /tmp/rs-src/$COMPONENT
[ "$COMPONENT" = backend ] && for kv in OPEN_CALLS_ENABLED=true OPEN_CALL_RECORDING_ENABLED=true PUBLIC_BASE_PATH=/livekit $EXTRA_ENV; do
  key=\${kv%%=*}
  if sudo grep -q "^\$key=" "\$DEPLOY_DIR/backend.env"; then
    sudo sed -i "s|^\$key=.*|\$kv|" "\$DEPLOY_DIR/backend.env"
  else
    echo "\$kv" | sudo tee -a "\$DEPLOY_DIR/backend.env" >/dev/null
  fi
done
cd "\$DEPLOY_DIR"
sudo docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate $COMPONENT
if [ "$COMPONENT" = backend ]; then
  for i in \$(seq 1 30); do
    sudo docker compose -f docker-compose.prod.yml ps backend | grep -q healthy && break
    sleep 2
  done
else
  # The agent has no healthcheck; wait for it to register with LiveKit.
  for i in \$(seq 1 30); do
    sudo docker compose -f docker-compose.prod.yml logs --since 2m agent 2>&1 | grep -q "registered worker" && break
    sleep 2
  done
  sudo docker compose -f docker-compose.prod.yml logs --since 2m agent 2>&1 | grep "registered worker" | tail -1 | cut -c1-200
fi
sudo docker compose -f docker-compose.prod.yml ps $COMPONENT
rm -rf /tmp/rs-src /tmp/rs-src.tgz
EOF
)"

[ "$COMPONENT" = agent ] && exit 0

echo "==> Smoke test"
curl -fsS "$PUBLIC_URL/healthz" && echo
curl -fsS -o /dev/null -w "GET /livekit/ -> %{http_code}\n" "$PUBLIC_URL/livekit/"
curl -fsS "$PUBLIC_URL/livekit/v1/calls/deploy-check" && echo
