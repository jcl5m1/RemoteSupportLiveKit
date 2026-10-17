import 'package:flutter_test/flutter_test.dart';
import 'package:translation_mobile/src/protocol.dart';

void main() {
  group('feed routing', () {
    test('client subscribes only to its own translation feed', () {
      expect(shouldSubscribeToFeed('translation-user-es', 'user-es'), isTrue);
      expect(shouldSubscribeToFeed('translation-user-zh', 'user-es'), isFalse);
      expect(shouldSubscribeToFeed('camera', 'user-es'), isFalse);
    });

    test('feed name convention matches the bridge', () {
      expect(feedNameFor('user-zh'), 'translation-user-zh');
    });
  });

  group('control payloads', () {
    test('mode switch payload', () {
      expect(modeControlPayload('NATIVE_RELAY'), {'mode': 'NATIVE_RELAY'});
    });

    test('preferred language payload', () {
      expect(preferredLanguagePayload('tl'), {'preferred_language': 'tl'});
    });
  });

  group('chat message parsing', () {
    test('parses a bridge packet', () {
      final m = TranslationMessage.fromJson(const {
        'id': 'evt-1',
        'from': 'user-es',
        'from_language': 'es',
        'text': 'hola',
        'to': 'user-zh',
        'to_language': 'zh',
        'translation': '你好',
        'mode': 'NATIVE_RELAY',
      });
      expect(m.from, 'user-es');
      expect(m.fromLanguage, 'es');
      expect(m.text, 'hola');
      expect(m.toLanguage, 'zh');
      expect(m.translation, '你好');
      expect(m.mode, 'NATIVE_RELAY');
    });

    test('tolerates missing fields', () {
      final m = TranslationMessage.fromJson(const {'text': 'hi'});
      expect(m.text, 'hi');
      expect(m.from, '');
      expect(m.translation, '');
    });
  });
}
