# 12 — User Guide

How to use the RemoteSupportLiveKit system as a caller or support operator.

## Roles

- **Caller** — the person who needs help. No account required.
- **Support** — the operator helping them. Must sign in with a Google account
  allowed by the backend allowlist.
- **Agent** — an AI voice participant that listens to both humans but only ever
  speaks to the caller. Support controls whether it speaks.

## For the caller

### Starting a call

1. Open the app and choose **"I need help"**.
2. Enter your display name (optional) and tap **Start**.
3. Read the consent screen carefully. The call is recorded and transcribed.
   - Tap **Agree** to continue.
   - Tap **Decline** to end the session.
4. You land in the **Waiting Room**. You see your own camera preview and a
   6-character join code (for example `K7R2XM`) plus a QR code.
5. Share the code or QR with support. You can also tap the share button to send
   the universal link via Messages/email.

### During the call

- Your video is in a small picture-in-picture window; support's video fills the
  screen.
- Use the bottom controls to mute your mic, turn your camera off, switch
  cameras, or hang up.
- The AI agent may greet you while you wait and answer questions. Once support
  joins, the agent goes silent by default unless support turns it back on.
- A "REC" indicator appears when the call is being recorded.
- Live captions appear at the bottom of the screen.

### Ending the call

Tap the red hang-up button. You briefly see a summary screen, then the session
ends and the recording stops.

## For support

### Signing in

1. Open the app and choose **"I'm support"**.
2. Sign in with your Google account.
3. If your domain or email is not in the backend allowlist, sign-in is rejected.
   Contact an admin to be added.

### Joining a call

You can join in three ways:

1. **Type the code** — enter the 6 characters the caller gives you. The keypad
   automatically remaps ambiguous letters (`I`→`1`, `O`→`0`, etc.).
2. **Scan the QR code** — point the scanner at the caller's QR code.
3. **Tap a deep link** — if the caller shares the universal link
   (`https://remotesupport.lgitech.net/j/XXXXXX`), tapping it opens the app
   directly.

If the code is expired, not found, or another support operator is already in the
session, you see a clear error message.

### Joining from a browser (no code needed)

Support operators can also join a live session straight from a web browser —
useful for desk-side testing or when the app isn't handy:

1. Open the short link you were given:
   `https://<backend>/join/<session_id>`.
2. Click **Sign in with Google** and pick your allowlisted account. The
   browser asks for camera and microphone permission — allow both.
3. You're connected with real camera and mic; the caller sees you as support
   and the AI agent follows the usual SOLO/ASSISTED rules.

The in-call browser experience mirrors Google Meet: a tile for everyone in
the call (you, the caller, the AI agent), a bottom bar with **microphone**,
**camera**, **hang up**, and **people**/**chat** toggles that open a side
panel. The People tab lists everyone with their mic state; the Chat tab is a
text chat for everyone in the call (the chat button shows an unread-count
badge while the panel is closed). A blue ring and animated bars mark whoever
is currently speaking; muting turns the button red, and turning the camera
off swaps the tile to an avatar.

The link carries only the session ID — no tokens. Your Google identity is
verified by the backend (allowlist enforced), and the LiveKit credentials are
minted after sign-in, so they never appear in a URL.

If the session ID is unknown, ended, or another operator is already in, the
page shows the same errors as the app ("Support session not found", etc.).

### During the call

- The caller's video fills the screen; your own preview is picture-in-picture.
- The bottom bar has the same mute, camera, switch-camera, speaker, and hang-up
  controls the caller sees.
- **AI toggle** — a switch labeled "Assistant" lets you mute or unmute the AI
  agent's voice. The default is **on** when you join. The caller sees a passive
  indicator reflecting the state you choose.
- **Transcript** — open the transcript panel to see the running diarized
  conversation (caller, support, agent). Scroll up to review earlier parts; tap
  "Jump to live" to return to the latest line.
- **REC indicator** — shows when recording is active.

### After the call

After hanging up, admins can download recordings and exports from the session
summary. Recordings and transcripts are retained according to the configured
lifecycle policy (default 30 days) and can be purged early by an admin.

## Open group calls

When the server has `OPEN_CALLS_ENABLED=true`, anyone can hold a plain group
video call, like a Google Meet link:

1. Open `https://<host>/call` to start a new call, or make up a name such as
   `https://<host>/call/team-standup`.
2. Enter your name and press **Join now**. Use **Copy joining link** to share it.
3. Everyone who opens the same link joins the same room (up to 12 people by default).

If the server has `OPEN_CALL_RECORDING_ENABLED=true`, a record button (circle)
appears in the control bar. **Anyone** in the call can press it to start or stop
recording. Everyone sees a red **REC** badge and a "Recording started by …"
notice, and people joining later are told before they join. While recording,
each person's camera and microphone are saved together as one video file per
person, including people who join mid-recording. If the LiveKit plan's
recording limit is reached, a warning says how many people are not being
recorded.

Before joining, the join screen shows a camera preview and lets you pick your
microphone, camera, and (on desktop browsers that support it) speaker. During
the call, tap the small arrow on the microphone or camera button to switch
devices instantly (the microphone menu also lists speakers), or use the gear
button for all choices at once; on a phone, the Camera list
includes the front and back cameras. The pause button mutes the microphone and
camera together (press again to turn both back on); the mic and camera buttons
still toggle each one separately. Your choices are remembered on that device.

The red hang-up button offers **Leave call** (only you leave; everyone else
stays) and **End call for everyone** (anyone can use it: recording stops and is
saved, and everyone sees "<name> ended the call"). The same link starts the
call again afterwards.

To watch recordings, use **View recordings** on the join screen or in the gear
(settings) menu during the call. Anyone with the call link can view and
download them, so treat the link as private if recordings are sensitive.

Open calls are not transcribed, have no AI agent, and need no sign-in. Anyone
who has the link can join, so use the generated ids for anything private.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Join code not found" | The code was mistyped or the session ended. Ask the caller to create a new session. |
| "Join code has expired" | The code timed out (default 30 minutes). Ask for a fresh one. |
| "Support role is already occupied" | Another operator is already in the call. Only one support participant is allowed per session. |
| Can't hear the other person | Check that the app has microphone permission and that the speaker icon is not muted. |
| Video is black | Check camera permission. On Android, make sure no other app is using the camera. |
| AI agent not responding | Support can toggle the AI switch off and on again. If it still doesn't respond, the agent worker may be reconnecting. |

## Links and deep links

The app responds to:

- **Deep link:** `remotesupport://join?code=K7R2XM`
- **Universal link:** `https://remotesupport.lgitech.net/j/K7R2XM`
- **QR payload:** same as the universal link

Universal links verify the app's identity with Google's Digital Asset Links,
so tapping the link opens the app directly without an extra disambiguation
sheet (on Android).
