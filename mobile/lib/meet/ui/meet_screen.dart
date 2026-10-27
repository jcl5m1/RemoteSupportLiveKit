// In-call screen for open meetings (web: #callUI). Stage: the big view(s)
// tiled, everyone else as small windows stacked bottom-right; control bar:
// mute, hang up, record, people, chat, settings.

import 'dart:async';
import 'dart:math';

import 'package:flutter/material.dart';

import '../meet_api.dart';
import '../meet_controller.dart';
import '../protocol.dart';
import 'panels.dart';
import 'participant_tile.dart';
import 'recordings_screen.dart';
import 'show_viewer.dart';

class MeetScreen extends StatefulWidget {
  final MeetController controller;
  const MeetScreen({super.key, required this.controller});

  @override
  State<MeetScreen> createState() => _MeetScreenState();
}

class _MeetScreenState extends State<MeetScreen> {
  MeetController get c => widget.controller;
  int? _panelTab; // null = closed
  final _subs = <StreamSubscription<Object>>[];
  bool _wasConnected = false;
  bool _leaving = false;

  @override
  void initState() {
    super.initState();
    _subs
      ..add(c.notices.listen(_onNotice))
      ..add(c.shows.listen((s) => _openShow(s.card, s.from)));
    c.addListener(_onChange);
    if (!c.connected && !c.connecting) c.connect();
  }

  void _onChange() {
    if (c.connected) _wasConnected = true;
    // Dropped out of the call (kicked, network gave up): back to the lobby.
    if (_wasConnected && !c.connected && !_leaving && mounted) {
      _leaving = true;
      Navigator.of(context).maybePop();
    }
  }

  @override
  void dispose() {
    for (final s in _subs) {
      s.cancel();
    }
    c.removeListener(_onChange);
    super.dispose();
  }

  void _onNotice(Notice n) {
    if (!mounted) return;
    final m = ScaffoldMessenger.of(context);
    m.hideCurrentSnackBar();
    m.showSnackBar(SnackBar(
      content: Text(n.text),
      backgroundColor: n.error ? const Color(0xFFB3261E) : null,
      behavior: SnackBarBehavior.floating,
      duration: Duration(seconds: n.error ? 5 : 2),
    ));
  }

  void _openShow(ShowCard card, String from) {
    if (!mounted) return;
    final base = '${c.api.baseUrl}/';
    // A new "/show" replaces whatever is showing.
    Navigator.of(context).popUntil((r) => r.settings.name != 'show');
    Navigator.of(context)
        .push(MaterialPageRoute<void>(
          settings: const RouteSettings(name: 'show'),
          fullscreenDialog: true,
          builder: (_) => ShowViewer(
            card: card,
            from: from,
            baseUrl: base,
            mapsApiKey: c.info?.mapsApiKey,
            mapsMapId: c.info?.mapsMapId,
          ),
        ))
        .then((_) => c.logShowClosed(card));
  }

  Future<void> _hangUp() async {
    _leaving = true;
    await c.leave();
    if (mounted) Navigator.of(context).maybePop();
  }

  void _togglePanel(int tab) => setState(() => _panelTab = _panelTab == tab ? null : tab);

  void _settings() {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (_) => SettingsSheet(
        controller: c,
        onRecordings: () {
          Navigator.of(context).pop();
          _recordings(context, c.api, c.callId);
        },
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: _leaving || !c.connected,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) _hangUp();
      },
      child: Scaffold(
        backgroundColor: const Color(0xFF202124),
        body: ListenableBuilder(
          listenable: c,
          builder: (context, _) {
            if (c.fatalError != null) return _ErrorView(message: c.fatalError!, onBack: () => Navigator.of(context).pop());
            if (!c.connected) return const Center(child: CircularProgressIndicator());
            return LayoutBuilder(builder: (context, box) {
              final wide = box.maxWidth >= 720;
              final stage = Column(children: [
                Expanded(child: _Stage(controller: c)),
                _ControlBar(controller: c, panelTab: _panelTab, onPanel: _togglePanel, onHangUp: _hangUp, onSettings: _settings),
              ]);
              final panel = _panelTab == null
                  ? null
                  : SidePanel(
                      controller: c,
                      initialTab: _panelTab!,
                      onClose: () => setState(() => _panelTab = null),
                      onShow: _openShow,
                    );
              if (wide) {
                return Row(children: [
                  Expanded(child: stage),
                  if (panel != null) SizedBox(width: min(380, box.maxWidth * 0.4), child: panel),
                ]);
              }
              // Phones: the panel covers the stage (keyboard-aware via the Scaffold).
              return Stack(children: [stage, if (panel != null) Positioned.fill(child: panel)]);
            });
          },
        ),
      ),
    );
  }
}

void _recordings(BuildContext context, MeetApi api, String callId) => Navigator.of(context)
    .push(MaterialPageRoute<void>(builder: (_) => RecordingsScreen(api: api, callId: callId)));

class _Stage extends StatelessWidget {
  final MeetController controller;
  const _Stage({required this.controller});

  @override
  Widget build(BuildContext context) {
    final tiles = controller.tiles;
    final big = tiles.where((t) => !t.pip).toList();
    // Your own window stays nearest the corner.
    final pips = tiles.where((t) => t.pip).toList()..sort((a, b) => (b.isLocal ? 1 : 0) - (a.isLocal ? 1 : 0));
    return LayoutBuilder(builder: (context, box) {
      final phone = box.maxWidth <= 600 || box.maxHeight <= 500;
      final solo = big.length == 1 && phone;
      final portrait = box.maxHeight > box.maxWidth;
      final n = max(big.length, 1);
      final cols = portrait ? (n <= 2 ? 1 : 2) : (n <= 1 ? 1 : n <= 4 ? 2 : 3);
      final rows = (n / cols).ceil();
      final pad = solo ? 0.0 : 8.0;
      final cellW = (box.maxWidth - pad * 2 - (cols - 1) * 8) / cols;
      final cellH = (box.maxHeight - pad * 2 - (rows - 1) * 8) / rows;
      final pipH = phone ? 120.0 : 150.0;
      final pipW = portrait ? pipH * 0.75 : min(box.maxWidth * 0.38, pipH * 16 / 9);
      final perCol = max(1, ((box.maxHeight - 12) / (pipH + 10)).floor());
      return Stack(children: [
        Padding(
          padding: EdgeInsets.all(pad),
          child: Wrap(
            spacing: 8,
            runSpacing: 8,
            alignment: WrapAlignment.center,
            runAlignment: WrapAlignment.center,
            children: [
              for (final t in big)
                SizedBox(
                  key: ValueKey('big-${t.identity}'),
                  width: cellW,
                  height: cellH,
                  child: ParticipantTile(tile: t, controller: controller, solo: solo),
                ),
            ],
          ),
        ),
        for (var i = 0; i < pips.length; i++)
          Positioned(
            key: ValueKey('pip-${pips[i].identity}'),
            right: 12 + (i ~/ perCol) * (pipW + 10),
            bottom: 12 + (i % perCol) * (pipH + 10),
            width: pipW,
            height: pipH,
            child: ParticipantTile(tile: pips[i], controller: controller),
          ),
        if (controller.showEveryoneVisible)
          Positioned(
            left: 12,
            bottom: 12,
            child: FilledButton.tonalIcon(
              key: const Key('showAllBtn'),
              onPressed: () => controller.setFocus(null),
              icon: const Icon(Icons.grid_view),
              label: const Text('Show everyone'),
            ),
          ),
        if (controller.recording.recording)
          Positioned(
            top: MediaQuery.paddingOf(context).top + 8,
            left: 10,
            child: Container(
              key: const Key('recBadge'),
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
              decoration: BoxDecoration(color: const Color(0xFFD93025), borderRadius: BorderRadius.circular(6)),
              child: const Text('REC', style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
            ),
          ),
      ]);
    });
  }
}

class _ControlBar extends StatelessWidget {
  final MeetController controller;
  final int? panelTab;
  final ValueChanged<int> onPanel;
  final VoidCallback onHangUp;
  final VoidCallback onSettings;
  const _ControlBar({
    required this.controller,
    required this.panelTab,
    required this.onPanel,
    required this.onHangUp,
    required this.onSettings,
  });

  @override
  Widget build(BuildContext context) {
    final c = controller;
    final noneEnabled = !c.micEnabled && !c.camEnabled;
    final off = c.muted || noneEnabled;
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Wrap(alignment: WrapAlignment.center, spacing: 10, runSpacing: 8, children: [
          _Btn(
            key: const Key('muteAllBtn'),
            icon: off ? Icons.mic_off : Icons.mic,
            tooltip: noneEnabled
                ? 'Microphone and camera are turned off in Settings'
                : c.muted
                    ? 'Unmute'
                    : 'Mute',
            danger: off,
            onPressed: noneEnabled ? null : c.toggleMute,
          ),
          _Btn(key: const Key('hangupBtn'), icon: Icons.call_end, tooltip: 'Leave call', red: true, onPressed: onHangUp),
          if (c.recordingEnabled)
            _Btn(
              key: const Key('recBtn'),
              icon: Icons.fiber_manual_record,
              tooltip: c.recording.recording ? 'Stop recording' : 'Start recording',
              danger: c.recording.recording,
              onPressed: c.toggleRecording,
            ),
          _Btn(key: const Key('peopleBtn'), icon: Icons.people, tooltip: 'People', active: panelTab == 0,
              badge: '${c.tiles.length}', onPressed: () => onPanel(0)),
          _Btn(key: const Key('chatBtn'), icon: Icons.chat, tooltip: 'Chat', active: panelTab == 1,
              badge: c.unread > 0 ? '${c.unread}' : null, badgeAlert: true, onPressed: () => onPanel(1)),
          _Btn(key: const Key('settingsBtn'), icon: Icons.settings, tooltip: 'Settings', onPressed: onSettings),
        ]),
      ),
    );
  }
}

class _Btn extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;
  final bool red, danger, active, badgeAlert;
  final String? badge;
  const _Btn({
    super.key,
    required this.icon,
    required this.tooltip,
    this.onPressed,
    this.red = false,
    this.danger = false,
    this.active = false,
    this.badge,
    this.badgeAlert = false,
  });

  @override
  Widget build(BuildContext context) {
    final bg = red
        ? const Color(0xFFD93025)
        : danger
            ? const Color(0xFFF28B82)
            : active
                ? const Color(0xFF8AB4F8)
                : const Color(0xFF3C4043);
    final fg = danger || active ? const Color(0xFF202124) : Colors.white;
    final button = Tooltip(
      message: tooltip,
      child: Material(
        color: bg,
        shape: const StadiumBorder(),
        child: InkWell(
          customBorder: const StadiumBorder(),
          onTap: onPressed,
          child: SizedBox(width: red ? 64 : 48, height: 48, child: Icon(icon, color: onPressed == null ? Colors.white38 : fg)),
        ),
      ),
    );
    if (badge == null) return button;
    return Badge(
      label: Text(badge!),
      backgroundColor: badgeAlert ? const Color(0xFFD93025) : const Color(0xFF5F6368),
      child: button,
    );
  }
}

class _ErrorView extends StatelessWidget {
  final String message;
  final VoidCallback onBack;
  const _ErrorView({required this.message, required this.onBack});

  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text(message, style: const TextStyle(color: Colors.white), textAlign: TextAlign.center),
            const SizedBox(height: 16),
            FilledButton(onPressed: onBack, child: const Text('Back')),
          ]),
        ),
      );
}

/// Shared with the lobby.
void openRecordings(BuildContext context, MeetApi api, String callId) => _recordings(context, api, callId);
