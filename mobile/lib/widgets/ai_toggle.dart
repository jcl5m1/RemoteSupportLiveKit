// Support-only AI switch (FR-4.1). See docs/07-flutter-app.md.
//
// A switch, not a button — it has persistent state. Three visuals:
//   on / off / pending (optimistic, awaiting the room-metadata echo)
//
// Sends the REST call and the `rs.agent.control` data message together.
// Confirms on the metadata echo; reverts + snackbar if no echo within 2s.

import 'package:flutter/material.dart';

import '../models/call_state.dart';

class AiToggleWidget extends StatelessWidget {
  final bool enabled;
  final AiToggleStatus status;
  final ValueChanged<bool> onChanged;

  const AiToggleWidget({
    super.key,
    required this.enabled,
    required this.status,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final bool effectiveValue = status == AiToggleStatus.pending ? !enabled : enabled;

    Widget trailing;
    switch (status) {
      case AiToggleStatus.idle:
        trailing = Switch(
          value: enabled,
          onChanged: onChanged,
        );
        break;
      case AiToggleStatus.pending:
        trailing = const SizedBox(
          height: 20,
          width: 20,
          child: CircularProgressIndicator(strokeWidth: 2),
        );
        break;
      case AiToggleStatus.failed:
        trailing = Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.error_outline, color: Theme.of(context).colorScheme.error),
            Switch(
              value: enabled,
              onChanged: onChanged,
            ),
          ],
        );
        break;
    }

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.6),
        borderRadius: BorderRadius.circular(24),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            effectiveValue ? Icons.smart_toy : Icons.smart_toy_outlined,
            color: effectiveValue ? Colors.greenAccent : Colors.white70,
          ),
          const SizedBox(width: 8),
          Text(
            effectiveValue ? 'Assistant on' : 'Assistant off',
            style: const TextStyle(color: Colors.white),
          ),
          const SizedBox(width: 8),
          trailing,
        ],
      ),
    );
  }
}
