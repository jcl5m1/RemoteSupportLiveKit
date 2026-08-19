"""Join-code generation and normalization.

Crockford base32 minus the visually ambiguous I, L, O, U -> 28 symbols.
6 characters => 28**6 ~= 481M combinations. See docs/08 for why that is
sufficient given the surrounding rate limits and single-support constraint.
"""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models import Session

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
ALPHABET = "".join(c for c in ALPHABET if c not in "ILOU")

# Users transcribe these wrong constantly. Remap on input rather than rejecting.
_NORMALIZE = {"I": "1", "L": "1", "O": "0", "U": "V"}


def generate_code(length: int = 6) -> str:
    """Generate one candidate code. Collision handling is the caller's job."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def normalize_code(raw: str) -> str:
    """Uppercase, strip separators, and remap ambiguous characters."""
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum())
    return "".join(_NORMALIZE.get(ch, ch) for ch in cleaned)


def is_valid_code(code: str, length: int = 6) -> bool:
    return len(code) == length and all(ch in ALPHABET for ch in code)


async def allocate_code(db, length: int = 6, max_attempts: int = 8) -> str:
    """Generate a join code and atomically claim it by INSERT/UPDATE.

    We retry on the partial unique index violation from
    ux_sessions_active_join_code. This is safe because only active/pending
    sessions participate in the unique index; completed/expired codes can be
    reused.
    """
    for _ in range(max_attempts):
        code = generate_code(length)
        result = await db.execute(
            select(Session.id).where(
                Session.join_code == code,
                Session.state.in_(["pending", "active"]),
            )
        )
        if result.scalar_one_or_none() is not None:
            # Code is currently in use; try again without burning an insert.
            continue
        return code
    raise RuntimeError(f"Could not allocate a unique join code after {max_attempts} attempts")


async def claim_code(db, session: Session, length: int = 6, max_attempts: int = 8) -> str:
    """Assign a unique join code to an existing pending session.

    Updates the session row directly; the caller is responsible for committing.
    Retries on unique-index collisions.
    """
    for _ in range(max_attempts):
        code = generate_code(length)
        session.join_code = code
        try:
            await db.flush()
            return code
        except IntegrityError:
            await db.rollback()
    raise RuntimeError(f"Could not claim a unique join code after {max_attempts} attempts")
