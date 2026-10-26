"""Open multi-party call: N browsers join the same /call/<id> link.

Requires a backend with OPEN_CALLS_ENABLED=true (BACKEND_URL, default the
production host) and Playwright Chromium. No service key is needed -- open
calls are link-only.
"""

from __future__ import annotations

import os
import uuid

import pytest

from playwright.sync_api import Browser, Page

BACKEND_URL = os.environ.get("BACKEND_URL", "https://remotesupport.lgitech.net").rstrip("/")
NUM_GUESTS = int(os.environ.get("OPEN_CALL_GUESTS", "3"))


def _join(browser: Browser, call_url: str, name: str) -> Page:
    context = browser.new_context(permissions=["camera", "microphone"])
    page = context.new_page()
    page.goto(call_url)
    page.wait_for_url("**/support-web/?call=*")
    page.fill("#nameInput", name)
    page.click("#joinCallBtn")
    page.wait_for_function("window.testState && window.testState.connected === true", timeout=30_000)
    return page


def _remote_media(page: Page) -> list[dict]:
    return page.evaluate(
        """() => [...window.testState.room.remoteParticipants.values()].map(p => ({
            name: p.name,
            video: [...p.videoTrackPublications.values()].some(t => t.isSubscribed && t.track),
            audio: [...p.audioTrackPublications.values()].some(t => t.isSubscribed && t.track),
        }))"""
    )


def test_open_call_all_guests_see_and_hear_each_other(browser: Browser) -> None:
    call_url = f"{BACKEND_URL}/call/e2e-{uuid.uuid4().hex[:10]}"
    names = [f"Guest {i + 1}" for i in range(NUM_GUESTS)]
    pages = [_join(browser, call_url, n) for n in names]
    try:
        for page, name in zip(pages, names, strict=True):
            page.wait_for_function(
                """n => [...window.testState.room.remoteParticipants.values()].filter(p =>
                    [...p.videoTrackPublications.values()].some(t => t.isSubscribed && t.track) &&
                    [...p.audioTrackPublications.values()].some(t => t.isSubscribed && t.track)
                ).length >= n""",
                arg=NUM_GUESTS - 1,
                timeout=30_000,
            )
            remotes = _remote_media(page)
            assert sorted(r["name"] for r in remotes) == sorted(n for n in names if n != name)
            assert page.locator("#tileGrid > *").count() == NUM_GUESTS
    finally:
        for page in pages:
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def _gcs_call_media(call_id: str) -> list[str]:
    import base64
    import json

    from google.cloud import storage

    creds = json.loads(base64.b64decode(os.environ["GCP_CREDENTIALS_B64"]))
    client = storage.Client.from_service_account_info(creds)
    return [
        b.name
        for b in client.list_blobs(os.environ["GCS_BUCKET"], prefix=f"calls/{call_id}/")
        if b.name.endswith((".ogg", ".webm", ".mp4")) and b.size
    ]


def test_open_call_recording_toggle(browser: Browser) -> None:
    """Any guest can start/stop recording; late joiners are recorded too; each
    person lands in GCS as one MP4 that anyone with the link can play."""
    import time

    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "backend", ".env"))
    if not os.environ.get("GCP_CREDENTIALS_B64") or not os.environ.get("GCS_BUCKET"):
        pytest.skip("GCS credentials not available")

    # Two guests = two participant egresses, under LiveKit Cloud's per-project
    # concurrent egress cap (observed 2-5 on the current plan).
    call_id = f"e2e-rec-{uuid.uuid4().hex[:8]}"
    call_url = f"{BACKEND_URL}/call/{call_id}"
    alice = _join(browser, call_url, "Alice")
    pages = [alice]
    try:
        assert alice.locator("#recBtn").is_visible()
        alice.click("#recBtn")
        alice.wait_for_function("window.testState.recording === true", timeout=15_000)
        assert alice.locator("#recBadge").is_visible()

        # Late joiner: told before joining, then recorded without any click.
        bob_ctx = browser.new_context(permissions=["camera", "microphone"])
        bob = bob_ctx.new_page()
        bob.goto(call_url)
        bob.wait_for_selector("#recNotice.active:not([hidden])")
        bob.fill("#nameInput", "Bob")
        bob.click("#joinCallBtn")
        bob.wait_for_function("window.testState.recording === true", timeout=30_000)
        pages.append(bob)
        bob.wait_for_timeout(12_000)
        assert bob.evaluate("window.testState.recordingSkipped") == 0

        # Anyone can stop it, not just whoever started it.
        bob.click("#recBtn")
        for page in pages:
            page.wait_for_function("window.testState.recording === false", timeout=15_000)
        assert alice.locator("#recBadge").is_hidden()
    finally:
        for page in pages:
            page.evaluate("window.testActions.hangUp()")
            page.context.close()

    deadline = time.time() + 120
    files: list[str] = []
    while time.time() < deadline:
        files = _gcs_call_media(call_id)
        if len(files) >= 2:
            break
        time.sleep(5)
    # One MP4 per person (camera + mic muxed by participant egress).
    for name in ("alice", "bob"):
        assert any(f"_guest-{name}-" in f and f.endswith(".mp4") for f in files), files

    # Anyone with the link can list and play them, even without joining.
    viewer = browser.new_context().new_page()
    try:
        viewer.goto(call_url)
        viewer.wait_for_selector("#viewRecsBtn:not([hidden])")
        viewer.click("#viewRecsBtn")
        viewer.wait_for_function(
            "window.testState.recordings && window.testState.recordings.length >= 2",
            timeout=15_000,
        )
        assert sorted(r["participant"] for r in viewer.evaluate("window.testState.recordings")) == [
            "Alice",
            "Bob",
        ]
        viewer.wait_for_function(
            "[...document.querySelectorAll('#recList video')].every(v => v.duration > 1)",
            timeout=30_000,
        )
    finally:
        viewer.context.close()


def test_open_call_device_selection_and_mute_all(browser: Browser) -> None:
    """Pick mic/camera before and during the call; one button mutes both."""
    call_url = f"{BACKEND_URL}/call/e2e-dev-{uuid.uuid4().hex[:8]}"

    # Pre-join: preview + device pickers (Chromium fake devices).
    ctx = browser.new_context(permissions=["camera", "microphone"])
    alice = ctx.new_page()
    alice.goto(call_url)
    alice.wait_for_function(
        "(window.testState.deviceKinds || []).includes('videoinput')", timeout=15_000
    )
    assert alice.evaluate(
        "document.getElementById('prejoinPreview').srcObject.getVideoTracks().length"
    ) == 1
    mic_ids = alice.eval_on_selector_all(
        "#prejoinDevices select[data-kind=audioinput] option", "els => els.map(e => e.value)"
    )
    assert len(mic_ids) >= 2, mic_ids
    alice.select_option("#prejoinDevices select[data-kind=audioinput]", mic_ids[1])
    alice.fill("#nameInput", "Alice")
    alice.click("#joinCallBtn")
    alice.wait_for_function("window.testState.connected === true", timeout=30_000)
    bob = _join(browser, call_url, "Bob")
    pages = [alice, bob]
    try:
        alice.wait_for_function(
            "window.testState.room.localParticipant.isMicrophoneEnabled &&"
            " window.testState.room.localParticipant.isCameraEnabled",
            timeout=15_000,
        )
        # The pre-join choice is the device actually used in the call.
        assert alice.evaluate("window.testState.room.getActiveDevice('audioinput')") == mic_ids[1]

        # Switch mid-call from the settings sheet.
        alice.click("#settingsBtn")
        alice.wait_for_selector("#settingsDevices select[data-kind=audioinput]")
        alice.select_option("#settingsDevices select[data-kind=audioinput]", mic_ids[0])
        alice.wait_for_function(
            f"window.testState.room.getActiveDevice('audioinput') === {mic_ids[0]!r}",
            timeout=10_000,
        )
        alice.click("#settingsCloseBtn")

        # Or straight from the arrow on the mic button (Meet-style menu).
        alice.click("#micMenuBtn")
        alice.click(f"#deviceMenu button[data-kind=audioinput][data-device-id={mic_ids[1]!r}]")
        alice.wait_for_function(
            f"window.testState.room.getActiveDevice('audioinput') === {mic_ids[1]!r}",
            timeout=10_000,
        )
        assert alice.locator("#deviceMenu").is_hidden()
        alice.click("#camMenuBtn")
        alice.wait_for_selector("#deviceMenu button[data-kind=videoinput][aria-checked=true]")
        alice.keyboard.press("Escape")
        assert alice.locator("#deviceMenu").is_hidden()

        # One button mutes both; Bob sees both of Alice's tracks muted.
        alice.click("#muteAllBtn")
        alice.wait_for_function(
            "!window.testState.room.localParticipant.isMicrophoneEnabled &&"
            " !window.testState.room.localParticipant.isCameraEnabled"
        )
        remote_alice = """() => [...window.testState.room.remoteParticipants.values()]
            .find(p => p.name === 'Alice')"""
        bob.wait_for_function(
            f"(() => {{ const a = ({remote_alice})(); return a &&"
            " [...a.trackPublications.values()].filter(t => t.isMuted).length >= 2; })()",
            timeout=15_000,
        )
        alice.click("#muteAllBtn")
        alice.wait_for_function(
            "window.testState.room.localParticipant.isMicrophoneEnabled &&"
            " window.testState.room.localParticipant.isCameraEnabled"
        )
        bob.wait_for_function(
            f"(() => {{ const a = ({remote_alice})(); return a &&"
            " [...a.trackPublications.values()].every(t => !t.isMuted); })()",
            timeout=15_000,
        )
    finally:
        for page in pages:
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


@pytest.mark.parametrize("device", ["iPhone 13", "iPhone SE"])
def test_open_call_fits_phone_screen(browser: Browser, playwright, device: str) -> None:
    """Control bar fits one row on phones (390px and 320px) without overflow."""
    iphone = playwright.devices[device]
    ctx = browser.new_context(
        **{k: v for k, v in iphone.items() if k != "default_browser_type"},
        permissions=["camera", "microphone"],
    )
    page = ctx.new_page()
    try:
        page.goto(f"{BACKEND_URL}/call/e2e-phone-{uuid.uuid4().hex[:8]}")
        page.fill("#nameInput", "Phone")
        page.click("#joinCallBtn")
        page.wait_for_function("window.testState.connected === true", timeout=30_000)
        bar = page.evaluate(
            "(() => { const r = document.getElementById('controlBar').getBoundingClientRect();"
            " return { right: r.right, width: window.innerWidth, height: r.height,"
            " overflow: document.documentElement.scrollWidth > window.innerWidth }; })()"
        )
        assert not bar["overflow"], bar
        assert bar["height"] < 70, bar  # a single row of buttons
        assert page.locator("#settingsBtn").is_visible()
        assert page.locator("#muteAllBtn").is_visible()
    finally:
        page.evaluate("window.testActions.hangUp()")
        ctx.close()


def test_open_call_leave_vs_end_for_everyone(browser: Browser) -> None:
    """Red button offers Leave (just you) and End call for everyone."""
    call_url = f"{BACKEND_URL}/call/e2e-end-{uuid.uuid4().hex[:8]}"
    alice, bob, carol = (_join(browser, call_url, n) for n in ("Alice", "Bob", "Carol"))
    try:
        for page in (alice, bob, carol):
            page.wait_for_function(
                "window.testState.room.remoteParticipants.size === 2", timeout=20_000
            )

        bob.click("#hangupBtn")
        bob.click("#leaveCallItem")
        bob.wait_for_function("window.testState.connected === false")
        assert bob.evaluate("window.testState.endTitle") == "You left the call"
        alice.wait_for_function("window.testState.room.remoteParticipants.size === 1")
        assert carol.evaluate("window.testState.connected") is True

        alice.click("#hangupBtn")
        alice.click("#endCallItem")
        for page in (alice, carol):
            page.wait_for_function("window.testState.connected === false", timeout=15_000)
            assert page.locator("#endCard").is_visible()
        assert carol.evaluate("window.testState.endDetail") == "Alice ended the call."
        assert alice.evaluate("window.testState.endDetail") == "You ended the call for everyone."
    finally:
        for page in (alice, bob, carol):
            page.context.close()
