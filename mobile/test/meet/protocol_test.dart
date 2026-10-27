// Wire compatibility with the web client (backend/app/static/support-web/app.js).

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:remote_support/meet/meet_controller.dart' show jpegSize, isJpeg;
import 'package:remote_support/meet/protocol.dart';

Map<String, dynamic> j(List<int> b) => jsonDecode(utf8.decode(b)) as Map<String, dynamic>;
List<int> b(Object o) => utf8.encode(jsonEncode(o));

void main() {
  group('meeting codes', () {
    test('normalize, validate, generate', () {
      expect(normalizeCallCode('  Team Standup_1 '), 'team-standup-1');
      expect(callCodeRe.hasMatch('team-standup'), isTrue);
      for (final bad in ['-bad-', 'ab', 'a' * 65, 'Upper']) {
        expect(callCodeRe.hasMatch(bad), isFalse, reason: bad);
      }
      final code = newCallCode();
      expect(RegExp(r'^[a-z]{3}-[a-z]{4}-[a-z]{3}$').hasMatch(code), isTrue);
      expect(code.contains('l'), isFalse);
    });

    test('code from pasted links (all the web shorthands)', () {
      const base = 'https://remotesupport.lgitech.net/livekit';
      expect(callCodeFromText('$base/?call=Team-Standup'), 'team-standup');
      expect(callCodeFromText('$base/?=team-standup'), 'team-standup');
      expect(callCodeFromText('$base/?team-standup'), 'team-standup');
      expect(callCodeFromText('$base/call/team-standup'), 'team-standup');
      expect(callCodeFromText('remotesupport://call?code=abc-defg-hij'), 'abc-defg-hij');
      expect(callCodeFromText('abc-defg-hij'), 'abc-defg-hij');
      expect(callCodeFromText('https://example.com/'), isNull);
    });
  });

  test('colours: palette order by join time, identity tie-break, agents excluded', () {
    final t = DateTime.utc(2026, 10, 27, 10);
    final colors = assignColors([
      ColorEntry('guest-carol-3', t.add(const Duration(seconds: 5))),
      ColorEntry('guest-bob-2', t),
      ColorEntry('guest-alice-1', t),
      const ColorEntry('agent-xyz', null),
    ]);
    expect(colors, {
      'guest-alice-1': '#E69F00',
      'guest-bob-2': '#56B4E9',
      'guest-carol-3': '#009E73',
    });
  });

  group('chat + /show', () {
    test('round trip and web payload shape', () {
      final card = ShowCard.parse({
        'kind': 'map',
        'url': 'https://maps.app.goo.gl/x',
        'final_url': 'https://www.google.com/maps/place/x',
        'embed_url': null,
        'geo': {'lat': 37.4, 'lng': -122.1, 'zoom': 25, 'label': 'HQ'},
      })!;
      expect(card.geo!.zoom, 19); // clamped like the web
      final payload = encodeChat(text: '/show maps.app.goo.gl/x', name: 'Ann', ts: 5, show: card);
      final m = j(payload);
      expect(m['text'], '/show maps.app.goo.gl/x');
      expect(m['show']['final_url'], 'https://www.google.com/maps/place/x');
      final back = decodeChat(payload, identity: 'guest-ann-1', name: 'Ann')!;
      expect(back.show!.kind, 'map');
      expect(back.show!.geo!.label, 'HQ');
    });

    test('sanitizes like the web: unknown kinds and non-http urls dropped', () {
      expect(ShowCard.parse({'kind': 'image', 'url': 'javascript:alert(1)', 'final_url': 'https://x.test/a'}), isNull);
      expect(ShowCard.parse({'kind': 'exe', 'url': 'https://x.test', 'final_url': 'https://x.test'}), isNull);
      expect(ShowCard.parse({'kind': 'image', 'url': 'https://x.test/a.png', 'final_url': 'https://x.test/a.png',
          'embed_url': 'file:///etc/passwd'}), isNull);
      // Message still arrives, just without the card.
      final msg = decodeChat(b({'text': 'hi', 'ts': 1, 'show': {'kind': 'exe'}}), identity: 'i', name: 'n')!;
      expect(msg.show, isNull);
      expect(decodeChat(b({'nope': 1}), identity: 'i', name: 'n'), isNull);
    });

    test('photo cards carry a title', () {
      final card = ShowCard.parse({
        'kind': 'image', 'url': 'https://h/livekit/v1/calls/c/photos/p.jpg',
        'final_url': 'https://h/livekit/v1/calls/c/photos/p.jpg', 'embed_url': null, 'title': 'Photo from Bob',
      })!;
      expect(card.title, 'Photo from Bob');
      expect(card.toJson()['title'], 'Photo from Bob');
    });
  });

  group('pointer', () {
    test('encode rounds to 4 places; decode validates and filters colours', () {
      final m = j(encodePointer(target: 'guest-bob-2', x: 0.123456, y: 0.5, color: '#E69F00', ts: 9));
      expect(m, {'target': 'guest-bob-2', 'x': 0.1235, 'y': 0.5, 'color': '#E69F00', 'ts': 9});
      expect(decodePointer(b({'target': 't', 'x': 1.2, 'y': 0.5})), isNull);
      expect(decodePointer(b({'target': 't', 'x': 0.2, 'y': 0.5, 'color': 'red'}))!.color, isNull);
      expect(decodePointer(b({'target': 't', 'x': 0.2, 'y': 0.5, 'color': '#009E73'}))!.color, '#009E73');
    });

    test('frame mapping: contain bars, cover crop, mirroring', () {
      // 1280x720 video in a 400x400 view: contain = 400x225 centred (bars 87.5).
      final contain = FrameRect.fit(1280, 720, 400, 400, cover: false)!;
      expect(contain.y, 87.5);
      expect(contain.toFrame(100, 200, mirrored: false), (x: 0.25, y: (200 - 87.5) / 225));
      expect(contain.toFrame(100, 50, mirrored: false), isNull); // on the bar
      // Mirrored self view: a tap on the left is the right of the frame.
      expect(contain.toFrame(100, 200, mirrored: true)!.x, 0.75);
      final back = contain.toView(0.75, 0.5, mirrored: true);
      expect(back.x, 100);
      // Cover: 711x400, cropped; edges clamp to the view.
      final cover = FrameRect.fit(1280, 720, 400, 400, cover: true)!;
      expect(cover.toView(0, 0.5, mirrored: false).x, 0);
      expect(contain.ringSize, 48); // 12% of 400
      expect(FrameRect.fit(10, 10, 100, 100, cover: false)!.ringSize, 18); // min
    });
  });

  group('camctl', () {
    test('state / cmd / result match the web shapes', () {
      const s = CamState(phone: true, cam: true, cams: 2, torch: true, torchOn: false);
      expect(j(const CamStateMsg(s).encode()),
          {'type': 'state', 'phone': true, 'cam': true, 'cams': 2, 'torch': true, 'torchOn': false});
      expect(j(const CamCmd('id1', 'torch', on: true).encode()), {'type': 'cmd', 'op': 'torch', 'id': 'id1', 'on': true});
      expect(j(const CamCmd('id2', 'photo').encode()), {'type': 'cmd', 'op': 'photo', 'id': 'id2'});
      expect(j(const CamResult('id2', 'photo', ok: true, photo: 'photos/x.jpg').encode()),
          {'type': 'result', 'id': 'id2', 'op': 'photo', 'ok': true, 'photo': 'photos/x.jpg'});
    });

    test('decodes what the web sends, rejects unknown ops', () {
      final cmd = CamCtl.decode(b({'type': 'cmd', 'op': 'cycle', 'id': 'abc'}));
      expect(cmd, isA<CamCmd>());
      expect((cmd! as CamCmd).op, 'cycle');
      expect(CamCtl.decode(b({'type': 'cmd', 'op': 'format_disk', 'id': 'x'})), isNull);
      final state = CamCtl.decode(b({'type': 'state', 'phone': true, 'cam': true, 'cams': 99, 'torch': false}));
      expect((state! as CamStateMsg).state.cams, 16); // clamped like the web
      final res = CamCtl.decode(b({'type': 'result', 'id': 'q', 'op': 'photo', 'ok': false, 'error': 'camera is off'}));
      expect((res! as CamResult).error, 'camera is off');
    });
  });

  test('recording metadata', () {
    expect(RecordingMeta.parse('').recording, isFalse);
    expect(RecordingMeta.parse('not json').recording, isFalse);
    final m = RecordingMeta.parse('{"recording": true, "started_by": "Ann", "skipped": 2}');
    expect((m.recording, m.startedBy, m.skipped), (true, 'Ann', 2));
  });

  test('data log descriptions never include the payload', () {
    expect(describePayload('chat', b({'text': 'secret words', 'ts': 1})), 'Chat message (12 chars)');
    expect(describePayload('camctl', b({'type': 'cmd', 'op': 'photo'})), 'Camera command (photo)');
    expect(describePayload(null, [0, 1, 2]), 'Binary data');
    expect(fmtBytes(1500), '2 KB');
    expect(fmtBytes(2500000), '2.5 MB');
  });

  test('JPEG size from the SOF header', () {
    // Minimal JPEG: SOI, APP0 (len 16), SOF0 (h=3024, w=4032), EOI.
    final bytes = <int>[
      0xFF, 0xD8,
      0xFF, 0xE0, 0x00, 0x10, ...List.filled(14, 0),
      0xFF, 0xC0, 0x00, 0x11, 0x08, 0x0B, 0xD0, 0x0F, 0xC0, 0x03, ...List.filled(9, 0),
      0xFF, 0xD9,
    ];
    expect(isJpeg(bytes), isTrue);
    expect(jpegSize(bytes), (4032, 3024));
    expect(jpegSize([0x89, 0x50, 0x4E, 0x47]), isNull);
  });
}
