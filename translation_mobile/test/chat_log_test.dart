import 'package:flutter_test/flutter_test.dart';
import 'package:translation_mobile/src/chat_log.dart';
import 'package:translation_mobile/src/protocol.dart';

TranslationMessage _msg(String id, String text) => TranslationMessage(
      id: id,
      from: 'user-es',
      fromLanguage: 'es',
      text: text,
      toLanguage: 'en',
      translation: text,
      mode: modeEnglishRelay,
    );

void main() {
  test('messages append in order and notify listeners', () {
    final log = ChatLog();
    var notified = 0;
    log.addListener(() => notified++);

    log.add(_msg('1', 'hola'));
    log.add(_msg('2', 'adiós'));

    expect(log.messages.map((m) => m.id), ['1', '2']);
    expect(notified, 2);
  });

  test('duplicate event ids are dropped', () {
    final log = ChatLog();
    log.add(_msg('1', 'hola'));
    log.add(_msg('1', 'hola'));
    expect(log.messages, hasLength(1));
  });

  test('log is capped at maxMessages', () {
    final log = ChatLog(maxMessages: 3);
    for (var i = 0; i < 10; i++) {
      log.add(_msg('$i', 'm$i'));
    }
    expect(log.messages.map((m) => m.id), ['7', '8', '9']);
  });

  test('clear empties the log', () {
    final log = ChatLog();
    log.add(_msg('1', 'hola'));
    log.clear();
    expect(log.messages, isEmpty);
  });
}
