# 07 — Flutter App

One app, both roles. Role is decided at runtime, not at build time — support
signs in, callers don't. This keeps a single binary and makes testing a full
call on two devices trivial.

## Dependencies

```yaml
dependencies:
  livekit_client: ^2.11.0        # verify latest at implementation time
  flutter_riverpod: ^2.5.0       # state management
  dio: ^5.4.0                    # REST client
  freezed_annotation: ^2.4.0
  json_annotation: ^4.9.0
  app_links: ^6.3.0              # deep links + universal links
  mobile_scanner: ^5.2.0         # QR scanning (support side)
  qr_flutter: ^4.1.0             # QR rendering (caller side)
  permission_handler: ^11.3.0    # camera/mic/bluetooth
  wakelock_plus: ^1.2.0          # keep screen on during a call
  flutter_secure_storage: ^9.2.0 # support auth token
```

## Screen flow

```
                    ┌──────────────┐
                    │ RoleSelect   │
                    └──┬────────┬──┘
            "I need help"        "I'm support"
                       │        │
          ┌────────────▼──┐  ┌──▼───────────┐
          │ CallerStart   │  │ SupportSignIn│
          │ (name entry)  │  └──────┬───────┘
          └───────┬───────┘         │
                  │           ┌─────▼──────┐
          ┌───────▼───────┐   │ SupportJoin│◄── deep link / QR
          │ ConsentSheet  │   │ code entry │
          │  (blocking)   │   └─────┬──────┘
          └───────┬───────┘         │
                  │                 │
          ┌───────▼───────┐         │
          │ WaitingRoom   │         │
          │ code + QR     │         │
          └───────┬───────┘         │
                  └────────┬────────┘
                           ▼
                    ┌─────────────┐
                    │  CallScreen │
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │ CallSummary │
                    └─────────────┘
```

### `ConsentSheet` — the blocking gate

Shown to the caller immediately after `POST /v1/sessions`, before any LiveKit
connection exists. Non-dismissible. Renders `consent_text` from the server
response (never hardcode it — the server owns the version).

Accept → `POST /consent {accepted:true}` → receive the LiveKit token → connect.
Decline → session ends with an explanatory screen (or proceeds unrecorded if the
server says `recording_enabled:false`).

Because the token is withheld until this resolves, there is no window in which
audio flows unrecorded. That is the whole point of doing it here rather than as
an in-call banner.

### `WaitingRoom`

Caller sees their own camera preview, the 6-char code in a large monospace font
with a tap-to-copy affordance, a QR code encoding the universal link, and a
share-sheet button. The AI agent is already connected and in `SOLO` mode, so the
caller can start describing the problem before support arrives. Show an
"Assistant is listening" indicator so this isn't surprising.

Auto-advances to `CallScreen` on `participantConnected` for a `support-` identity.

### `SupportJoin`

Three entry paths, one resolution:

1. **Type** — 6-char input, auto-uppercase, ambiguous characters (`I L O U`)
   remapped on entry (`I`→`1`, `O`→`0`) rather than rejected. Users will
   transcribe them wrong; be forgiving.
2. **Scan** — `mobile_scanner`, extracts the code from the universal link.
3. **Deep link** — `app_links` stream, handled both at cold start
   (`getInitialLink`) and while running (`uriLinkStream`).

All three call `POST /v1/sessions/join`. Handle `404`, `410`, and `409
role_occupied` with distinct, plain-language messages.

### `CallScreen`

Layout, both roles:

```
┌───────────────────────────────────┐
│ ● REC   Assistant: on    00:04:12 │  ← status bar
│                                   │
│                                   │
│         remote video              │
│         (full bleed)              │
│                       ┌─────────┐ │
│                       │  local  │ │  ← draggable PiP
│                       │ preview │ │
│                       └─────────┘ │
│  ┌─────────────────────────────┐  │
│  │ live caption overlay        │  │  ← last 2 lines
│  └─────────────────────────────┘  │
│                                   │
│   🎤    📷    🔄    🔊    ☎️      │  ← shared controls
│  [ 🤖 AI  ]  [ 📝 Transcript ]    │  ← support only
└───────────────────────────────────┘
```

Shared controls: mute mic, toggle camera, switch camera, speaker/earpiece, hang up.

**Support-only controls:**

- **AI toggle** — a switch, not a button, because it has persistent state. Three
  visual states: `on`, `off`, `pending` (optimistic, awaiting confirmation).
  Sends `POST /v1/sessions/{id}/agent` **and** publishes the
  `rs.agent.control` data message simultaneously. Confirms on the room-metadata
  echo. If no echo in 2 s, revert the switch and show a snackbar.
- **Transcript panel** — a draggable bottom sheet with the running diarized
  transcript, color-coded by speaker, auto-scrolling with a "jump to live"
  button when the user scrolls up.

**Caller-side AI indicator** — passive text/icon only, driven by room metadata.
The caller must never be able to toggle it (FR-4.1 gives that to support alone,
and the API enforces it server-side regardless of what the UI does).

**REC indicator** — visible whenever `recording` is true in room metadata,
satisfying the persistent-indicator requirement.

## State management

Riverpod providers, one `CallController` owning the `Room` object:

```dart
final callControllerProvider =
    StateNotifierProvider<CallController, CallState>((ref) => ...);

@freezed
class CallState with _$CallState {
  const factory CallState({
    required ConnectionStatus status,
    required Role myRole,
    String? sessionId,
    String? joinCode,
    RemoteParticipant? remoteHuman,
    RemoteParticipant? agent,
    @Default(true) bool aiEnabled,
    @Default(AiToggleStatus.idle) AiToggleStatus aiToggleStatus,
    @Default(false) bool recording,
    @Default(AgentMode.solo) AgentMode agentMode,
    @Default([]) List<CaptionLine> captions,
    @Default([]) List<TranscriptEntry> transcript,
    String? error,
  }) = _CallState;
}
```

Room event subscriptions the controller must handle:

| Event | Action |
|---|---|
| `RoomMetadataChangedEvent` | parse JSON, apply `v`-guard, update `aiEnabled` / `recording` / `agentMode`, resolve pending toggle |
| `ParticipantConnectedEvent` | classify by identity prefix; bind remote human or agent |
| `ParticipantDisconnectedEvent` | show "reconnecting" or end the call |
| `TrackSubscribedEvent` | attach renderer |
| `RoomDisconnectedEvent` | route to `CallSummary` with reason |
| `RoomReconnectingEvent` / `RoomReconnectedEvent` | show a connectivity banner |
| text stream `lk.transcription` | append caption / transcript entry |

## Connection setup

```dart
final room = Room(
  roomOptions: const RoomOptions(
    adaptiveStream: true,
    dynacast: false,                      // 1:1 call; nothing to pause
    defaultVideoPublishOptions: VideoPublishOptions(
      videoCodec: 'h264',                 // FR-2.4 — see docs/06
      simulcast: false,
    ),
    defaultAudioCaptureOptions: AudioCaptureOptions(
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    ),
  ),
);
await room.connect(wsUrl, token);
await room.localParticipant?.setCameraEnabled(true);
await room.localParticipant?.setMicrophoneEnabled(true);
```

Token refresh: schedule a timer at `expires_at - 2min` calling
`POST /v1/sessions/{id}/token/refresh`, then `room.setToken(newToken)`. Do not
let a token expire mid-call — LiveKit will disconnect the participant.

## Platform setup

**iOS** (`Info.plist`): `NSCameraUsageDescription`,
`NSMicrophoneUsageDescription`, `NSLocalNetworkUsageDescription`;
`UIBackgroundModes` = `audio`, `voip`. Deployment target ≥ 12.1. Add the
associated-domains entitlement for universal links.

**Android** (`AndroidManifest.xml`): `CAMERA`, `RECORD_AUDIO`, `INTERNET`,
`ACCESS_NETWORK_STATE`, `MODIFY_AUDIO_SETTINGS`, `BLUETOOTH_CONNECT`,
`FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MICROPHONE`. `minSdk` 24. Add an
intent-filter for the `remotesupport` scheme and an App Links verification entry
for `APP_LINK_HOST`. A foreground service is required to keep audio alive when
the app is backgrounded.

Request permissions before connecting, not at app launch — the request makes
more sense to the user in context, and a denial at launch is unrecoverable
without a settings trip.

## Out of scope for v1

CallKit / ConnectionService integration, push-to-call, screen sharing,
picture-in-picture at the OS level, and background call continuation beyond
audio. Note these explicitly so nobody assumes they were forgotten — a real
support product will eventually want CallKit, but it is a substantial platform
project of its own.
