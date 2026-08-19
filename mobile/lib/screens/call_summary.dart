// Post-call summary screen. See docs/07-flutter-app.md.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers.dart';
import 'role_select.dart';

class CallSummaryScreen extends ConsumerWidget {
  const CallSummaryScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(callControllerProvider);
    final transcriptCount = state.transcript.length;

    return Scaffold(
      appBar: AppBar(title: const Text('Call ended')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24.0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Icon(
                Icons.check_circle_outline,
                size: 80,
                color: Theme.of(context).colorScheme.primary,
              ),
              const SizedBox(height: 24),
              Text(
                'Thanks for using Remote Support',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 24),
              _SummaryTile(
                icon: Icons.chat_bubble_outline,
                label: 'Transcript entries',
                value: transcriptCount.toString(),
              ),
              _SummaryTile(
                icon: state.recording ? Icons.videocam : Icons.videocam_off,
                label: 'Recording',
                value: state.recording ? 'Recorded' : 'Not recorded',
              ),
              _SummaryTile(
                icon: Icons.smart_toy_outlined,
                label: 'AI assistant',
                value: state.aiEnabled ? 'Enabled' : 'Disabled',
              ),
              const Spacer(),
              FilledButton(
                onPressed: () {
                  ref.read(callControllerProvider.notifier).disconnect();
                  Navigator.of(context).pushAndRemoveUntil(
                    MaterialPageRoute(builder: (_) => const RoleSelectScreen()),
                    (_) => false,
                  );
                },
                child: const Text('Done'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _SummaryTile extends StatelessWidget {
  final IconData icon;
  final String label;
  final String value;

  const _SummaryTile({
    required this.icon,
    required this.label,
    required this.value,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8.0),
      child: Row(
        children: [
          Icon(icon, color: Theme.of(context).colorScheme.primary),
          const SizedBox(width: 16),
          Expanded(child: Text(label)),
          Text(value, style: const TextStyle(fontWeight: FontWeight.bold)),
        ],
      ),
    );
  }
}
