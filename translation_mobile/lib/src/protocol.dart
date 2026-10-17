/// Wire protocol shared with the agent's translation bridge
/// (agent/agent/translation/bridge.py).
///
/// Two data topics:
///   rs.translation.chat    agent -> each participant, translated transcripts
///   rs.translation.control participant -> agent, mode + language declarations
///
/// One audio track per participant: `translation-<identity>` is that
/// participant's private relay feed; a client subscribes ONLY to its own.
library translation_protocol;

const String chatTopic = 'rs.translation.chat';
const String controlTopic = 'rs.translation.control';
const String trackPrefix = 'translation-';

const String modeEnglishRelay = 'ENGLISH_RELAY';
const String modeNativeRelay = 'NATIVE_RELAY';

const List<String> supportedLanguages = <String>['en', 'zh', 'es', 'tl'];

String feedNameFor(String identity) => '$trackPrefix$identity';

/// Whether this client should subscribe to a published translation feed.
bool shouldSubscribeToFeed(String trackName, String myIdentity) {
  return trackName == feedNameFor(myIdentity);
}

/// One chat message from the bridge.
class TranslationMessage {
  final String id;
  final String from;
  final String fromLanguage;
  final String text;
  final String toLanguage;
  final String translation;
  final String mode;

  const TranslationMessage({
    required this.id,
    required this.from,
    required this.fromLanguage,
    required this.text,
    required this.toLanguage,
    required this.translation,
    required this.mode,
  });

  factory TranslationMessage.fromJson(Map<String, dynamic> json) {
    return TranslationMessage(
      id: json['id'] as String? ?? '',
      from: json['from'] as String? ?? '',
      fromLanguage: json['from_language'] as String? ?? '',
      text: json['text'] as String? ?? '',
      toLanguage: json['to_language'] as String? ?? '',
      translation: json['translation'] as String? ?? '',
      mode: json['mode'] as String? ?? '',
    );
  }
}

/// `{"mode": "NATIVE_RELAY"}` — switch the room's relay mode.
Map<String, dynamic> modeControlPayload(String mode) => <String, dynamic>{
      'mode': mode,
    };

/// `{"preferred_language": "zh"}` — declare MY listen language.
Map<String, dynamic> preferredLanguagePayload(String language) =>
    <String, dynamic>{'preferred_language': language};
