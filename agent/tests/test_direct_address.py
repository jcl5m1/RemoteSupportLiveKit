"""Truth table for the ASSISTED-mode gate.

This is one of the two tests that protect the core requirement (the other is
the manual "support talks for 60 seconds, agent stays silent" check in
docs/09 phase 2).
"""

import pytest

from agent.prompts import is_direct_address


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hey assistant, can you look that up?", True),
        ("Hey Agent what was the order number", True),
        ("so then I restarted the router", False),
        ("I think the assistant said something earlier", False),  # mention != address
        ("", False),
        ("yeah that's right", False),
        # Second-person questions naming the assistant.
        ("can you look that up, assistant?", True),
        ("what do you think agent?", True),
        ("can you help me?", False),  # no assistant name
        ("I think the assistant said something earlier?", False),  # not addressing
    ],
)
def test_direct_address(text, expected):
    assert is_direct_address(text) is expected


def test_starts_strict():
    """Regression guard: the gate should stay conservative. A false positive
    means the agent interrupts a human support call, which is worse than a
    false negative (the caller just repeats themselves)."""
    assert is_direct_address("can you help me") is False
