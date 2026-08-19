// In-call screen. Layout from docs/07-flutter-app.md § CallScreen.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:wakelock_plus/wakelock_plus.dart';

import '../models/call_state.dart';
import '../providers.dart';
import '../widgets/ai_toggle.dart';
import '../widgets/call_controls.dart';
import '../widgets/caption_overlay.dart';
import '../widgets/participant_video.dart';
import '../widgets/rec_indicator.dart';
import '../widgets/transcript_panel.dart';
import 'call_summary.dart';

class CallScreen extends ConsumerStatefulWidget {
  const CallScreen({super.key});

  @override
  ConsumerState<CallScreen> createState() => _CallScreenState();
}

class _CallScreenState extends ConsumerState<CallScreen> {
  @override
  void initState() {
    super.initState();
    WakelockPlus.enable();
  }

  @override
  void dispose() {
    WakelockPlus.disable();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(callControllerProvider);
    final controller = ref.read(callControllerProvider.notifier);
    final isSupport = state.myRole == Role.support;

    // Route to summary on clean disconnect.
    if (state.status == ConnectionStatus.disconnected) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          Navigator.of(context).pushReplacement(
            MaterialPageRoute(builder: (_) => const CallSummaryScreen()),
          );
        }
      });
    }

    final remote = state.remoteHuman;
    final local = controller.room?.localParticipant;

    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: Stack(
          fit: StackFit.expand,
          children: [
            // Remote video (full bleed).
            if (remote != null)
              ParticipantVideoWidget(participant: remote)
            else
              const Center(
                child: Text(
                  'Waiting for the other person…',
                  style: TextStyle(color: Colors.white70),
                ),
              ),

            // Local PiP.
            if (local != null)
              Positioned(
                right: 16,
                top: 80,
                width: 120,
                height: 160,
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(12),
                  child: ParticipantVideoWidget(participant: local),
                ),
              ),

            // Status bar.
            Positioned(
              top: 0,
              left: 0,
              right: 0,
              child: _StatusBar(state: state),
            ),

            // Caption overlay.
            Positioned(
              left: 16,
              right: 16,
              bottom: isSupport ? 180 : 120,
              child: CaptionOverlayWidget(captions: state.captions),
            ),

            // Support-only transcript panel drag handle.
            if (isSupport)
              DraggableScrollableSheet(
                initialChildSize: 0.12,
                minChildSize: 0.12,
                maxChildSize: 0.5,
                builder: (context, scrollController) {
                  return TranscriptPanel(
                    entries: state.transcript,
                    scrollController: scrollController,
                  );
                },
              ),

            // Controls at the bottom.
            Positioned(
              left: 0,
              right: 0,
              bottom: 0,
              child: Container(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                    colors: [Colors.transparent, Colors.black.withValues(alpha: 0.8)],
                  ),
                ),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    CallControlsWidget(controller: controller),
                    if (isSupport) ...[
                      const SizedBox(height: 8),
                      AiToggleWidget(
                        enabled: state.aiEnabled,
                        status: state.aiToggleStatus,
                        onChanged: (enabled) async {
                          final auth = ref.read(supportAuthProvider);
                          final idToken = await auth.idToken();
                          await controller.setAiEnabled(enabled, idToken: idToken);
                        },
                      ),
                    ],
                  ],
                ),
              ),
            ),

            // Connectivity banner.
            if (state.status == ConnectionStatus.reconnecting)
              const Positioned(
                top: 56,
                left: 0,
                right: 0,
                child: Center(
                  child: Chip(
                    avatar: Icon(Icons.cloud_off, size: 16),
                    label: Text('Reconnecting…'),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _StatusBar extends StatelessWidget {
  final CallState state;
  const _StatusBar({required this.state});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      color: Colors.black.withValues(alpha: 0.5),
      child: SafeArea(
        child: Row(
          children: [
            if (state.recording) const RecIndicatorWidget(),
            if (state.recording) const SizedBox(width: 12),
            if (state.myRole == Role.caller || state.myRole == Role.support)
              _AiIndicator(enabled: state.aiEnabled),
            const Spacer(),
            Text(
              state.agentMode.name.toUpperCase(),
              style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
            ),
          ],
        ),
      ),
    );
  }
}

class _AiIndicator extends StatelessWidget {
  final bool enabled;
  const _AiIndicator({required this.enabled});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(
          enabled ? Icons.smart_toy : Icons.smart_toy_outlined,
          color: enabled ? Colors.greenAccent : Colors.white70,
          size: 18,
        ),
        const SizedBox(width: 4),
        Text(
          enabled ? 'Assistant on' : 'Assistant off',
          style: TextStyle(color: enabled ? Colors.greenAccent : Colors.white70),
        ),
      ],
    );
  }
}
