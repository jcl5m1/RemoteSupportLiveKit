import 'package:flutter/foundation.dart';

import 'protocol.dart';

/// Ordered, listener-friendly chat log for the translation feed.
class ChatLog extends ChangeNotifier {
  final List<TranslationMessage> _messages = <TranslationMessage>[];
  final int maxMessages;

  ChatLog({this.maxMessages = 200});

  List<TranslationMessage> get messages => List.unmodifiable(_messages);

  void add(TranslationMessage message) {
    // Dedupe on bridge event id in case a packet is delivered twice.
    if (_messages.any((m) => m.id.isNotEmpty && m.id == message.id)) return;
    _messages.add(message);
    if (_messages.length > maxMessages) {
      _messages.removeRange(0, _messages.length - maxMessages);
    }
    notifyListeners();
  }

  void clear() {
    _messages.clear();
    notifyListeners();
  }
}
