// Support transcript panel. Color-coded by role, auto-scroll + jump-to-live.
// See docs/07-flutter-app.md § CallScreen.

import 'package:flutter/material.dart';

import '../models/call_state.dart';

class TranscriptPanel extends StatefulWidget {
  final List<TranscriptEntry> entries;
  final ScrollController scrollController;

  const TranscriptPanel({
    super.key,
    required this.entries,
    required this.scrollController,
  });

  @override
  State<TranscriptPanel> createState() => _TranscriptPanelState();
}

class _TranscriptPanelState extends State<TranscriptPanel> {
  bool _autoScroll = true;

  @override
  void initState() {
    super.initState();
    widget.scrollController.addListener(_onScroll);
  }

  @override
  void didUpdateWidget(covariant TranscriptPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.entries.length > oldWidget.entries.length && _autoScroll) {
      _scrollToBottom();
    }
  }

  @override
  void dispose() {
    widget.scrollController.removeListener(_onScroll);
    super.dispose();
  }

  void _onScroll() {
    if (!widget.scrollController.hasClients) return;
    final max = widget.scrollController.position.maxScrollExtent;
    final current = widget.scrollController.offset;
    const threshold = 48.0;
    setState(() => _autoScroll = current >= max - threshold);
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!widget.scrollController.hasClients) return;
      widget.scrollController.animateTo(
        widget.scrollController.position.maxScrollExtent,
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOut,
      );
    });
  }

  void _jumpToLive() {
    _scrollToBottom();
    setState(() => _autoScroll = true);
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: const BorderRadius.vertical(top: Radius.circular(16)),
        boxShadow: [BoxShadow(color: Colors.black.withValues(alpha: 0.2), blurRadius: 8)],
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          // Drag handle.
          Container(
            width: 40,
            height: 4,
            margin: const EdgeInsets.symmetric(vertical: 8),
            decoration: BoxDecoration(
              color: Colors.grey[400],
              borderRadius: BorderRadius.circular(2),
            ),
          ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Row(
              children: [
                const Icon(Icons.chat_bubble_outline, size: 18),
                const SizedBox(width: 8),
                const Expanded(child: Text('Transcript', style: TextStyle(fontWeight: FontWeight.bold))),
                if (!_autoScroll)
                  TextButton.icon(
                    onPressed: _jumpToLive,
                    icon: const Icon(Icons.arrow_downward, size: 16),
                    label: const Text('Live'),
                  ),
              ],
            ),
          ),
          Expanded(
            child: ListView.builder(
              controller: widget.scrollController,
              padding: const EdgeInsets.all(16),
              itemCount: widget.entries.length,
              itemBuilder: (context, index) {
                final entry = widget.entries[index];
                return _TranscriptLine(entry: entry);
              },
            ),
          ),
        ],
      ),
    );
  }
}

class _TranscriptLine extends StatelessWidget {
  final TranscriptEntry entry;

  const _TranscriptLine({required this.entry});

  @override
  Widget build(BuildContext context) {
    final color = _roleColor(entry.role);
    return Padding(
      padding: const EdgeInsets.only(bottom: 8.0),
      child: RichText(
        text: TextSpan(
          style: DefaultTextStyle.of(context).style,
          children: [
            TextSpan(
              text: '${_label(entry.role)} ',
              style: TextStyle(fontWeight: FontWeight.bold, color: color),
            ),
            TextSpan(text: entry.text),
          ],
        ),
      ),
    );
  }

  Color _roleColor(String role) {
    switch (role) {
      case 'caller':
        return Colors.blue;
      case 'support':
        return Colors.green;
      case 'agent':
        return Colors.purple;
      default:
        return Colors.grey;
    }
  }

  String _label(String role) {
    switch (role) {
      case 'caller':
        return 'Caller:';
      case 'support':
        return 'Support:';
      case 'agent':
        return 'Assistant:';
      default:
        return '$role:';
    }
  }
}
