#!/usr/bin/env python3
"""Export the backend OpenAPI schema to a JSON file.

Usage:
    python scripts/export-openapi.py [output_path]

Defaults to docs/openapi.json.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
os.environ.setdefault("CALLER_JWT_SECRET", "export-only")
os.environ.setdefault("SERVICE_API_KEY", "export-only")
os.environ.setdefault("FIREBASE_PROJECT_ID", "export-only")
os.environ.setdefault("SUPPORT_ALLOWED_DOMAINS", "example.com")
os.environ.setdefault("ALLOW_DEGRADED_START", "true")
os.environ.setdefault("LIVEKIT_URL", "wss://example.livekit.cloud")
os.environ.setdefault("LIVEKIT_API_KEY", "export-only")
os.environ.setdefault("LIVEKIT_API_SECRET", "export-only")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://rs:rs@localhost:5432/remote_support")
os.environ.setdefault("GCS_BUCKET", "export-only")
os.environ.setdefault("GCP_CREDENTIALS_B64", "")

sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "docs" / "openapi.json"
    schema = app.openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"OpenAPI schema written to {output}")


if __name__ == "__main__":
    main()
