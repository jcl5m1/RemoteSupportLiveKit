import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:translation_mobile/call_screen.dart';
import 'package:translation_mobile/src/chat_log.dart';
import 'package:translation_mobile/src/protocol.dart';
import 'package:translation_mobile/src/room_controller.dart';

class FakeTranslationClient extends ChangeNotifier
    implements TranslationClient {
  final List<String> sentModes = [];
  String? declaredLanguage;

  @override
  String myIdentity = 'user-zh';

  @override
  CallStatus status = CallStatus.connected;

  @override
  String? error;

  @override
  bool microphoneEnabled = true;

  @override
  final ChatLog chat = ChatLog();

  @override
  String? bridgeLanguage;

  @override
  Future<void> connect({
    required String serverUrl,
    required String token,
    required String identity,
    required String preferredLanguage,
  }) async {}

  @override
  Future<void> disconnect() async {}

  @override
  Future<void> setMicrophoneEnabled(bool enabled) async {
    microphoneEnabled = enabled;
    notifyListeners();
  }

  @override
  Future<void> setRelayMode(String mode) async {
    sentModes.add(mode);
  }

  @override
  Future<void> declarePreferredLanguage(String language) async {
    declaredLanguage = language;
  }
}

void main() {
  testWidgets('renders chat translations as bubbles', (tester) async {
    final client = FakeTranslationClient();
    await tester.pumpWidget(MaterialApp(home: CallScreen(client: client)));

    client.chat.add(const TranslationMessage(
      id: '1',
      from: 'user-es',
      fromLanguage: 'es',
      text: 'hola',
      toLanguage: 'zh',
      translation: '你好',
      mode: 'NATIVE_RELAY',
    ));
    await tester.pump();

    expect(find.text('你好'), findsOneWidget);
    expect(find.text('hola'), findsOneWidget);
    expect(find.text('user-es (es)'), findsOneWidget);
  });

  testWidgets('mode toggle sends NATIVE_RELAY then ENGLISH_RELAY', (tester) async {
    final client = FakeTranslationClient();
    await tester.pumpWidget(MaterialApp(home: CallScreen(client: client)));

    await tester.tap(find.text('Switch to native'));
    await tester.pump();
    expect(client.sentModes, ['NATIVE_RELAY']);
    expect(find.text('Switch to English'), findsOneWidget);

    await tester.tap(find.text('Switch to English'));
    await tester.pump();
    expect(client.sentModes, ['NATIVE_RELAY', 'ENGLISH_RELAY']);
  });

  testWidgets('mic button mutes and unmutes', (tester) async {
    final client = FakeTranslationClient();
    await tester.pumpWidget(MaterialApp(home: CallScreen(client: client)));

    await tester.tap(find.text('Mute'));
    await tester.pump();
    expect(client.microphoneEnabled, isFalse);
    expect(find.text('Unmute'), findsOneWidget);

    await tester.tap(find.text('Unmute'));
    await tester.pump();
    expect(client.microphoneEnabled, isTrue);
  });

  testWidgets('shows error banner when not connected', (tester) async {
    final client = FakeTranslationClient()
      ..status = CallStatus.error
      ..error = 'Could not connect: denied';
    await tester.pumpWidget(MaterialApp(home: CallScreen(client: client)));

    expect(find.text('Could not connect: denied'), findsOneWidget);
  });
}
