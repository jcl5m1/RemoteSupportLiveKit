// Live caption overlay — last two lines. See docs/07-flutter-app.md § CallScreen.

import 'package:flutter/material.dart';

import '../models/call_state.dart';

class CaptionOverlayWidget extends StatelessWidget {
  final List<CaptionLine> captions;

  const CaptionOverlayWidget({super.key, required this.captions});

  @override
  Widget build(BuildContext context) {
    final display = captions.length > 2 ? captions.sublist(captions.length - 2) : captions;

    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.6),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: display.map((line) {
          return Text(
            '${_speakerLabel(line)}: ${line.text}',
            style: const TextStyle(color: Colors.white, fontSize: 16),
          );
        }).toList(),
      ),
    );
  }

  String _speakerLabel(CaptionLine line) {
    if (line.speakerRole == Role.caller) return 'Caller';
    if (line.speakerRole == Role.support) return 'Support';
    return line.identity;
  }
}
