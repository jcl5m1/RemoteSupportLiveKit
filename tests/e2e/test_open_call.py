"""Open multi-party call: N browsers join the same <base>/?call=<code> link.

Requires a backend with OPEN_CALLS_ENABLED=true (BACKEND_URL, default the
production host) and Playwright Chromium. No service key is needed -- open
calls are link-only.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

import pytest

from playwright.sync_api import Browser, Page

BACKEND_URL = os.environ.get("BACKEND_URL", "https://remotesupport.lgitech.net").rstrip("/")
NUM_GUESTS = int(os.environ.get("OPEN_CALL_GUESTS", "3"))


def _join(browser: Browser, call_url: str, name: str) -> Page:
    context = browser.new_context(permissions=["camera", "microphone"])
    page = context.new_page()
    page.goto(call_url)
    page.wait_for_selector("#prejoin:not([hidden])")
    assert "/support-web" not in page.url  # meeting links are <base>/?call=<code>
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
    call_url = f"{BACKEND_URL}/?call=e2e-{uuid.uuid4().hex[:10]}"
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
            assert page.locator("#tileGrid > .tile").count() == NUM_GUESTS
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
    call_url = f"{BACKEND_URL}/?call={call_id}"
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
        assert viewer.inner_text("#recCloseBtn") == "\u2715"  # ✕, like the side panel
        viewer.click("#recCloseBtn")
        assert viewer.locator("#recordings").is_hidden()
    finally:
        viewer.context.close()


def test_open_call_device_selection_and_mute_all(browser: Browser) -> None:
    """Pick mic/camera before and during the call; one button mutes both."""
    call_url = f"{BACKEND_URL}/?call=e2e-dev-{uuid.uuid4().hex[:8]}"

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

        # The mic/camera buttons (and their device arrows) are gone; devices
        # are chosen only in Settings, and mute is the single combined button.
        for gone in ("micBtn", "camBtn", "micMenuBtn", "camMenuBtn"):
            assert alice.locator(f"#{gone}").count() == 0

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
        page.goto(f"{BACKEND_URL}/?call=e2e-phone-{uuid.uuid4().hex[:8]}")
        page.wait_for_selector("#prejoin:not([hidden])")
        # Join card (device names, code row + copy button) fits the screen.
        assert page.evaluate(
            "document.querySelector('#prejoin .card').getBoundingClientRect().right"
            " <= window.innerWidth"
        )
        assert page.locator("#copyCodeBtn").is_visible()
        page.fill("#nameInput", "Phone")
        page.click("#joinCallBtn")
        page.wait_for_function("window.testState.connected === true", timeout=30_000)
        bar = page.evaluate(
            "(() => { const r = document.getElementById('controlBar').getBoundingClientRect();"
            " return { right: r.right, width: window.innerWidth, height: r.height,"
            " overflow: document.documentElement.scrollWidth > window.innerWidth }; })()"
        )
        assert not bar["overflow"], bar
        # The whole bar must sit inside the visible viewport. Real phone
        # browsers make 100vh taller than the visible area (toolbar), so the
        # layout must not be sized with vh.
        assert page.evaluate(
            "document.getElementById('controlBar').getBoundingClientRect().bottom"
            " <= window.innerHeight"
        )
        assert page.evaluate("getComputedStyle(document.getElementById('callUI')).position") == "fixed"
        assert bar["height"] < 70, bar  # a single row of buttons
        assert page.locator("#settingsBtn").is_visible()
        for btn in ("muteAllBtn", "hangupBtn", "recBtn", "peopleBtn", "chatBtn", "settingsBtn"):
            box = page.locator(f"#{btn}").bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= bar["width"], (btn, box)
    finally:
        page.evaluate("window.testActions.hangUp()")
        ctx.close()


def test_hangup_leaves_and_last_person_ends_meeting(browser: Browser) -> None:
    """Red button just leaves; the meeting ends when the last person leaves."""
    import time

    import requests

    code = f"e2e-end-{uuid.uuid4().hex[:8]}"
    call_url = f"{BACKEND_URL}/?call={code}"
    info_url = f"{BACKEND_URL}/v1/calls/{code}"
    alice, bob, carol = (_join(browser, call_url, n) for n in ("Alice", "Bob", "Carol"))
    try:
        for page in (alice, bob, carol):
            page.wait_for_function(
                "window.testState.room.remoteParticipants.size === 2", timeout=20_000
            )

        bob.click("#hangupBtn")  # no menu: leaves immediately
        bob.wait_for_function("window.testState.connected === false")
        assert bob.evaluate("window.testState.endTitle") == "You left the call"
        assert bob.evaluate("window.testState.endedSession") is False
        alice.wait_for_function("window.testState.room.remoteParticipants.size === 1")

        alice.click("#hangupBtn")
        alice.wait_for_function("window.testState.connected === false")
        assert alice.evaluate("window.testState.endedSession") is False
        carol.wait_for_function("window.testState.room.remoteParticipants.size === 0")
        assert carol.evaluate("window.testState.connected") is True
        assert requests.get(info_url, timeout=10).json()["room_active"] is True

        # Last one out ends the meeting right away (not after the 60 s
        # departure timeout).
        carol.click("#hangupBtn")
        carol.wait_for_function("window.testState.connected === false")
        assert carol.evaluate("window.testState.endedSession") is True
        assert "meeting has ended" in carol.evaluate("window.testState.endDetail")
        deadline = time.monotonic() + 10
        while requests.get(info_url, timeout=10).json()["room_active"]:
            assert time.monotonic() < deadline, "room still active after last person left"
            time.sleep(1)
    finally:
        for page in (alice, bob, carol):
            page.context.close()

def test_open_call_data_channel_log(browser: Browser) -> None:
    """Data tab lists each data message's time, source, description, size —
    never its content."""
    call_url = f"{BACKEND_URL}/?call=e2e-data-{uuid.uuid4().hex[:8]}"
    alice, bob = _join(browser, call_url, "Alice"), _join(browser, call_url, "Bob")
    secret = "the launch code is 4242"
    try:
        for page in (alice, bob):
            page.wait_for_function(
                "window.testState.room.remoteParticipants.size === 1", timeout=20_000
            )
        alice.evaluate(f"window.testActions.sendChat({secret!r})")
        bob.wait_for_function("window.testState.dataLog.length >= 1", timeout=10_000)

        (received,) = bob.evaluate("window.testActions.getDataLog()")
        assert received["dir"] == "in"
        assert received["source"] == "Alice"
        assert received["topic"] == "chat"
        assert received["description"] == f"Chat message ({len(secret)} chars)"
        assert received["reliable"] is True
        (sent,) = alice.evaluate("window.testActions.getDataLog()")
        assert sent["dir"] == "out" and sent["source"] == "You"
        assert sent["size"] == received["size"] > len(secret)

        # The Data tab sits next to People and Chat in the side panel.
        bob.click("#chatBtn")
        bob.click("#tabDataBtn")
        assert bob.locator("#tabDataBtn.active").count() == 1
        assert bob.locator("#dataBtn").count() == 0  # no control-bar button
        bob.wait_for_selector("#dataLog tbody tr")
        table = bob.inner_text("#dataTab")
        assert "Alice" in table and "Chat message" in table and f"{received['size']} B" in table
        assert secret not in table and "4242" not in table
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def test_join_screen_start_meeting_and_editable_code(browser: Browser) -> None:
    """No code -> join screen with a generated code and "Start meeting"; the
    code is editable, validated, and drives the link, URL, and room joined."""
    page = browser.new_context(
        permissions=["camera", "microphone", "clipboard-read", "clipboard-write"]
    ).new_page()
    other = None
    try:
        page.goto(f"{BACKEND_URL}/")  # entry point, no meeting code
        page.wait_for_selector("#prejoin:not([hidden])")
        code = page.input_value("#callCodeInput")
        assert re.fullmatch(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", code), code
        assert page.inner_text("#joinCallBtn") == "Start meeting"
        assert page.locator("#callLink").count() == 0  # URL text removed
        assert page.locator("#copyLinkBtn").count() == 0
        page.click("#copyCodeBtn")  # copy icon next to the code
        page.wait_for_function(f"(window.testState.copiedUrl || '').endsWith('/?call={code}')")
        assert page.locator("#copyCodeBtn.copied").count() == 1
        assert page.url.endswith(f"/?call={code}")

        # Invalid code: flagged and the button disabled.
        page.fill("#callCodeInput", "-bad-")
        assert page.locator("#codeError").is_visible()
        assert page.locator("#joinCallBtn").is_disabled()

        # Edited code: normalized, button becomes "Join now", link/URL follow.
        custom = f"E2E Edit {uuid.uuid4().hex[:6]}"
        page.fill("#callCodeInput", custom)
        expected = custom.lower().replace(" ", "-")
        assert page.input_value("#callCodeInput") == expected
        assert page.inner_text("#joinCallBtn") == "Join now"
        page.click("#copyCodeBtn")
        page.wait_for_function(f"(window.testState.copiedUrl || '').endsWith('/?call={expected}')")
        assert page.evaluate("navigator.clipboard.readText()").endswith(f"/?call={expected}")
        assert page.url.endswith(f"/?call={expected}")

        page.fill("#nameInput", "Starter")
        page.click("#joinCallBtn")
        page.wait_for_function("window.testState.connected === true", timeout=30_000)
        assert page.evaluate("window.testState.room.name") == f"call_{expected}"

        # Someone using the shared link lands in the same meeting.
        other = _join(browser, f"{BACKEND_URL}/?call={expected}", "Guest")
        page.wait_for_function("window.testState.room.remoteParticipants.size === 1")
    finally:
        for p in (page, other):
            if p:
                p.evaluate("window.testActions.hangUp()")
                p.context.close()


def test_chat_chime_and_unread_badge(browser: Browser) -> None:
    """Incoming chat chimes; a red badge counts unread until chat is opened."""
    call_url = f"{BACKEND_URL}/?call=e2e-chime-{uuid.uuid4().hex[:8]}"
    alice, bob = _join(browser, call_url, "Alice"), _join(browser, call_url, "Bob")
    try:
        for page in (alice, bob):
            page.wait_for_function(
                "window.testState.room.remoteParticipants.size === 1", timeout=20_000
            )
        # Bob's chat panel is closed: chime + badge.
        alice.evaluate("window.testActions.sendChat('one')")
        alice.evaluate("window.testActions.sendChat('two')")
        bob.wait_for_function("window.testState.chatUnread === 2", timeout=10_000)
        assert bob.evaluate("window.testState.chimeCount") == 2
        assert bob.inner_text("#chatBtn .badge") == "2"
        assert bob.locator("#chatBtn .badge").is_visible()
        assert alice.evaluate("window.testState.chimeCount || 0") == 0  # own messages: silent

        # Opening chat clears the badge; messages while it is open only chime.
        bob.click("#chatBtn")
        assert bob.locator("#chatBtn .badge").is_hidden()
        assert bob.evaluate("window.testState.chatUnread") == 0
        alice.evaluate("window.testActions.sendChat('three')")
        bob.wait_for_function("window.testState.chimeCount === 3", timeout=10_000)
        assert bob.locator("#chatBtn .badge").is_hidden()
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def test_phone_chat_input_stays_visible_while_typing(browser: Browser, playwright) -> None:
    """Typing in chat on a phone: control bar yields its space and the input
    stays inside the (keyboard-shrunk) viewport."""
    iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
    page = browser.new_context(**iphone, permissions=["camera", "microphone"]).new_page()
    try:
        page.goto(f"{BACKEND_URL}/?call=e2e-kbd-{uuid.uuid4().hex[:8]}")
        page.fill("#nameInput", "Phone")
        page.click("#joinCallBtn")
        page.wait_for_function("window.testState.connected === true", timeout=30_000)
        page.click("#chatBtn")
        page.click("#chatInput")
        assert page.evaluate("document.body.classList.contains('typing')")
        assert page.locator("#controlBar").is_hidden()
        assert page.get_attribute("#chatInput", "placeholder") == "Message, or /show <link>"
        # No send button on phones: the keyboard's Enter ("send") key sends.
        assert page.locator("#chatSend").is_hidden()
        assert page.get_attribute("#chatInput", "enterkeyhint") == "send"
        page.keyboard.type("sent with enter")
        page.keyboard.press("Enter")
        page.wait_for_function(
            "window.testState.chatMessages.some(m => m.mine && m.text === 'sent with enter')"
        )
        assert page.input_value("#chatInput") == ""
        # Emulate the keyboard taking ~45% of the screen.
        size = page.viewport_size
        page.set_viewport_size({"width": size["width"], "height": int(size["height"] * 0.55)})
        page.wait_for_timeout(300)
        bottom = page.evaluate("document.getElementById('chatInput').getBoundingClientRect().bottom")
        assert bottom <= page.evaluate("window.visualViewport.height"), bottom
        page.evaluate("document.getElementById('chatInput').blur()")
        page.set_viewport_size(size)
        page.wait_for_timeout(300)
        assert page.locator("#controlBar").is_visible()
    finally:
        page.evaluate("window.testActions.hangUp()")
        page.context.close()


def test_legacy_support_web_call_link_still_works(browser: Browser) -> None:
    """Old /support-web/?call=<code> links open the meeting; the address bar
    is rewritten to the canonical <base>/?call=<code>."""
    code = f"e2e-legacy-{uuid.uuid4().hex[:6]}"
    page = browser.new_context(permissions=["camera", "microphone"]).new_page()
    try:
        page.goto(f"{BACKEND_URL}/support-web/?call={code}")
        page.wait_for_selector("#prejoin:not([hidden])")
        assert page.input_value("#callCodeInput") == code
        assert page.url.endswith(f"/?call={code}")
    finally:
        page.context.close()


@pytest.mark.parametrize("query", ["?call=team-standup", "?=team-standup", "?team-standup"])
def test_meeting_code_query_shorthand(browser: Browser, query: str) -> None:
    """?call=<code> is the canonical meeting link; ?=<code> and ?<code> redirect to it."""
    page = browser.new_context(permissions=["camera", "microphone"]).new_page()
    try:
        page.goto(f"{BACKEND_URL}/{query}")
        page.wait_for_selector("#prejoin:not([hidden])")
        assert page.input_value("#callCodeInput") == "team-standup"
        assert page.inner_text("#joinCallBtn") == "Join now"
        assert page.url.endswith("/?call=team-standup")
    finally:
        page.context.close()


def test_path_style_meeting_link_redirects(browser: Browser) -> None:
    """Older /call/<code> links still work and land on <base>/?call=<code>."""
    page = browser.new_context(permissions=["camera", "microphone"]).new_page()
    try:
        page.goto(f"{BACKEND_URL}/call/Team-Standup")
        page.wait_for_selector("#prejoin:not([hidden])")
        assert page.input_value("#callCodeInput") == "team-standup"
        assert page.url.endswith("/?call=team-standup")
    finally:
        page.context.close()


IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/a/a9/Example.jpg"
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _pair(browser: Browser, prefix: str) -> tuple[Page, Page]:
    call_url = f"{BACKEND_URL}/?call={prefix}-{uuid.uuid4().hex[:8]}"
    alice, bob = _join(browser, call_url, "Alice"), _join(browser, call_url, "Bob")
    for page in (alice, bob):
        page.wait_for_function("window.testState.room.remoteParticipants.size === 1", timeout=20_000)
    return alice, bob


def _send_from_chat(page: Page, text: str) -> None:
    if page.locator("#chatTab").is_hidden():
        page.click("#chatBtn")
    page.fill("#chatInput", text)
    page.press("#chatInput", "Enter")


def test_show_media_full_screen_on_other_clients(browser: Browser) -> None:
    alice, bob = _pair(browser, "e2e-show")
    try:
        _send_from_chat(alice, f"/show {IMAGE_URL}")
        bob.wait_for_function("window.testState.showOpen === true", timeout=20_000)
        assert bob.evaluate("window.testState.lastShow.kind") == "image"
        assert bob.locator("#showOverlay").is_visible()
        assert "Alice shared an image" in bob.inner_text("#showTitle")
        assert bob.get_attribute("#showStage img", "src") == IMAGE_URL
        # Full screen: the overlay covers the viewport.
        assert bob.evaluate(
            "(() => { const r = document.getElementById('showOverlay').getBoundingClientRect();"
            " return r.width === innerWidth && r.height === innerHeight; })()"
        )
        # Sender doesn't get the overlay, but sees the shared message.
        assert alice.evaluate("window.testState.showOpen || false") is False
        alice.wait_for_function("window.testState.chatMessages.some(m => m.mine && m.show)")

        bob.click("#showCloseBtn")
        assert bob.locator("#showOverlay").is_hidden()

        _send_from_chat(alice, f"/show {YOUTUBE_URL}")
        bob.wait_for_function("window.testState.lastShow.kind === 'youtube'", timeout=20_000)
        src = bob.get_attribute("#showStage iframe", "src")
        assert src.startswith("https://www.youtube.com/embed/dQw4w9WgXcQ")
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def test_show_unresolvable_link_errors_on_sender_only(browser: Browser) -> None:
    alice, bob = _pair(browser, "e2e-showbad")
    try:
        bad = "https://example.com/definitely-missing-page-404"
        _send_from_chat(alice, f"/show {bad}")
        alice.wait_for_function("window.testState.chatError", timeout=20_000)
        assert "HTTP 404" in alice.evaluate("window.testState.chatError")
        assert alice.locator(".chat-error").is_visible()
        assert alice.input_value("#chatInput") == f"/show {bad}"  # kept for fixing
        _send_from_chat(alice, "/show http://127.0.0.1:8000/healthz")
        alice.wait_for_function(
            "window.testState.chatError.includes(\"isn't reachable\")", timeout=20_000
        )
        bob.wait_for_timeout(1500)
        assert bob.evaluate("window.testState.chatMessages.length") == 0  # nothing sent
        assert bob.evaluate("window.testState.showOpen || false") is False
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def test_settings_switches_override_mute(browser: Browser) -> None:
    """Camera/mic off in Settings stays off; mute only pauses what's on."""
    alice, bob = _pair(browser, "e2e-switch")
    lp = "window.testState.room.localParticipant"
    remote_cam_muted = """(() => { const a = [...window.testState.room.remoteParticipants.values()][0];
        const c = a && [...a.videoTrackPublications.values()][0];
        return !c || c.isMuted; })()"""
    try:
        alice.click("#settingsBtn")
        alice.click("label.toggle:has(#camEnabledToggle)")
        alice.wait_for_function(f"!{lp}.isCameraEnabled && {lp}.isMicrophoneEnabled")
        assert alice.inner_text("#settingsCloseBtn") == "\u2715"  # ✕, like the side panel
        alice.click("#settingsCloseBtn")
        assert alice.locator("#settings").is_hidden()
        bob.wait_for_function(remote_cam_muted, timeout=15_000)

        alice.click("#muteAllBtn")  # mute: mic off too
        alice.wait_for_function(f"!{lp}.isMicrophoneEnabled && !{lp}.isCameraEnabled")
        alice.click("#muteAllBtn")  # unmute: mic back, camera stays off (Settings wins)
        alice.wait_for_function(f"{lp}.isMicrophoneEnabled")
        alice.wait_for_timeout(500)
        assert alice.evaluate(f"{lp}.isCameraEnabled") is False

        alice.click("#settingsBtn")
        alice.click("label.toggle:has(#camEnabledToggle)")
        alice.wait_for_function(f"{lp}.isCameraEnabled")
        assert alice.evaluate("localStorage.getItem('media.cam')") == "on"

        # Mic off in Settings, then muting/unmuting never turns it on.
        alice.click("label.toggle:has(#micEnabledToggle)")
        alice.wait_for_function(f"!{lp}.isMicrophoneEnabled")
        alice.click("#settingsCloseBtn")
        alice.click("#muteAllBtn")
        alice.click("#muteAllBtn")
        alice.wait_for_function(f"{lp}.isCameraEnabled")
        assert alice.evaluate(f"{lp}.isMicrophoneEnabled") is False
    finally:
        for page in (alice, bob):
            page.evaluate("localStorage.clear()")
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def _touch(cdp, kind: str, points: list[tuple[float, float]]) -> None:
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": kind, "touchPoints": [{"x": x, "y": y, "id": i} for i, (x, y) in enumerate(points)]},
    )


def test_shown_image_fits_landscape_phone_and_pinch_zooms(browser: Browser, playwright) -> None:
    """Image fits width AND height in landscape; pinch, pan, double-tap work."""
    call_url = f"{BACKEND_URL}/?call=e2e-zoom-{uuid.uuid4().hex[:8]}"
    alice = _join(browser, call_url, "Alice")
    land = {k: v for k, v in playwright.devices["iPhone 13 landscape"].items() if k != "default_browser_type"}
    bob = browser.new_context(**land, permissions=["camera", "microphone"]).new_page()
    try:
        bob.goto(call_url)
        bob.fill("#nameInput", "Bob")
        bob.click("#joinCallBtn")
        bob.wait_for_function("window.testState.connected === true", timeout=30_000)
        alice.wait_for_function("window.testState.room.remoteParticipants.size === 1", timeout=20_000)
        _send_from_chat(alice, f"/show {IMAGE_URL}")
        bob.wait_for_function("window.testState.showOpen === true", timeout=20_000)
        bob.wait_for_function("document.querySelector('#showStage img').complete")

        fit = bob.evaluate(
            """(() => { const img = document.querySelector('#showStage img');
                const i = img.getBoundingClientRect();
                const s = document.getElementById('showStage').getBoundingClientRect();
                return { fit: getComputedStyle(img).objectFit, top: i.top, bottom: i.bottom,
                         left: i.left, right: i.right, sTop: s.top, sBottom: s.bottom,
                         w: innerWidth, h: innerHeight }; })()"""
        )
        assert fit["w"] > fit["h"], fit  # landscape
        assert fit["fit"] == "contain"
        # The image box is exactly the stage, which is inside the screen, so
        # 'contain' shows the whole image (both dimensions).
        assert abs(fit["top"] - fit["sTop"]) < 1 and abs(fit["bottom"] - fit["sBottom"]) < 1, fit
        assert fit["bottom"] <= fit["h"] and fit["right"] <= fit["w"] and fit["left"] >= 0, fit

        cx, cy = fit["w"] / 2, (fit["sTop"] + fit["sBottom"]) / 2
        cdp = bob.context.new_cdp_session(bob)
        # Pinch out with two fingers.
        _touch(cdp, "touchStart", [(cx - 40, cy), (cx + 40, cy)])
        for step in range(1, 8):
            d = 40 + step * 20
            _touch(cdp, "touchMove", [(cx - d, cy), (cx + d, cy)])
        _touch(cdp, "touchEnd", [])
        zoomed = bob.evaluate("window.testState.imageZoom.scale")
        assert zoomed > 2.5, zoomed
        # One-finger pan while zoomed.
        before = bob.evaluate("window.testState.imageZoom.tx")
        _touch(cdp, "touchStart", [(cx, cy)])
        for step in range(1, 6):
            _touch(cdp, "touchMove", [(cx + step * 15, cy)])
        _touch(cdp, "touchEnd", [])
        assert bob.evaluate("window.testState.imageZoom.tx") > before
        # Double-tap resets to fit.
        for _ in range(2):
            _touch(cdp, "touchStart", [(cx, cy)])
            _touch(cdp, "touchEnd", [])
            bob.wait_for_timeout(80)
        bob.wait_for_function("window.testState.imageZoom.scale === 1")
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def test_shown_image_wheel_and_double_click_zoom_on_desktop(browser: Browser) -> None:
    alice, bob = _pair(browser, "e2e-wheel")
    try:
        _send_from_chat(alice, f"/show {IMAGE_URL}")
        bob.wait_for_function("window.testState.showOpen === true", timeout=20_000)
        box = bob.locator("#showStage img").bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        bob.mouse.move(x, y)
        bob.mouse.wheel(0, -600)
        bob.wait_for_function("window.testState.imageZoom.scale > 1.5")
        bob.mouse.dblclick(x, y)  # zoomed -> back to fit
        bob.wait_for_function("window.testState.imageZoom.scale === 1")
        bob.mouse.dblclick(x, y)  # fit -> 2.5x
        bob.wait_for_function("window.testState.imageZoom.scale === 2.5")
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


@pytest.fixture(scope="session")
def portrait_y4m(tmp_path_factory) -> Path:
    """Portrait (360x640) clip for Chrome's fake camera, generated on demand."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("cam") / "portrait_360x640.y4m"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=10",
         "-t", "1", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


def test_portrait_phone_video_shows_as_portrait_tile(
    browser: Browser, browser_type, playwright, portrait_y4m: Path
) -> None:
    """A phone held in portrait (camera 360x640) gets portrait tiles on every
    screen instead of being cropped to 16:9."""
    phone_browser = browser_type.launch(
        args=[
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-video-capture={portrait_y4m}",
        ]
    )
    call_url = f"{BACKEND_URL}/?call=e2e-portrait-{uuid.uuid4().hex[:8]}"
    desk = _join(browser, call_url, "Desk")
    iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
    phone = phone_browser.new_context(**iphone, permissions=["camera", "microphone"]).new_page()
    tile_box = """(name => { const t = [...document.querySelectorAll('.tile')]
        .find(el => el.querySelector('.nm').textContent.startsWith(name));
        const r = t.getBoundingClientRect(); const g = document.getElementById('tileGrid').getBoundingClientRect();
        return { w: r.width, h: r.height, portrait: t.classList.contains('portrait'),
                 inside: r.bottom <= g.bottom + 1 && r.right <= g.right + 1 }; })"""
    try:
        phone.goto(call_url)
        phone.fill("#nameInput", "Phone")
        phone.click("#joinCallBtn")
        phone.wait_for_function("window.testState.connected === true", timeout=30_000)

        # Everyone else (desktop): Phone's tile is portrait-shaped and fits.
        desk.wait_for_function(
            "Object.entries(window.testState.tileAspects || {})"
            ".some(([id, ar]) => id.startsWith('guest-phone') && ar < 0.7)",
            timeout=30_000,
        )
        box = desk.evaluate(tile_box, "Phone")
        assert box["portrait"] and box["h"] > box["w"] * 1.5, box
        assert box["inside"], box
        # The desktop camera (landscape) stays landscape.
        own = desk.evaluate(tile_box, "Desk")
        assert not own["portrait"] and own["w"] > own["h"], own

        # The phone's own preview is portrait too.
        phone.wait_for_function(
            "Object.entries(window.testState.tileAspects || {})"
            ".some(([id, ar]) => id.startsWith('guest-phone') && ar < 0.7)",
            timeout=20_000,
        )
        mine = phone.evaluate(tile_box, "Phone")
        assert mine["portrait"] and mine["h"] > mine["w"], mine
    finally:
        for page in (desk, phone):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()
        phone_browser.close()


def test_self_view_is_mini_and_other_caller_fills_stage(browser: Browser) -> None:
    code = f"e2e-pip-{uuid.uuid4().hex[:8]}"
    call_url = f"{BACKEND_URL}/?call={code}"
    rect = """(name => { const t = [...document.querySelectorAll('.tile')]
        .find(el => el.querySelector('.nm').textContent.startsWith(name));
        const r = t.getBoundingClientRect(); const g = document.getElementById('tileGrid').getBoundingClientRect();
        return { w: r.width, h: r.height, right: r.right, bottom: r.bottom,
                 gw: g.width, gh: g.height, gright: g.right, gbottom: g.bottom,
                 pip: t.classList.contains('self-pip') }; })"""
    alice = _join(browser, call_url, "Alice")
    bob = None
    try:
        # Alone: your own video is a normal full tile.
        assert alice.evaluate("window.testState.selfPip") is False
        assert alice.evaluate(rect, "Alice")["pip"] is False

        bob = _join(browser, call_url, "Bob")
        alice.wait_for_function("window.testState.selfPip === true", timeout=20_000)
        me = alice.evaluate(rect, "Alice")
        other = alice.evaluate(rect, "Bob")
        # Mine: small, floating in the bottom-right corner of the stage.
        assert me["w"] * me["h"] < 0.12 * me["gw"] * me["gh"], me
        assert me["gright"] - me["right"] < 30 and me["gbottom"] - me["bottom"] < 30, me
        # Theirs: fills the stage (full width or full height).
        assert other["w"] > 0.9 * other["gw"] or other["h"] > 0.9 * other["gh"], other

        # Tap your small view: it becomes the big view and theirs floats (swap).
        alice.locator(".tile.self-pip").click()
        alice.wait_for_function("window.testState.stageFocus === 'self'")
        me, other = alice.evaluate(rect, "Alice"), alice.evaluate(rect, "Bob")
        assert not me["pip"] and other["pip"], (me, other)
        assert me["w"] > 0.9 * me["gw"] or me["h"] > 0.9 * me["gh"], me
        assert other["w"] * other["h"] < 0.12 * other["gw"] * other["gh"], other
        # Tap their small view: swap back.
        alice.locator(".tile.self-pip", has_text="Bob").click()
        alice.wait_for_function("window.testState.stageFocus === 'remote' && window.testState.selfPip")

        # Other caller leaves: your video goes back to full size.
        bob.evaluate("window.testActions.hangUp()")
        bob.context.close()
        bob = None
        alice.wait_for_function("window.testState.selfPip === false", timeout=20_000)
    finally:
        alice.evaluate("window.testActions.hangUp()")
        alice.context.close()
        if bob:
            bob.context.close()


# Self-hosted (served by the backend under test) so the test has no
# third-party dependency.
AUDIO_URL = f"{BACKEND_URL}/support-web/test-assets/caller_prompt.wav"


def test_show_audio_player_controls(browser: Browser) -> None:
    """Audio /show: play/pause, repeat (default off), timeline current/total."""
    alice, bob = _pair(browser, "e2e-audio")
    st = "window.testState.audioPlayer"
    try:
        _send_from_chat(alice, f"/show {AUDIO_URL}")
        try:
            bob.wait_for_function("window.testState.showOpen === true", timeout=20_000)
        except Exception as exc:  # say why (sender-side link error vs delivery)
            sender_error = alice.evaluate("window.testState.chatError || null")
            raise AssertionError(f"audio /show never opened on Bob; sender error: {sender_error}") from exc
        assert bob.locator("#audioPlayer").is_visible()
        assert bob.inner_text("#audioPlayer .ap-title") == "caller_prompt.wav"
        assert bob.inner_text("#showTitle") == "Alice shared an audio file"
        bob.wait_for_function(f"{st} && Number.isFinite({st}.duration) && {st}.duration > 1")
        assert bob.get_attribute("#apRepeat", "aria-pressed") == "false"  # repeat off by default
        assert bob.evaluate(f"{st}.loop") is False

        # Make sure it's playing (autoplay may or may not be allowed).
        if not bob.evaluate(f"{st}.playing"):
            bob.click("#apPlay")
        bob.wait_for_function(f"{st}.playing && {st}.current > 0.2")
        assert bob.get_attribute("#apPlay", "title") == "Pause"
        total = bob.evaluate(f"{st}.duration")
        assert bob.inner_text("#apTime").endswith(f"/ 0:{int(total):02d}")

        bob.click("#apPlay")  # pause
        bob.wait_for_function(f"!{st}.playing")
        assert bob.get_attribute("#apPlay", "title") == "Play"

        # Seek with the timeline.
        bob.evaluate(
            "(() => { const s = document.getElementById('apSeek'); s.value = 1.0;"
            " s.dispatchEvent(new Event('input')); s.dispatchEvent(new Event('change')); })()"
        )
        bob.wait_for_function(f"Math.abs({st}.current - 1.0) < 0.15")
        assert bob.inner_text("#apTime").startswith("0:01")

        # Repeat on: it keeps playing past the end.
        bob.click("#apRepeat")
        assert bob.get_attribute("#apRepeat", "aria-pressed") == "true"
        bob.click("#apPlay")
        bob.wait_for_timeout(int((total + 1.0) * 1000))
        assert bob.evaluate(f"{st}.playing") is True

        bob.click("#showCloseBtn")  # closing stops playback
        assert bob.evaluate("document.querySelectorAll('#showStage audio').length") == 0
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


MAP_URL = "https://www.google.com/maps/place/Space+Needle/@47.6205,-122.3493,17z"


def _expected_map_provider(page: Page) -> str:
    return "google" if page.evaluate("(window.APP_CONFIG || {}).googleMapsKey") else "osm"


def _in_view(page: Page, lat: float, lng: float) -> bool:
    b = page.evaluate("window.testState.map.bounds")
    return b["s"] <= lat <= b["n"] and b["w"] <= lng <= b["e"]


def test_shown_map_drags_with_one_finger(browser: Browser, playwright) -> None:
    """Google Maps when a key is configured (OpenStreetMap otherwise): one
    finger pans the map, and (Google) Map/Satellite switches."""
    call_url = f"{BACKEND_URL}/?call=e2e-map-{uuid.uuid4().hex[:8]}"
    alice = _join(browser, call_url, "Alice")
    iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
    bob = browser.new_context(**iphone, permissions=["camera", "microphone"]).new_page()
    try:
        bob.goto(call_url)
        provider = _expected_map_provider(bob)
        bob.fill("#nameInput", "Bob")
        bob.click("#joinCallBtn")
        bob.wait_for_function("window.testState.connected === true", timeout=30_000)
        alice.wait_for_function("window.testState.room.remoteParticipants.size === 1", timeout=20_000)
        _send_from_chat(alice, f"/show {MAP_URL}")
        bob.wait_for_function(
            f"window.testState.map && window.testState.map.provider === '{provider}'"
            " && window.testState.map.bounds",
            timeout=30_000,
        )
        label_sel = ".gm-style-iw" if provider == "google" else ".leaflet-tooltip"
        bob.wait_for_selector(f"#showStage {label_sel}", timeout=15_000)
        assert "Space Needle" in bob.inner_text(f"#showStage {label_sel}")
        start = bob.evaluate("window.testState.map")
        assert abs(start["lat"] - 47.6205) < 1e-3 and abs(start["lng"] + 122.3493) < 1e-3

        box = bob.locator("#showStage .show-map").bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        cdp = bob.context.new_cdp_session(bob)
        _touch(cdp, "touchStart", [(x, y)])  # ONE finger
        for step in range(1, 11):
            _touch(cdp, "touchMove", [(x - step * 15, y - step * 10)])
        _touch(cdp, "touchEnd", [])
        bob.wait_for_function(
            f"Math.abs(window.testState.map.lng - ({start['lng']})) > 1e-4", timeout=8_000
        )
        moved = bob.evaluate("window.testState.map")
        assert moved["lng"] > start["lng"] and moved["lat"] < start["lat"]  # dragged up-left
        assert moved["zoom"] == start["zoom"]

        if provider == "google":
            bob.get_by_role("menuitemradio", name="Satellite").or_(
                bob.locator("#showStage button", has_text="Satellite")
            ).first.click()
            bob.wait_for_function("window.testState.map.mapType === 'satellite'", timeout=8_000)

        bob.click("#showCloseBtn")
        assert bob.locator("#showStage .show-map").count() == 0
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()

def _map_receiver(browser: Browser, playwright, call_url: str, **ctx) -> Page:
    iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
    page = browser.new_context(**iphone, **ctx).new_page()
    page.goto(call_url)
    page.fill("#nameInput", "Bob")
    page.click("#joinCallBtn")
    page.wait_for_function("window.testState.connected === true", timeout=30_000)
    return page


def test_map_shows_my_location_blue_dot(browser: Browser, playwright) -> None:
    call_url = f"{BACKEND_URL}/?call=e2e-geo-{uuid.uuid4().hex[:8]}"
    alice = _join(browser, call_url, "Alice")
    here = {"latitude": 47.6150, "longitude": -122.3400}  # ~1 km from the Space Needle
    bob = _map_receiver(
        browser, playwright, call_url,
        permissions=["camera", "microphone", "geolocation"], geolocation=here,
    )
    try:
        alice.wait_for_function("window.testState.room.remoteParticipants.size === 1", timeout=20_000)
        _send_from_chat(alice, f"/show {MAP_URL}")
        bob.wait_for_function(
            "window.testState.map && window.testState.map.interactive && window.testState.map.bounds",
            timeout=30_000,
        )
        # Permission already granted: the dot appears without tapping anything.
        bob.wait_for_function("window.testState.myLocation", timeout=10_000)
        me = bob.evaluate("window.testState.myLocation")
        assert abs(me["lat"] - here["latitude"]) < 1e-4 and abs(me["lng"] - here["longitude"]) < 1e-4
        assert not _in_view(bob, here["latitude"], here["longitude"])  # off-screen at first

        # Tap the locate button: the view frames both you and the shared place.
        bob.click("#mapLocateBtn")
        bob.wait_for_function(
            f"(() => {{ const b = window.testState.map.bounds; return b && b.s <= {here['latitude']}"
            f" && {here['latitude']} <= b.n && b.w <= {here['longitude']} && {here['longitude']} <= b.e; }})()",
            timeout=8_000,
        )
        assert _in_view(bob, 47.6205, -122.3493)
        assert bob.locator("#mapLocateBtn.active").count() == 1

        # Moving updates the dot.
        bob.context.set_geolocation({"latitude": 47.6100, "longitude": -122.3420})
        bob.wait_for_function("Math.abs(window.testState.myLocation.lat - 47.6100) < 1e-4", timeout=10_000)
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()

def test_map_location_not_requested_without_tap_and_handles_denial(browser: Browser, playwright) -> None:
    call_url = f"{BACKEND_URL}/?call=e2e-nogeo-{uuid.uuid4().hex[:8]}"
    alice = _join(browser, call_url, "Alice")
    bob = _map_receiver(browser, playwright, call_url, permissions=["camera", "microphone"])
    try:
        alice.wait_for_function("window.testState.room.remoteParticipants.size === 1", timeout=20_000)
        _send_from_chat(alice, f"/show {MAP_URL}")
        bob.wait_for_function("window.testState.map && window.testState.map.interactive", timeout=30_000)
        bob.wait_for_selector("#mapLocateBtn", timeout=10_000)
        bob.wait_for_timeout(1500)
        assert bob.evaluate("window.testState.myLocation || null") is None  # no auto prompt/dot
        bob.click("#mapLocateBtn")  # not permitted -> denied
        bob.wait_for_function("window.testState.myLocationError === 'denied'", timeout=10_000)
        assert bob.get_attribute("#mapLocateBtn", "title") == "Location permission denied"
        assert bob.evaluate("window.testState.myLocation || null") is None
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()

def test_desktop_side_panel_width_is_adjustable(browser: Browser) -> None:
    code = f"e2e-panel-{uuid.uuid4().hex[:8]}"
    page = browser.new_context(
        permissions=["camera", "microphone"], viewport={"width": 1400, "height": 800}
    ).new_page()
    try:
        page.goto(f"{BACKEND_URL}/?call={code}")
        page.fill("#nameInput", "Alice")
        page.click("#joinCallBtn")
        page.wait_for_function("window.testState.connected === true", timeout=30_000)
        page.click("#chatBtn")
        panel_w = "document.getElementById('peoplePanel').getBoundingClientRect().width"
        grid_w = "document.getElementById('tileGrid').getBoundingClientRect().width"
        w0, g0 = page.evaluate(panel_w), page.evaluate(grid_w)
        assert abs(w0 - 300) < 2

        h = page.locator("#panelResizer").bounding_box()
        x, y = h["x"] + h["width"] / 2, h["y"] + h["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x - 100, y, steps=5)
        page.mouse.move(x - 200, y, steps=5)
        page.mouse.up()
        w1, g1 = page.evaluate(panel_w), page.evaluate(grid_w)
        assert abs(w1 - 500) < 3, w1  # dragged 200 px left -> 200 px wider
        assert abs((g0 - g1) - 200) < 4, (g0, g1)  # the video area gave up the space

        # Clamped: can't exceed 60% of the window or go below 260 px.
        page.mouse.move(x - 200, y)
        page.mouse.down()
        page.mouse.move(x - 1200, y, steps=8)
        page.mouse.up()
        assert page.evaluate(panel_w) <= 0.6 * 1400 + 1
        page.focus("#panelResizer")
        for _ in range(40):
            page.keyboard.press("ArrowRight")
        assert abs(page.evaluate(panel_w) - 260) < 2

        # Remembered across a reload.
        page.keyboard.press("Shift+ArrowLeft")
        saved = page.evaluate(panel_w)
        page.reload()
        page.fill("#nameInput", "Alice")
        page.click("#joinCallBtn")
        page.wait_for_function("window.testState.connected === true", timeout=30_000)
        page.click("#chatBtn")
        assert abs(page.evaluate(panel_w) - saved) < 2

        page.dblclick("#panelResizer")  # reset
        assert abs(page.evaluate(panel_w) - 300) < 2
    finally:
        page.evaluate("localStorage.removeItem('panelWidth')")
        page.evaluate("window.testActions.hangUp()")
        page.context.close()


def test_phone_side_panel_has_no_resize_handle(browser: Browser, playwright) -> None:
    iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
    page = browser.new_context(**iphone, permissions=["camera", "microphone"]).new_page()
    try:
        page.goto(f"{BACKEND_URL}/?call=e2e-panelph-{uuid.uuid4().hex[:8]}")
        page.fill("#nameInput", "Phone")
        page.click("#joinCallBtn")
        page.wait_for_function("window.testState.connected === true", timeout=30_000)
        page.click("#chatBtn")
        assert page.locator("#panelResizer").is_hidden()
        w = page.evaluate("document.getElementById('peoplePanel').getBoundingClientRect().width")
        assert abs(w - page.evaluate("innerWidth")) < 2  # full width
    finally:
        page.evaluate("window.testActions.hangUp()")
        page.context.close()


def test_phone_camera_request_lets_browser_handle_orientation(browser: Browser, playwright) -> None:
    """Regression: phones must request the standard landscape size; mobile
    browsers rotate it to the device orientation themselves. Requesting
    720x1280 when upright produced landscape video on real phones (reversed)."""
    iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
    ctx = browser.new_context(**iphone, permissions=["camera", "microphone"])
    ctx.add_init_script(
        """(() => { const orig = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
            window.__videoConstraints = [];
            navigator.mediaDevices.getUserMedia = (c) => {
              if (c && c.video && typeof c.video === 'object') window.__videoConstraints.push(c.video);
              return orig(c); }; })()"""
    )
    page = ctx.new_page()
    try:
        page.goto(f"{BACKEND_URL}/?call=e2e-orient-{uuid.uuid4().hex[:8]}")
        page.fill("#nameInput", "Phone")
        page.click("#joinCallBtn")
        page.wait_for_function("window.testState.connected === true", timeout=30_000)
        page.wait_for_function("window.__videoConstraints.some(v => v.width || v.height)")
        sized = page.evaluate("window.__videoConstraints.filter(v => v.width || v.height)")
        for v in sized:
            w = v["width"]["ideal"] if isinstance(v["width"], dict) else v["width"]
            h = v["height"]["ideal"] if isinstance(v["height"], dict) else v["height"]
            assert w >= h, v  # never a swapped portrait request
    finally:
        page.evaluate("window.testActions.hangUp()")
        ctx.close()


def _with_maps_key(page: Page, key: str) -> None:
    """Serve the meeting page with a given Google Maps key injected."""

    def handle(route):
        resp = route.fetch()
        body = re.sub(r'"googleMapsKey": (null|"[^"]*")', f'"googleMapsKey": "{key}"', resp.text())
        route.fulfill(response=resp, body=body)

    page.route("**/?call=*", handle)


def test_google_maps_rejected_key_falls_back_to_openstreetmap(browser: Browser, playwright) -> None:
    """With a key configured, maps use Google; if Google rejects the key the
    viewer switches to OpenStreetMap instead of showing an error."""
    call_url = f"{BACKEND_URL}/?call=e2e-gkey-{uuid.uuid4().hex[:8]}"
    alice = _join(browser, call_url, "Alice")
    iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
    bob = browser.new_context(**iphone, permissions=["camera", "microphone"]).new_page()
    _with_maps_key(bob, "AIzaSyInvalidKeyForE2ETestOnly000000000")
    try:
        bob.goto(call_url)
        assert bob.evaluate("window.APP_CONFIG.googleMapsKey").startswith("AIza")
        bob.fill("#nameInput", "Bob")
        bob.click("#joinCallBtn")
        bob.wait_for_function("window.testState.connected === true", timeout=30_000)
        alice.wait_for_function("window.testState.room.remoteParticipants.size === 1", timeout=20_000)
        _send_from_chat(alice, f"/show {MAP_URL}")
        bob.wait_for_function("window.testState.googleMapsAuthFailed === true", timeout=30_000)
        bob.wait_for_function(
            "window.testState.map && window.testState.map.provider === 'osm'", timeout=20_000
        )
        assert bob.locator("#showStage .leaflet-container").is_visible()
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def _click_frame_point(page: Page, tile_name: str, fx: float, fy: float) -> None:
    """Click the point (fx, fy) of the displayed video frame in a tile."""
    pt = page.evaluate(
        """([name, fx, fy]) => { const t = [...document.querySelectorAll('.tile')]
            .find(el => el.querySelector('.nm').textContent.startsWith(name));
            const v = t.querySelector('video'); const b = v.getBoundingClientRect();
            const vw = v.videoWidth, vh = v.videoHeight;
            const s = Math.max(b.width / vw, b.height / vh);
            const w = vw * s, h = vh * s;
            return { x: b.left + (b.width - w) / 2 + fx * w, y: b.top + (b.height - h) / 2 + fy * h }; }""",
        [tile_name, fx, fy],
    )
    page.mouse.click(pt["x"], pt["y"])


def test_click_on_video_blinks_same_spot_on_their_camera(browser: Browser, playwright) -> None:
    call_url = f"{BACKEND_URL}/?call=e2e-ptr-{uuid.uuid4().hex[:8]}"
    alice = _join(browser, call_url, "Alice")
    iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
    bob = browser.new_context(**iphone, permissions=["camera", "microphone"]).new_page()
    try:
        bob.goto(call_url)
        bob.fill("#nameInput", "Bob")
        bob.click("#joinCallBtn")
        bob.wait_for_function("window.testState.connected === true", timeout=30_000)
        alice.wait_for_function(
            "[...window.testState.room.remoteParticipants.values()].some(p =>"
            " [...p.videoTrackPublications.values()].some(t => t.track))", timeout=20_000
        )
        alice.wait_for_timeout(800)
        # Alice points at 30% across, 60% down of Bob's (big) video.
        _click_frame_point(alice, "Bob", 0.30, 0.60)
        alice.wait_for_function("window.testState.pointersSent === 1")
        mine = alice.evaluate("window.testState.lastPointer")
        assert abs(mine["x"] - 0.30) < 0.02 and abs(mine["y"] - 0.60) < 0.02
        assert mine["tile"].startswith("guest-bob")

        # Bob sees the ring on HIS camera view (his small self view), mirrored.
        bob.wait_for_function("window.testState.pointersReceived === 1", timeout=10_000)
        got = bob.evaluate("window.testState.lastPointer")
        assert got["tile"].startswith("guest-bob")
        assert abs(got["x"] - 0.30) < 0.02 and abs(got["y"] - 0.60) < 0.02
        geom = bob.evaluate(
            """(() => { const t = document.querySelector('.tile.self-pip'); const v = t.querySelector('video');
                return { w: v.clientWidth, h: v.clientHeight, mirror: t.classList.contains('mirror') }; })()"""
        )
        expect_px = (1 - 0.30 if geom["mirror"] else 0.30) * geom["w"]
        assert abs(got["px"] - expect_px) < 0.05 * geom["w"], (got, geom)
        assert abs(got["py"] - 0.60 * geom["h"]) < 0.05 * geom["h"], (got, geom)
        assert bob.locator(".tile.self-pip .pointer-ring").count() == 1

        # Colour-coded by who pointed: same palette colours on every screen,
        # distinct per person; Bob's small window flashes in Alice's colour.
        colors_a = alice.evaluate("window.testState.participantColors")
        colors_b = bob.evaluate("window.testState.participantColors")
        assert colors_a == colors_b and len(set(colors_a.values())) == 2, (colors_a, colors_b)
        alice_color = next(c for i, c in colors_a.items() if i.startswith("guest-alice"))
        assert mine["color"] == alice_color and got["color"] == alice_color
        # The border flash marks the SOURCE (Alice's video), not the target.
        state = """(() => { const tiles = [...document.querySelectorAll('.tile')];
            const byName = n => tiles.find(t => t.querySelector('.nm').textContent.startsWith(n));
            const ring = document.querySelector('.pointer-ring');
            const f = t => ({ flashing: t.classList.contains('pointer-flash'),
                              color: t.style.getPropertyValue('--flash-color'),
                              pulses: getComputedStyle(t).animationIterationCount });
            return { alice: f(byName('Alice')), bob: f(byName('Bob')),
                     ringPulses: ring && getComputedStyle(ring).animationIterationCount }; })()"""
        on_bob = bob.evaluate(state)
        assert on_bob["alice"] == {"flashing": True, "color": alice_color, "pulses": "2"}, on_bob
        assert on_bob["bob"]["flashing"] is False, on_bob  # target's own view: ring only
        assert on_bob["ringPulses"] == "2", on_bob
        on_alice = alice.evaluate(state)  # her own (small) self view flashes on her screen
        assert on_alice["alice"]["flashing"] and on_alice["alice"]["color"] == alice_color, on_alice
        assert on_alice["bob"]["flashing"] is False, on_alice
        # The ring scales with each screen's view: small window -> smaller ring.
        assert got["size"] < mine["size"], (got, mine)
        assert "Alice pointed at your camera" in bob.inner_text("#status")
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def test_switch_camera_cycles_devices_when_self_view_is_big(browser_type) -> None:
    b = browser_type.launch(
        args=[
            "--use-fake-device-for-media-stream=device-count=2",
            "--use-fake-ui-for-media-stream",
        ]
    )
    call_url = f"{BACKEND_URL}/?call=e2e-cyc-{uuid.uuid4().hex[:8]}"
    try:
        alice = _join(b, call_url, "Alice")
        bob = _join(b, call_url, "Bob")
        alice.wait_for_function("window.testState.selfPip === true", timeout=20_000)
        assert alice.evaluate("window.testState.camCycleVisible || false") is False  # small view
        alice.locator(".tile.self-pip").click()  # make my camera the big view
        alice.wait_for_function("window.testState.camCycleVisible === true", timeout=10_000)
        before = alice.evaluate("window.testState.room.getActiveDevice('videoinput')")
        alice.click("#camCycleBtn")
        alice.wait_for_function(
            f"window.testState.cameraDeviceId && window.testState.cameraDeviceId !== {before!r}",
            timeout=10_000,
        )
        assert alice.evaluate("window.testState.room.getActiveDevice('videoinput')") != before
        assert alice.evaluate("window.testState.room.localParticipant.isCameraEnabled")
        alice.click("#camCycleBtn")  # cycles back around
        alice.wait_for_function(
            f"window.testState.room.getActiveDevice('videoinput') === {before!r}", timeout=10_000
        )
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
    finally:
        b.close()


def test_phone_big_view_fills_stage_edge_to_edge(browser: Browser, playwright) -> None:
    """Phone: the single big view has no frame and runs from the top edge down
    to the control bar; desktop keeps its framed tile."""
    call_url = f"{BACKEND_URL}/?call=e2e-edge-{uuid.uuid4().hex[:8]}"
    desk = _join(browser, call_url, "Desk")
    iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
    phone = browser.new_context(**iphone, permissions=["camera", "microphone"]).new_page()
    big = """(() => { const t = document.querySelector('.tile:not(.self-pip)');
        const r = t.getBoundingClientRect(); const bar = document.getElementById('controlBar').getBoundingClientRect();
        const cs = getComputedStyle(t); const v = getComputedStyle(t.querySelector('video'));
        return { top: r.top, left: r.left, right: r.right, bottom: r.bottom, barTop: bar.top,
                 w: innerWidth, border: cs.borderTopWidth, radius: cs.borderTopLeftRadius,
                 fit: v.objectFit, bg: cs.backgroundColor,
                 name: t.querySelector('.nm').textContent }; })()"""
    try:
        phone.goto(call_url)
        phone.fill("#nameInput", "Phone")
        phone.click("#joinCallBtn")
        phone.wait_for_function("window.testState.connected === true", timeout=30_000)
        phone.wait_for_function("window.testState.selfPip === true", timeout=20_000)
        for label in ("other caller big", "my camera big"):
            g = phone.evaluate(big)
            assert g["top"] <= 1 and g["left"] <= 1 and abs(g["right"] - g["w"]) <= 1, (label, g)
            assert abs(g["bottom"] - g["barTop"]) <= 2, (label, g)  # down to the toolbar, not over it
            assert g["border"] == "0px" and g["radius"] == "0px", (label, g)
            # Landscape fake camera on a portrait phone: whole picture on black
            # (cropping would cut off ~60% of it), never a grey frame.
            assert g["fit"] == "contain" and g["bg"] == "rgb(0, 0, 0)", (label, g)
            if label == "other caller big":
                assert g["name"].startswith("Desk")
                phone.locator(".tile.self-pip").click()  # swap: my camera big
                phone.wait_for_function("window.testState.stageFocus === 'self'")
            else:
                assert g["name"].startswith("Phone")
        # Desktop keeps the framed, rounded tile.
        d = desk.evaluate(big)
        assert d["radius"] != "0px" and d["border"] != "0px", d
    finally:
        for page in (desk, phone):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()



def test_phone_big_view_crops_to_fill_when_shapes_match(browser_type, playwright, portrait_y4m: Path) -> None:
    """A portrait camera on a portrait phone fills the screen (crop-to-fill)."""
    b = browser_type.launch(args=[
        "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
        f"--use-file-for-fake-video-capture={portrait_y4m}",
    ])
    try:
        iphone = {k: v for k, v in playwright.devices["iPhone 13"].items() if k != "default_browser_type"}
        phone = b.new_context(**iphone, permissions=["camera", "microphone"]).new_page()
        phone.goto(f"{BACKEND_URL}/?call=e2e-fill-{uuid.uuid4().hex[:8]}")
        phone.fill("#nameInput", "Phone")
        phone.click("#joinCallBtn")
        phone.wait_for_function("window.testState.connected === true", timeout=30_000)
        # Alone in the call: my (portrait) camera is the single big view.
        phone.wait_for_function(
            "document.querySelector('#stageRow.solo .tile.fit-cover') !== null", timeout=15_000
        )
        fit = phone.evaluate(
            "getComputedStyle(document.querySelector('.tile:not(.self-pip) video')).objectFit"
        )
        assert fit == "cover"
        phone.evaluate("window.testActions.hangUp()")
    finally:
        b.close()


def _gcs_call_events(call_id: str) -> list[dict]:
    import base64
    import json

    from dotenv import load_dotenv
    from google.cloud import storage

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "backend", ".env"))
    creds = json.loads(base64.b64decode(os.environ["GCP_CREDENTIALS_B64"]))
    client = storage.Client.from_service_account_info(creds)
    out = []
    for b in client.list_blobs(os.environ["GCS_BUCKET"], prefix=f"calls/{call_id}/"):
        if b.name.endswith("_events.jsonl"):
            out += [json.loads(line) for line in b.download_as_text().splitlines() if line]
    return out


def test_interactions_are_stored_with_the_recordings(browser: Browser) -> None:
    """Chat, pointer clicks, controls and server events land as timestamped
    JSON lines beside the call's recordings."""
    import time

    if not os.environ.get("GCP_CREDENTIALS_B64"):
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "backend", ".env"))
    if not os.environ.get("GCP_CREDENTIALS_B64"):
        pytest.skip("GCS credentials not available")
    code = f"e2e-log-{uuid.uuid4().hex[:8]}"
    call_url = f"{BACKEND_URL}/?call={code}"
    alice, bob = _join(browser, call_url, "Alice"), _join(browser, call_url, "Bob")
    try:
        for page in (alice, bob):
            page.wait_for_function(
                "[...window.testState.room.remoteParticipants.values()].some(p =>"
                " [...p.videoTrackPublications.values()].some(t => t.track))", timeout=20_000
            )
        alice.click("#recBtn")
        alice.wait_for_function("window.testState.recording === true", timeout=15_000)
        _send_from_chat(alice, "please check the left cable")
        alice.wait_for_timeout(500)
        _click_frame_point(alice, "Bob", 0.25, 0.75)
        alice.wait_for_function("window.testState.pointersSent === 1")
        alice.click("#muteAllBtn")
        bob.evaluate("window.testActions.sendChat('found it')")
        bob.wait_for_timeout(2500)  # let Bob's batch flush
        alice.click("#muteAllBtn")
        alice.wait_for_timeout(2500)
        alice.click("#recBtn")  # stop -> immediate export beside the recordings
        alice.wait_for_function("window.testState.recording === false", timeout=15_000)

        deadline = time.monotonic() + 30
        events: list[dict] = []
        while time.monotonic() < deadline:
            events = _gcs_call_events(code)
            if any(e["type"] == "recording_stop" for e in events):
                break
            time.sleep(2)
        types = [e["type"] for e in events]
        for needed in ("join", "recording_start", "chat", "pointer", "mute", "recording_stop"):
            assert needed in types, types
        chats = [(e["name"], e["data"]["text"]) for e in events if e["type"] == "chat"]
        assert ("Alice", "please check the left cable") in chats and ("Bob", "found it") in chats
        ptr = next(e for e in events if e["type"] == "pointer")
        assert ptr["name"] == "Alice" and ptr["data"]["target"].startswith("guest-bob")
        assert abs(ptr["data"]["x"] - 0.25) < 0.02 and abs(ptr["data"]["y"] - 0.75) < 0.02
        assert [e["data"]["muted"] for e in events if e["type"] == "mute"] == [True, False]
        # Every event has a server-clock time (aligns with the recordings) and
        # client events also carry the sender's raw clock; file is time-ordered.
        assert all(e["ts"] and e["received_ts"] for e in events)
        assert all(e["client_ts"] for e in events if e["type"] in ("chat", "pointer", "mute"))
        assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
        # Real order of what happened: recording started before the chat, and
        # Alice's chat preceded her pointer click and mute.
        order = [e["type"] for e in events if e["type"] in ("recording_start", "chat", "pointer")]
        assert order.index("recording_start") < order.index("chat") < order.index("pointer"), order

        # The viewer lists the interaction log for download.
        alice.evaluate("showRecordings()")
        alice.wait_for_function("(window.testState.eventLogs || []).length >= 1", timeout=15_000)
        assert alice.locator("#recList .rec-log").count() >= 1
    finally:
        for page in (alice, bob):
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


_FPS = """(async () => { const out = {};
    for (const p of window.testState.room.remoteParticipants.values()) {
      const pub = [...p.trackPublications.values()].find(t => t.kind === 'video' && t.track);
      if (!pub || !pub.track.receiver) continue;
      const stats = await pub.track.receiver.getStats();
      stats.forEach(s => { if (s.type === 'inbound-rtp') out[p.name] = s.framesPerSecond || 0; });
    }
    return out; })()"""

_ACTIVE_LAYERS = """(() => { const lp = window.testState.room.localParticipant;
    const pub = [...lp.trackPublications.values()].find(t => t.kind === 'video' && t.track);
    const sender = pub && pub.track.sender;
    return sender ? sender.getParameters().encodings.filter(e => e.active).map(e => e.rid) : null; })()"""


def _wait_fps(page: Page, check, timeout_s: float = 30) -> dict:
    deadline = time.time() + timeout_s
    fps: dict = {}
    while time.time() < deadline:
        fps = page.evaluate(_FPS)
        if check(fps):
            return fps
        page.wait_for_timeout(1000)
    raise AssertionError(f"frame rates never matched: {fps}")


def test_click_maximizes_video_and_small_videos_use_quarter_frame_rate(browser: Browser) -> None:
    code = f"e2e-focus-{uuid.uuid4().hex[:8]}"
    call_url = f"{BACKEND_URL}/?call={code}"
    pages = [_join(browser, call_url, n) for n in ("Alice", "Bob", "Carol")]
    alice, bob, carol = pages
    big = "[...document.querySelectorAll('.tile:not(.self-pip) .nm')].map(e => e.textContent)"
    try:
        for page in pages:
            page.wait_for_function("window.testState.participants.length === 3", timeout=30_000)
        # Default with 3 people: the two others tiled big, yourself small.
        alice.wait_for_function("window.testState.bigCount === 2 && window.testState.selfPip")
        assert alice.locator("#showAllBtn").is_hidden()
        _wait_fps(alice, lambda f: f.get("Bob", 0) >= 14 and f.get("Carol", 0) >= 14)

        # Click Carol's tile: she fills the stage, Bob and Alice float.
        alice.locator(".tile", has_text="Carol").click()
        alice.wait_for_function("window.testState.focusIdentity && window.testState.bigCount === 1")
        assert alice.evaluate(big) == ["Carol"]
        assert alice.locator(".tile.self-pip").count() == 2
        assert alice.locator("#showAllBtn").is_visible()
        requested = alice.evaluate(
            """(() => { const id = n => [...window.testState.room.remoteParticipants.values()]
                .find(p => p.name === n).identity; const r = window.testState.requestedFps;
                return { bob: r[id('Bob')], carol: r[id('Carol')] }; })()"""
        )
        assert requested == {"bob": 7, "carol": 60}, requested
        # Bob is small on Alice's screen: received at ~1/4 frame rate.
        _wait_fps(alice, lambda f: 0 < f.get("Bob", 0) <= 10 and f.get("Carol", 0) >= 14)

        # Clicking a small window maximizes it instead.
        alice.locator(".tile.self-pip", has_text="Bob").click()
        alice.wait_for_function(f"JSON.stringify({big}) === '[\"Bob\"]'")
        _wait_fps(alice, lambda f: f.get("Bob", 0) >= 14 and 0 < f.get("Carol", 0) <= 10)

        # Nobody shows Bob big: Bob's camera stops sending its larger layers.
        alice.locator(".tile.self-pip", has_text="Carol").click()
        carol.locator(".tile", has_text="Alice").click()
        carol.wait_for_function(f"JSON.stringify({big}) === '[\"Alice\"]'")
        bob.wait_for_function(f"JSON.stringify({_ACTIVE_LAYERS}) === '[\"q\"]'", timeout=30_000)

        # Show everyone: back to the tiled view; Bob's layers resume.
        alice.locator("#showAllBtn").click()
        alice.wait_for_function("window.testState.bigCount === 2 && !window.testState.focusIdentity")
        assert alice.locator("#showAllBtn").is_hidden()
        bob.wait_for_function(f"{_ACTIVE_LAYERS}.length > 1", timeout=30_000)
        _wait_fps(alice, lambda f: f.get("Bob", 0) >= 14)
    finally:
        for page in pages:
            page.evaluate("window.testActions.hangUp()")
            page.context.close()


def test_lobby_layout_order(browser: Browser) -> None:
    page = browser.new_context(permissions=["camera", "microphone"]).new_page()
    try:
        page.goto(f"{BACKEND_URL}/?call=e2e-lobby-{uuid.uuid4().hex[:6]}")
        page.wait_for_selector("#prejoin:not([hidden])")
        page.wait_for_function("(window.testState.deviceKinds || []).includes('videoinput')", timeout=15_000)
        tops = page.evaluate(
            """(() => { const y = s => document.querySelector(s).getBoundingClientRect().top;
            const sel = k => `#prejoinDevices select[data-kind="${k}"]`;
            return { title: y('#prejoinTitle'), preview: y('#prejoinPreview'), nameLabel: y('#nameField'),
                     name: y('#nameInput'), code: y('#callCodeInput'),
                     join: y('#joinCallBtn'), cam: y(sel('videoinput')), mic: y(sel('audioinput')),
                     kinds: window.testState.deviceKinds,
                     spk: document.querySelector(sel('audiooutput')) ? y(sel('audiooutput')) : null }; })()"""
        )
        order = ["title", "preview", "nameLabel", "name", "code", "join", "cam", "mic"]
        assert [tops[k] for k in order] == sorted(tops[k] for k in order), tops
        assert page.inner_text("#nameField") == "Name"
        # No "anyone can join / record" lines; the recording warning only shows
        # while a recording is running.
        assert page.locator("#linkNotice").count() == 0
        assert page.locator("#recNotice").is_hidden()
        assert "Anyone" not in page.inner_text("#prejoin")
        assert tops["kinds"][0] == "videoinput", tops
        if tops["spk"] is not None:  # microphone and speaker share a row
            assert abs(tops["spk"] - tops["mic"]) < 2, tops
        # "View recordings", when offered, sits between Join and the devices.
        page.evaluate("document.getElementById('viewRecsBtn').hidden = false")
        recs = page.evaluate("document.getElementById('viewRecsBtn').getBoundingClientRect().top")
        cam = page.evaluate(
            "document.querySelector('#prejoinDevices select[data-kind=\"videoinput\"]').getBoundingClientRect().top"
        )
        assert tops["join"] < recs < cam
    finally:
        page.context.close()


_TORCH_STUB = """(() => {
  // Fake cameras have no flashlight: pretend this one does, and record it.
  const caps = MediaStreamTrack.prototype.getCapabilities;
  MediaStreamTrack.prototype.getCapabilities = function () {
    const c = caps ? caps.call(this) : {};
    return this.kind === 'video' ? { ...c, torch: true } : c;
  };
  const apply = MediaStreamTrack.prototype.applyConstraints;
  MediaStreamTrack.prototype.applyConstraints = function (c) {
    const adv = c && c.advanced && c.advanced.find((a) => 'torch' in a);
    if (!adv) return apply.call(this, c);
    window.__torch = adv.torch;
    const rest = { ...c };
    delete rest.advanced;
    return Object.keys(rest).length ? apply.call(this, rest) : Promise.resolve();
  };
})();"""


@pytest.mark.parametrize("device", ["Pixel 7", "iPhone 13"])
def test_remote_phone_camera_switch_photo_and_flashlight(browser_type, playwright, device: str) -> None:
    """Pixel: photo via ImageCapture.takePhoto. iPhone (no ImageCapture, as on
    iOS Safari): the camera is raised to full size and a frame grabbed."""
    b = browser_type.launch(
        args=["--use-fake-device-for-media-stream=device-count=2", "--use-fake-ui-for-media-stream"]
    )
    call_url = f"{BACKEND_URL}/?call=e2e-rcam-{uuid.uuid4().hex[:8]}"
    try:
        desk = _join(b, call_url, "Desk")
        ctx = b.new_context(
            **{k: v for k, v in playwright.devices[device].items() if k != "default_browser_type"},
            permissions=["camera", "microphone"],
        )
        ctx.add_init_script(_TORCH_STUB)
        if device.startswith("iPhone"):
            ctx.add_init_script("delete window.ImageCapture;")
        phone = ctx.new_page()
        phone.goto(call_url)
        phone.wait_for_selector("#prejoin:not([hidden])")
        phone.fill("#nameInput", "Phone")
        phone.click("#joinCallBtn")
        phone.wait_for_function("window.testState.connected === true", timeout=30_000)
        assert phone.evaluate("window.testState.isPhone") is True
        assert desk.evaluate("window.testState.isPhone") is False

        # The desktop sees the phone's controls on its big view (all three).
        bar = desk.locator(".tile", has_text="Phone").locator(".remote-cam")
        bar.wait_for(state="visible", timeout=20_000)
        for op in ("cycle", "photo", "torch"):
            assert bar.locator(f".rc-{op}").is_visible(), op
        # The desktop isn't a phone: no controls for it on the phone.
        assert phone.locator(".remote-cam:not([hidden])").count() == 0

        # Flashlight on / off.
        bar.locator(".rc-torch").click()
        phone.wait_for_function("window.__torch === true", timeout=10_000)
        desk.wait_for_function(
            "document.querySelector('.remote-cam .rc-torch').getAttribute('aria-pressed') === 'true'"
        )
        bar.locator(".rc-torch").click()
        phone.wait_for_function("window.__torch === false", timeout=10_000)

        # Switch the phone's camera.
        before = phone.evaluate("window.testState.room.getActiveDevice('videoinput')")
        bar.locator(".rc-cycle").click()
        phone.wait_for_function(
            f"window.testState.room.getActiveDevice('videoinput') !== {before!r}", timeout=15_000
        )

        # Photo: taken on the phone, shown full screen to the requester only.
        settings = phone.evaluate(
            "window.testState.room.localParticipant.getTrackPublication('camera').track"
            ".mediaStreamTrack.getSettings()"
        )
        bar.locator(".rc-photo").click()
        desk.wait_for_selector("#showOverlay:not([hidden]) #showStage img", timeout=60_000)
        desk.wait_for_function(
            "(() => { const i = document.querySelector('#showStage img'); return i && i.complete && i.naturalWidth > 0; })()",
            timeout=30_000,
        )
        assert desk.inner_text("#showTitle") == "Photo from Phone"
        natural = desk.evaluate(
            "(() => { const i = document.querySelector('#showStage img'); return [i.naturalWidth, i.naturalHeight]; })()"
        )
        photo = phone.evaluate("window.testState.lastPhoto")
        print("camera", settings["width"], settings["height"], "photo", natural, photo["type"])
        assert natural == [photo["width"], photo["height"]]
        assert natural[0] * natural[1] >= settings["width"] * settings["height"]
        assert phone.locator("#showOverlay:not([hidden])").count() == 0  # not shown to the phone
        assert {"op": "photo", "ok": True} in desk.evaluate("window.testState.camResults")

        # Saved beside the recordings; the stable link serves the image.
        call_id = call_url.rsplit("=", 1)[1]
        recs = desk.request.get(f"{BACKEND_URL}/v1/calls/{call_id}/recordings").json()
        assert [p["name"] for p in recs["photos"]] == [photo["name"]]
        img = desk.request.get(f"{BACKEND_URL}{photo['path']}")
        assert img.ok and img.headers["content-type"].startswith("image/")
        # And the recordings viewer lists it with a preview.
        desk.click("#showCloseBtn")
        desk.evaluate("showRecordings()")
        desk.wait_for_function(
            "(() => { const i = document.querySelector('#recordings .rec-photo img');"
            " return i && i.complete && i.naturalWidth > 0; })()",
            timeout=20_000,
        )
        # The phone's video keeps going at its normal size afterwards.
        phone.wait_for_function(
            "window.testState.room.localParticipant.getTrackPublication('camera').track"
            f".mediaStreamTrack.getSettings().width === {settings['width']}",
            timeout=10_000,
        )
        for page in (desk, phone):
            page.evaluate("window.testActions.hangUp()")
    finally:
        b.close()
