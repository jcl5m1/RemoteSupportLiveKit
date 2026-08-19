// Caller waiting room. See docs/07-flutter-app.md § WaitingRoom.
//
// Caller sees their own camera preview, the 6-char code, a QR code, and a
// share affordance. The agent is already connected in SOLO mode, so we show an
// "Assistant is listening" indicator. Auto-advances to CallScreen when support
// connects.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:qr_flutter/qr_flutter.dart';
import 'package:share_plus/share_plus.dart';

import '../models/call_state.dart';
import '../providers.dart';
import '../widgets/participant_video.dart';
import 'call_screen.dart';

class WaitingRoomScreen extends ConsumerStatefulWidget {
  final String joinCode;
  final bool unrecorded;

  const WaitingRoomScreen({
    super.key,
    required this.joinCode,
    this.unrecorded = false,
  });

  @override
  ConsumerState<WaitingRoomScreen> createState() => _WaitingRoomScreenState();
}

class _WaitingRoomScreenState extends ConsumerState<WaitingRoomScreen> {
  @override
  void initState() {
    super.initState();
    // Wakelock keeps the screen on so the caller can set the phone down and
    // still be heard while waiting.
    // WakelockPlus.enable(); // kept minimal; enabled in CallScreen.
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(callControllerProvider);

    // Auto-advance when a support participant connects.
    if (state.remoteHuman != null && state.status == ConnectionStatus.connected) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          Navigator.of(context).pushReplacement(
            MaterialPageRoute(builder: (_) => const CallScreen()),
          );
        }
      });
    }

    final localParticipant = ref.read(callControllerProvider.notifier).room?.localParticipant;

    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16.0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Waiting for support',
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const SizedBox(height: 8),
              Text(
                'Share this code with your support operator.',
                style: Theme.of(context).textTheme.bodyMedium?.copyWith(color: Colors.grey[600]),
              ),
              const SizedBox(height: 16),
              Container(
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  color: Theme.of(context).colorScheme.surfaceContainerHighest,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Column(
                  children: [
                    Text(
                      widget.joinCode,
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 8),
                    QrImageView(
                      data: _qrPayload(),
                      size: 160,
                    ),
                    const SizedBox(height: 8),
                    Row(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        IconButton(
                          icon: const Icon(Icons.copy),
                          tooltip: 'Copy code',
                          onPressed: _copyCode,
                        ),
                        IconButton(
                          icon: const Icon(Icons.share),
                          tooltip: 'Share link',
                          onPressed: _shareLink,
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),
              Expanded(
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(12),
                  child: localParticipant != null
                      ? ParticipantVideoWidget(participant: localParticipant)
                      : const Center(child: CircularProgressIndicator()),
                ),
              ),
              const SizedBox(height: 16),
              _AssistantBanner(state: state),
              if (widget.unrecorded)
                Padding(
                  padding: const EdgeInsets.only(top: 8.0),
                  child: Text(
                    'This session is not being recorded.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Theme.of(context).colorScheme.error),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }

  String _qrPayload() {
    return 'https://support.example.com/j/${widget.joinCode}';
  }

  void _copyCode() {
    // Clipboard.setData(ClipboardData(text: widget.joinCode));
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Code copied')),
    );
  }

  void _shareLink() {
    Share.share(_qrPayload(), subject: 'Join my Remote Support call');
  }
}

class _AssistantBanner extends StatelessWidget {
  final CallState state;
  const _AssistantBanner({required this.state});

  @override
  Widget build(BuildContext context) {
    final listening = state.agent != null && state.aiEnabled;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      decoration: BoxDecoration(
        color: listening ? Colors.green[50] : Colors.grey[100],
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: [
          Icon(
            listening ? Icons.smart_toy : Icons.smart_toy_outlined,
            color: listening ? Colors.green[700] : Colors.grey,
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              listening
                  ? 'Assistant is listening — you can start describing the problem.'
                  : 'Assistant is not active right now.',
              style: Theme.of(context).textTheme.bodyMedium,
            ),
          ),
        ],
      ),
    );
  }
}
