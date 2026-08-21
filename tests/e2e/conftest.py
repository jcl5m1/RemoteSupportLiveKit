"""pytest-playwright configuration for the e2e harness."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """Force Chromium to use fake camera/mic so tests run headless without real A/V hardware."""
    return {
        **browser_type_launch_args,
        "args": [
            *(browser_type_launch_args.get("args") or []),
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
        ],
    }


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Grant camera/mic permissions in every browser context."""
    return {
        **browser_context_args,
        "permissions": [*(browser_context_args.get("permissions") or []), "camera", "microphone"],
    }
