import 'package:flutter/material.dart';

import 'src/protocol.dart';
import 'src/room_controller.dart';

/// In-call screen: translation chat, relay-mode toggle, mic toggle.
///
/// Takes any [TranslationClient] so tests can inject a fake.
class CallScreen extends StatefulWidget {
  final TranslationClient client;
  const CallScreen({super.key, required this.client});

  @override
  State<CallScreen> createState() => _CallScreenState();
}

class _CallScreenState extends State<CallScreen> {
  String _mode = modeEnglishRelay;

  TranslationClient get client => widget.client;

  @override
  void initState() {
    super.initState();
    client.addListener(_onChanged);
  }

  @override
  void dispose() {
    client.removeListener(_onChanged);
    super.dispose();
  }

  void _onChanged() => setState(() {});

  Future<void> _toggleMode() async {
    final next = _mode == modeEnglishRelay ? modeNativeRelay : modeEnglishRelay;
    await client.setRelayMode(next);
    setState(() => _mode = next);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('Translation call — ${client.myIdentity}'),
        actions: [
          Center(
            child: Padding(
              padding: const EdgeInsets.only(right: 12),
              child: Text(
                'Relay: ${_mode == modeEnglishRelay ? 'English' : 'Native'}',
                style: const TextStyle(fontSize: 13),
              ),
            ),
          ),
        ],
      ),
      body: Column(
        children: [
          if (client.status != CallStatus.connected)
            MaterialBanner(
              content: Text(client.error ?? 'Connecting…'),
              actions: const [SizedBox.shrink()],
            ),
          Expanded(
            child: AnimatedBuilder(
              animation: client.chat,
              builder: (context, _) {
                final messages = client.chat.messages;
                if (messages.isEmpty) {
                  return const Center(
                    child: Text('Translations will appear here'),
                  );
                }
                return ListView.builder(
                  padding: const EdgeInsets.all(12),
                  itemCount: messages.length,
                  itemBuilder: (context, index) =>
                      _TranslationBubble(message: messages[index]),
                );
              },
            ),
          ),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: [
                  FilledButton.tonalIcon(
                    onPressed: () => client.setMicrophoneEnabled(
                      !client.microphoneEnabled,
                    ),
                    icon: Icon(
                      client.microphoneEnabled ? Icons.mic : Icons.mic_off,
                    ),
                    label: Text(client.microphoneEnabled ? 'Mute' : 'Unmute'),
                  ),
                  FilledButton.tonalIcon(
                    onPressed: _toggleMode,
                    icon: const Icon(Icons.translate),
                    label: Text(
                      _mode == modeEnglishRelay
                          ? 'Switch to native'
                          : 'Switch to English',
                    ),
                  ),
                  FilledButton.tonalIcon(
                    onPressed: () => client.disconnect(),
                    icon: const Icon(Icons.call_end),
                    label: const Text('Leave'),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _TranslationBubble extends StatelessWidget {
  final TranslationMessage message;
  const _TranslationBubble({required this.message});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: Padding(
        padding: const EdgeInsets.all(10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${message.from} (${message.fromLanguage})',
              style: theme.textTheme.labelSmall,
            ),
            const SizedBox(height: 2),
            Text(message.translation, style: theme.textTheme.bodyLarge),
            if (message.translation != message.text)
              Text(
                message.text,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.hintColor,
                  fontStyle: FontStyle.italic,
                ),
              ),
          ],
        ),
      ),
    );
  }
}
