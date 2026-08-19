"""Tests for join-code generation and allocation."""

from __future__ import annotations

import uuid

import pytest

from app.models import Session, SessionState
from app.services.livekit_tokens import room_name_for
from app.services.room_codes import (
    ALPHABET,
    claim_code,
    generate_code,
    is_valid_code,
    normalize_code,
)


def test_generate_code_length():
    assert len(generate_code()) == 6
    assert len(generate_code(8)) == 8


def test_generate_code_uses_alphabet():
    code = generate_code(100)
    assert all(ch in ALPHABET for ch in code)


def test_ambiguous_characters_removed():
    assert "I" not in ALPHABET
    assert "L" not in ALPHABET
    assert "O" not in ALPHABET
    assert "U" not in ALPHABET


def test_normalize_code():
    assert normalize_code("k7r2xm") == "K7R2XM"
    assert normalize_code("K7R2-XM") == "K7R2XM"
    assert normalize_code("iloU") == "110V"


def test_is_valid_code():
    assert is_valid_code("K7R2XM") is True
    assert is_valid_code("K7R2X") is False
    assert is_valid_code("ILOUBA") is False


@pytest.mark.asyncio
async def test_allocate_code_unique(db):
    session1 = Session(room_name=room_name_for(uuid.uuid4()), state=SessionState.PENDING)
    db.add(session1)
    await db.flush()
    code1 = await claim_code(db, session1)
    await db.commit()

    # A second pending session should get a different code with high probability.
    session2 = Session(room_name=room_name_for(uuid.uuid4()), state=SessionState.PENDING)
    db.add(session2)
    await db.flush()
    code2 = await claim_code(db, session2)
    await db.commit()

    assert code1 != code2
    assert is_valid_code(code1)
    assert is_valid_code(code2)
