#!/usr/bin/env python3
"""Mirror call/session recordings from the GCS bucket to a local drive.

Copies every object under ``calls/`` and ``sessions/`` in GCS_BUCKET into
DEST (same relative paths), skipping files already present with the same size.
Downloads go to a temp name and are renamed into place, so a partially written
file is never mistaken for a complete one. Nothing is deleted on either side:
the bucket stays the primary copy (the in-call viewer serves from it).

Credentials and bucket come from backend/.env (GCP_CREDENTIALS_B64,
GCS_BUCKET). Run with the backend venv, which has google-cloud-storage:

  backend/.venv/bin/python scripts/sync-recordings.py [--dest DIR] [--dry-run]

Scheduled every 5 minutes by the systemd user timer
infra/systemd/remote-support-recordings-sync.timer.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
from pathlib import Path

from google.cloud import storage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = Path("/mnt/ext_primary/users/jcl5m1/remote-support-recordings")
PREFIXES = ("calls/", "sessions/")

log = logging.getLogger("sync-recordings")


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (PROJECT_ROOT / "backend" / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def sync(client: storage.Client, bucket: str, dest: Path, dry_run: bool = False) -> tuple[int, int]:
    """Return (files copied, bytes copied)."""
    copied = copied_bytes = 0
    for prefix in PREFIXES:
        for blob in client.list_blobs(bucket, prefix=prefix):
            if blob.name.endswith("/"):
                continue
            target = dest / blob.name
            # Defense in depth: object names must stay inside dest.
            if dest.resolve() not in target.resolve().parents:
                log.warning("skipping unsafe object name %r", blob.name)
                continue
            if target.exists() and target.stat().st_size == blob.size:
                continue
            log.info("copy %s (%d bytes)", blob.name, blob.size or 0)
            if dry_run:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.part")
            blob.download_to_filename(str(tmp))
            os.replace(tmp, target)
            copied += 1
            copied_bytes += blob.size or 0
    return copied, copied_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    env = _load_env()
    creds = json.loads(base64.b64decode(env["GCP_CREDENTIALS_B64"]))
    client = storage.Client.from_service_account_info(creds)
    args.dest.mkdir(parents=True, exist_ok=True)

    copied, nbytes = sync(client, env["GCS_BUCKET"], args.dest, dry_run=args.dry_run)
    log.info("done: %d new file(s), %.1f MB -> %s", copied, nbytes / 1e6, args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
