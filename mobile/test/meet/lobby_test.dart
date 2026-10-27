import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:remote_support/meet/audio_route.dart';
import 'package:remote_support/meet/ui/lobby_screen.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'fakes.dart';

Widget lobby({String? code, bool recordingActive = false, VoidCallback? onSupport, AudioRouter? audio}) {
  final api = fakeApi((o, _) {
    if (o.method == 'GET' && o.path.contains('/v1/calls/')) {
      final id = Uri.decodeComponent(o.path.split('/v1/calls/').last);
      return (200, {
        'call_id': id,
        'recording_enabled': true,
        'recording_active': recordingActive,
        'room_active': false,
        'participant_count': 0,
      });
    }
    return (404, {'error': {'code': 'not_found', 'message': 'nope'}});
  });
  return MaterialApp(
    home: LobbyScreen(
      api: api,
      initialCode: code,
      previewEnabled: false,
      onSupportSession: onSupport,
      audio: audio ?? AudioRouter(platform: FakeAudioPlatform(phoneRoutes), setSpeakerPreferred: (p, {bool force = false}) async {}),
    ),
  );
}

double top(WidgetTester t, Key k) => t.getTopLeft(find.byKey(k)).dy;

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({'meet.name': 'Ann'}));

  testWidgets('order: title, preview, name, code, join, recordings, devices', (t) async {
    t.view.physicalSize = const Size(1080, 2600);
    t.view.devicePixelRatio = 2;
    addTearDown(t.view.reset);
    final audio = AudioRouter(platform: FakeAudioPlatform(phoneRoutes), setSpeakerPreferred: (p, {bool force = false}) async {});
    await audio.refresh();
    await t.pumpWidget(lobby(code: 'team-standup', audio: audio));
    await t.pump(const Duration(milliseconds: 400));
    await t.pumpAndSettle();

    expect(find.text('Ready to join?'), findsOneWidget);
    expect(find.text('Name'), findsOneWidget);
    expect(find.text('Anyone with the link can join the call.'), findsNothing);
    expect(find.textContaining('can start recording'), findsNothing);
    expect(find.byKey(const Key('recNotice')), findsNothing); // not recording now
    final keys = ['prejoinTitle', 'prejoinPreview', 'nameField', 'nameInput', 'codeField', 'callCodeInput',
        'joinCallBtn', 'viewRecsBtn', 'micPicker'];
    final ys = [for (final k in keys) top(t, Key(k))];
    expect(ys, [...ys]..sort(), reason: '$keys -> $ys');
    // Microphone and speaker side by side.
    expect(top(t, const Key('micPicker')), top(t, const Key('speakerPicker')));
    expect(t.getTopLeft(find.byKey(const Key('speakerPicker'))).dx,
        greaterThan(t.getTopLeft(find.byKey(const Key('micPicker'))).dx));
    expect(find.text('Pixel Buds'), findsNothing); // closed dropdowns show the current choice
    expect(find.text('Speaker'), findsWidgets);
    expect((t.widget<TextField>(find.byKey(const Key('nameInput')))).controller!.text, 'Ann');
  });

  testWidgets('new meeting: generated code, "Start meeting" until edited; validation', (t) async {
    await t.pumpWidget(lobby());
    await t.pumpAndSettle();
    final field = t.widget<TextField>(find.byKey(const Key('callCodeInput')));
    expect(RegExp(r'^[a-z]{3}-[a-z]{4}-[a-z]{3}$').hasMatch(field.controller!.text), isTrue);
    expect(find.text('Start a new meeting'), findsOneWidget);
    expect(find.text('Start meeting'), findsOneWidget);

    await t.enterText(find.byKey(const Key('callCodeInput')), '-bad-');
    await t.pump();
    expect(find.textContaining('Use 3–64 letters'), findsOneWidget);
    expect(t.widget<FilledButton>(find.byKey(const Key('joinCallBtn'))).onPressed, isNull);

    await t.enterText(find.byKey(const Key('callCodeInput')), 'https://remotesupport.lgitech.net/livekit/?call=Weekly-Sync');
    await t.pumpAndSettle();
    expect(field.controller!.text, 'weekly-sync'); // pasted link -> code
    expect(find.text('Join now'), findsOneWidget);
    expect(find.text('Ready to join?'), findsOneWidget);
  });

  testWidgets('copy button copies the meeting link', (t) async {
    String? copied;
    t.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, (call) async {
      if (call.method == 'Clipboard.setData') copied = (call.arguments as Map)['text'] as String;
      return null;
    });
    await t.pumpWidget(lobby(code: 'team-standup'));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const Key('copyCodeBtn')));
    await t.pump();
    expect(copied, 'https://example.test/livekit/?call=team-standup');
    expect(find.byIcon(Icons.check), findsOneWidget);
    await t.pump(const Duration(seconds: 2));
  });

  testWidgets('warns only while a recording is running', (t) async {
    await t.pumpWidget(lobby(code: 'team-standup', recordingActive: true));
    await t.pump(const Duration(milliseconds: 400));
    await t.pumpAndSettle();
    expect(find.text('● This call is being recorded.'), findsOneWidget);
  });

  testWidgets('support sessions are one tap away', (t) async {
    var opened = false;
    await t.pumpWidget(lobby(code: 'team-standup', onSupport: () => opened = true));
    await t.pumpAndSettle();
    await t.scrollUntilVisible(find.text('Remote support session'), 200, scrollable: find.byType(Scrollable).first);
    await t.tap(find.text('Remote support session'));
    expect(opened, isTrue);
  });
}
