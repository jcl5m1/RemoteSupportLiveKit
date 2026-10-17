import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:livekit_client/livekit_client.dart' as lk;
import 'package:permission_handler/permission_handler.dart';

import 'chat_log.dart';
import 'protocol.dart';

enum CallStatus { disconnected, connecting, connected, error }

/// Abstraction over the LiveKit room so widget tests can use a fake.
abstract class TranslationClient implements Listenable {
  String get myIdentity;
  CallStatus get status;
  String? get error;
  bool get microphoneEnabled;

  ChatLog get chat;

  /// My listen language as understood by the bridge (detected or declared).
  String? get bridgeLanguage;

  Future<void> connect({
    required String serverUrl,
    required String token,
    required String identity,
    required String preferredLanguage,
  });

  Future<void> disconnect();

  Future<void> setMicrophoneEnabled(bool enabled);

  /// Sends a relay-mode switch to the bridge (ENGLISH_RELAY / NATIVE_RELAY).
  Future<void> setRelayMode(String mode);

  /// Declares my preferred listen language to the bridge.
  Future<void> declarePreferredLanguage(String language);

  void dispose();
}

/// Live translation-call client.
///
/// Contract with the translation bridge (docs/13):
///   * publish microphone ONLY — audio-only participants, no camera;
///   * connect with autoSubscribe=false and subscribe ONLY to the
///     `translation-<my identity>` feed, so we never hear another
///     participant's relay and never hear our own;
///   * render `rs.translation.chat` data packets into [chat];
///   * declare our preferred language and mode changes on
///     `rs.translation.control`.
class LiveKitTranslationClient extends ChangeNotifier implements TranslationClient {
  final lk.Room _room = lk.Room();
  final List<lk.CancelListenFunc> _subscriptions = [];

  @override
  final ChatLog chat = ChatLog();

  @override
  String myIdentity = '';

  String _preferredLanguage = 'en';

  @override
  CallStatus status = CallStatus.disconnected;

  @override
  String? error;

  @override
  bool microphoneEnabled = false;

  /// Latest language the bridge reported for us in a chat packet.
  String? _bridgeLanguage;

  @override
  String? get bridgeLanguage => _bridgeLanguage ?? _preferredLanguage;

  @override
  Future<void> connect({
    required String serverUrl,
    required String token,
    required String identity,
    required String preferredLanguage,
  }) async {
    myIdentity = identity;
    _preferredLanguage = preferredLanguage;
    status = CallStatus.connecting;
    error = null;
    notifyListeners();

    try {
      await [Permission.microphone].request();

      // autoSubscribe=false: we pick exactly one feed — our own.
      await _room.connect(
        serverUrl,
        token,
        connectOptions: const lk.ConnectOptions(autoSubscribe: false),
      );

      // Audio capture can fail (emulator without audio, revoked permission) —
      // listening to translations must still work.
      try {
        await _room.localParticipant?.setMicrophoneEnabled(true);
        microphoneEnabled = true;
      } catch (e) {
        microphoneEnabled = false;
        debugPrint('microphone unavailable, continuing listen-only: $e');
      }

      _wireEvents();
      await _subscribeOwnFeed();

      // Tell the bridge which language to speak to us in.
      await declarePreferredLanguage(preferredLanguage);

      status = CallStatus.connected;
      notifyListeners();
    } catch (e) {
      status = CallStatus.error;
      error = 'Could not connect: $e';
      debugPrint('translation connect failed: $e');
      notifyListeners();
      rethrow;
    }
  }

  void _wireEvents() {
    _subscriptions.add(
      _room.events.listen((event) async {
        if (event is lk.TrackPublishedEvent) {
          await _maybeSubscribe(event.publication);
        } else if (event is lk.DataReceivedEvent) {
          _onData(event);
        }
      }),
    );
  }

  Future<void> _subscribeOwnFeed() async {
    for (final participant in _room.remoteParticipants.values) {
      for (final publication in participant.trackPublications.values) {
        await _maybeSubscribe(publication);
      }
    }
  }

  Future<void> _maybeSubscribe(lk.RemoteTrackPublication publication) async {
    if (publication.name != feedNameFor(myIdentity)) return;
    if (!publication.subscribed) {
      await publication.subscribe();
    }
  }

  void _onData(lk.DataReceivedEvent event) {
    if (event.topic != chatTopic) return;
    try {
      final json = jsonDecode(utf8.decode(event.data)) as Map<String, dynamic>;
      final message = TranslationMessage.fromJson(json);
      if (message.from == myIdentity) {
        _bridgeLanguage = message.fromLanguage;
      }
      chat.add(message);
    } catch (_) {
      // Malformed packet: ignore, the transcript sink has the durable copy.
    }
  }

  @override
  Future<void> declarePreferredLanguage(String language) async {
    _preferredLanguage = language;
    await _room.localParticipant?.publishData(
      utf8.encode(jsonEncode(preferredLanguagePayload(language))),
      reliable: true,
      topic: controlTopic,
    );
    notifyListeners();
  }

  @override
  Future<void> setRelayMode(String mode) async {
    await _room.localParticipant?.publishData(
      utf8.encode(jsonEncode(modeControlPayload(mode))),
      reliable: true,
      topic: controlTopic,
    );
  }

  @override
  Future<void> setMicrophoneEnabled(bool enabled) async {
    await _room.localParticipant?.setMicrophoneEnabled(enabled);
    microphoneEnabled = enabled;
    notifyListeners();
  }

  @override
  Future<void> disconnect() async {
    for (final cancel in _subscriptions) {
      await cancel();
    }
    _subscriptions.clear();
    await _room.disconnect();
    await _room.dispose();
    status = CallStatus.disconnected;
    notifyListeners();
  }

  @override
  void dispose() {
    for (final cancel in _subscriptions) {
      cancel();
    }
    _subscriptions.clear();
    _room.dispose();
    super.dispose();
  }
}
