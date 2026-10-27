# Remote Support — mobile app (Flutter, iOS + Android)

Home is the **meeting lobby**: the same open multi-party calls as the web client
at `https://remotesupport.lgitech.net/livekit/?call=<code>`, so phones and
browsers can be in one call. The older two-party support flow (caller / support
agent, docs/07-flutter-app.md) is under **Remote support session** in the lobby.

## Meeting features (parity with the web client)

- **Lobby:** "Ready to join?", camera preview, name, editable meeting code (a new
  code and **Start meeting** when opened without one; pasting a whole meeting link
  keeps just the code), copy-link button, **Join now**, **View recordings**,
  camera, then microphone and speaker side by side. A red notice shows only while
  the call is being recorded.
- **Call:** others fill the screen (tiled when several), your own video floats.
  Tap any small window, or one of several tiled videos, to make it the big view;
  **Show everyone** goes back. Small videos are received at ~¼ frame rate.
  Tap the single big video to **point**: a ring blinks on that spot on every
  screen, with a two-pulse border flash on the pointer's own video in their
  colour (same colour-blind-safe palette and ordering as the web).
- **Controls:** mute (mic + camera), hang up (the last person to leave ends the
  meeting), record, people, chat (unread badge, chime), settings.
- **Panel:** People / Chat / Data (data-channel log: time, source, description,
  size — never the payload). Beside the call on tablets, full screen on phones.
- **Chat `/show <link>`:** checked by the server first; others see it full
  screen — image (pinch / double-tap zoom), audio (play/pause, repeat, timeline),
  video, YouTube, web page, map (Google Maps with one-finger drag, map /
  satellite, my-location dot; OpenStreetMap if no key).
- **Settings:** microphone and camera switches (the mute button only pauses the
  ones switched on), camera / microphone / speaker, recordings.
- **Phone camera control:** on a phone's big view, others get switch camera, take
  photo (full resolution, shown to whoever asked, saved with the recordings) and
  flashlight. The phone shows a notice (and a flash for photos) for each.
  This app answers those requests from browsers and other phones, and can send
  them.
- **Interaction log:** chat, pointer, mute, camera and view changes are posted to
  the server and stored with the recordings, like the web client.
- **Links:** `remotesupport://call?code=<code>`, and on Android the
  `https://remotesupport.lgitech.net/livekit/...` meeting links (offered as an
  "open with" choice; automatic opening would need `assetlinks.json` on the site).

## Audio devices (Bluetooth, choosing mic and speaker)

Native code: `android/app/src/main/kotlin/.../AudioRoutePlugin.kt`,
`ios/Runner/AppDelegate.swift`; Dart: `lib/meet/audio_route.dart`.

| | Android | iOS |
|---|---|---|
| Microphones | built-in (each mic), wired, USB, Bluetooth headset | built-in, wired, Bluetooth (HFP) headsets |
| Speakers | phone earpiece, speaker, wired, USB, Bluetooth (SCO / LE audio) | phone earpiece, speaker, headsets with a mic; other devices (AirPods for music, AirPlay, Bluetooth speakers, car) via the system route picker button next to the speaker list |
| How | `AudioManager.setCommunicationDevice` (Android 12+; speakerphone / SCO switches before) and WebRTC's preferred recording device | `AVAudioSession` preferred input and speaker override |

Choosing a Bluetooth/wired/USB **microphone** also moves the call audio to that
headset (call audio over Bluetooth is two-way). Choices are remembered, follow
devices being connected/disconnected, and are mirrored into LiveKit's own
speaker preference so LiveKit doesn't route back to its default. Android 12+
asks for the "Nearby devices" (Bluetooth) permission on first launch; without it
Bluetooth devices aren't listed.

## Build and run

```bash
export PATH=$PWD/../.flutter-sdk/flutter/bin:$PATH   # repo-local SDK (gitignored)
flutter pub get
flutter run --dart-define=MEET_BASE_URL=https://remotesupport.lgitech.net/livekit
#   dev backend: --dart-define=MEET_BASE_URL=https://dev-remotesupport.lgitech.net/livekit
```

- iOS 15+ (video_player); `ios/Podfile` enables camera, microphone and
  location-when-in-use in permission_handler.
- Android minSdk 24.
- The support-session flow still uses `API_BASE_URL` and Firebase (support
  sign-in); meetings work without Firebase.

## Tests

```bash
flutter analyze
flutter test                                   # protocol, event log, audio routing, lobby
flutter test test/meet/live_api_test.dart \
  --dart-define=MEET_LIVE_URL=https://dev-remotesupport.lgitech.net/livekit  # real server
```

`test/meet/protocol_test.dart` pins the wire format to what the web client sends
(chat / `/show`, pointer, camera control, recording metadata, colours).
