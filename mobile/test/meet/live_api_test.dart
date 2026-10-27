// Runs the Dart API client against a real backend (skipped by default):
//   flutter test test/meet/live_api_test.dart \
//     --dart-define=MEET_LIVE_URL=https://dev-remotesupport.lgitech.net/livekit

import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:remote_support/meet/meet_api.dart';
import 'package:remote_support/meet/protocol.dart';

const liveUrl = String.fromEnvironment('MEET_LIVE_URL');

// 1x1 JPEG.
final tinyJpeg = Uint8List.fromList([
  0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00,
  0x00, 0xFF, 0xDB, 0x00, 0x43, 0x00, ...List.filled(64, 0x01), 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01, 0x00,
  0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x14, 0x00, 0x01, ...List.filled(15, 0x00), 0x03, 0xFF, 0xC4,
  0x00, 0x14, 0x10, 0x01, ...List.filled(15, 0x00), 0x00, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3F,
  0x00, 0x37, 0xFF, 0xD9,
]);

void main() {
  test('join, events, /show check, photo, recording, recordings list, end', () async {
    final api = MeetApi(liveUrl);
    final code = 'flutter-live-${DateTime.now().millisecondsSinceEpoch % 100000000}';
    final info = await api.info(code);
    expect(info.callId, code);
    expect(info.roomActive, isFalse);

    final join = await api.join(code, 'Flutter Test');
    expect(join.wsUrl, startsWith('wss://'));
    expect(join.identity, startsWith('guest-flutter-test'));
    expect(api.meetingLink(code), '$liveUrl/?call=$code');

    final now = DateTime.now().millisecondsSinceEpoch;
    final stored = await api.postEvents(code, join.token, [
      {'id': 'f1-$now', 'type': 'chat', 'ts': now - 500, 'data': {'text': 'hello from flutter'}},
      {'id': 'f2-$now', 'type': 'torch', 'ts': now - 200, 'data': {'on': true}},
    ], now);
    expect(stored, 2);

    final card = await api.mediaCheck(code, join.token, '$liveUrl/support-web/test-assets/caller_prompt.wav');
    expect(card.kind, 'audio');
    await expectLater(
      api.mediaCheck(code, join.token, 'https://nonexistent.invalid/x.png'),
      throwsA(isA<MeetApiException>()),
    );

    final photo = await api.uploadPhoto(code, join.token, tinyJpeg,
        contentType: 'image/jpeg', requestedBy: 'guest-someone-1', width: 1, height: 1);
    expect(photo.name, startsWith('photos/'));
    expect(api.absolute(photo.path), '$liveUrl/v1/calls/$code/${photo.name}');

    if (join.recordingEnabled) {
      expect(await api.recording(code, join.token, 'start'), isTrue);
      expect(await api.recording(code, join.token, 'stop'), isFalse);
    }
    final recs = await api.recordings(code);
    expect(recs.photos.map((p) => p.name), contains(photo.name));
    expect(recs.photos.first.sizeBytes, tinyJpeg.length);
    await api.end(code, join.token);
    expect(fmtBytes(recs.photos.first.sizeBytes), endsWith('B'));
  }, skip: liveUrl.isEmpty ? 'set --dart-define=MEET_LIVE_URL=<base> to run' : null, timeout: const Timeout(Duration(minutes: 2)));
}
