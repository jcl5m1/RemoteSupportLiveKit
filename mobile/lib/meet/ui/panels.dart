// Side panel tabs (People / Chat / Data) and the Settings sheet.

import 'package:flutter/material.dart';
import 'package:livekit_client/livekit_client.dart' show MediaDevice;
import 'package:shared_preferences/shared_preferences.dart';

import '../meet_controller.dart';
import '../protocol.dart';
import 'device_pickers.dart';
import 'participant_tile.dart' show hexColor;

String _hm(DateTime t) =>
    '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}:${t.second.toString().padLeft(2, '0')}';

class SidePanel extends StatefulWidget {
  final MeetController controller;
  final int initialTab; // 0 people, 1 chat, 2 data
  final VoidCallback onClose;
  final void Function(ShowCard card, String from) onShow;
  const SidePanel({
    super.key,
    required this.controller,
    required this.initialTab,
    required this.onClose,
    required this.onShow,
  });

  @override
  State<SidePanel> createState() => _SidePanelState();
}

class _SidePanelState extends State<SidePanel> with SingleTickerProviderStateMixin {
  late final TabController _tabs =
      TabController(length: 3, vsync: this, initialIndex: widget.initialTab)..addListener(_onTab);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _onTab());
  }

  void _onTab() => widget.controller.setChatVisible(_tabs.index == 1);

  @override
  void didUpdateWidget(SidePanel old) {
    super.didUpdateWidget(old);
    if (old.initialTab != widget.initialTab) _tabs.animateTo(widget.initialTab);
  }

  @override
  void dispose() {
    widget.controller.chatVisible = false;
    _tabs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Material(
      color: const Color(0xFF292A2D),
      child: SafeArea(
        child: Column(children: [
          Row(children: [
            Expanded(
              child: TabBar(
                controller: _tabs,
                labelColor: Colors.white,
                unselectedLabelColor: Colors.white60,
                tabs: const [Tab(text: 'People'), Tab(text: 'Chat'), Tab(text: 'Data')],
              ),
            ),
            IconButton(
              key: const Key('panelClose'),
              tooltip: 'Close',
              color: Colors.white,
              icon: const Icon(Icons.close),
              onPressed: widget.onClose,
            ),
          ]),
          Expanded(
            child: ListenableBuilder(
              listenable: widget.controller,
              builder: (context, _) => TabBarView(controller: _tabs, children: [
                _People(c: widget.controller),
                ChatTab(c: widget.controller, onShow: widget.onShow),
                _DataTab(c: widget.controller),
              ]),
            ),
          ),
        ]),
      ),
    );
  }
}

class _People extends StatelessWidget {
  final MeetController c;
  const _People({required this.c});

  @override
  Widget build(BuildContext context) {
    final tiles = c.tiles;
    return ListView(children: [
      for (final t in tiles)
        ListTile(
          leading: CircleAvatar(
            backgroundColor: hexColor(t.color),
            child: Text(t.name.isNotEmpty ? t.name.characters.first.toUpperCase() : '?',
                style: const TextStyle(color: Colors.black)),
          ),
          title: Text(t.isLocal ? '${t.name} (You)' : t.name, style: const TextStyle(color: Colors.white)),
          subtitle: t.speaking ? const Text('Speaking', style: TextStyle(color: Color(0xFF8AB4F8))) : null,
          trailing: Icon(t.micOn ? Icons.mic : Icons.mic_off, color: t.micOn ? Colors.white70 : const Color(0xFFF28B82)),
        ),
    ]);
  }
}

class ChatTab extends StatefulWidget {
  final MeetController c;
  final void Function(ShowCard card, String from) onShow;
  const ChatTab({super.key, required this.c, required this.onShow});
  @override
  State<ChatTab> createState() => _ChatTabState();
}

class _ChatTabState extends State<ChatTab> {
  final _input = TextEditingController();
  final _scroll = ScrollController();
  bool _sending = false;

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    if (_sending) return;
    setState(() => _sending = true);
    final ok = await widget.c.sendChat(_input.text);
    if (!mounted) return;
    setState(() => _sending = false);
    if (ok) _input.clear();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) _scroll.jumpTo(_scroll.position.maxScrollExtent);
    });
  }

  @override
  Widget build(BuildContext context) {
    final msgs = widget.c.chat;
    return Column(children: [
      Expanded(
        child: msgs.isEmpty
            ? const Center(
                child: Padding(
                  padding: EdgeInsets.all(24),
                  child: Text('Messages are visible to everyone in the call. Type /show <link> to share a '
                      'picture, video, audio clip, map or page full screen.',
                      textAlign: TextAlign.center, style: TextStyle(color: Colors.white54)),
                ),
              )
            : ListView.builder(
                controller: _scroll,
                padding: const EdgeInsets.all(12),
                itemCount: msgs.length,
                itemBuilder: (context, i) => _Bubble(m: msgs[i], onShow: widget.onShow),
              ),
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
        child: TextField(
          key: const Key('chatInput'),
          controller: _input,
          style: const TextStyle(color: Colors.white),
          textInputAction: TextInputAction.send,
          onSubmitted: (_) => _send(),
          decoration: InputDecoration(
            hintText: 'Send a message',
            hintStyle: const TextStyle(color: Colors.white38),
            filled: true,
            fillColor: const Color(0xFF3C4043),
            border: OutlineInputBorder(borderRadius: BorderRadius.circular(24), borderSide: BorderSide.none),
            suffixIcon: _sending
                ? const Padding(padding: EdgeInsets.all(12), child: CircularProgressIndicator(strokeWidth: 2))
                : null,
          ),
        ),
      ),
    ]);
  }
}

class _Bubble extends StatelessWidget {
  final ChatMessage m;
  final void Function(ShowCard card, String from) onShow;
  const _Bubble({required this.m, required this.onShow});

  @override
  Widget build(BuildContext context) {
    final t = DateTime.fromMillisecondsSinceEpoch(m.ts);
    final show = m.show;
    return Align(
      alignment: m.mine ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        constraints: const BoxConstraints(maxWidth: 300),
        padding: const EdgeInsets.all(10),
        decoration: BoxDecoration(
          color: m.mine ? const Color(0xFF394457) : const Color(0xFF3C4043),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${m.mine ? 'You' : m.name} · ${_hm(t).substring(0, 5)}',
              style: const TextStyle(color: Colors.white54, fontSize: 11)),
          const SizedBox(height: 3),
          if (show == null)
            SelectableText(m.text, style: const TextStyle(color: Colors.white))
          else ...[
            Text('Shared ${show.label}', style: const TextStyle(color: Colors.white70, fontSize: 12)),
            Text(show.title ?? show.url,
                maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(color: Color(0xFF8AB4F8))),
            if (!m.mine)
              TextButton(onPressed: () => onShow(show, m.name), child: const Text('Show')),
          ],
        ]),
      ),
    );
  }
}

class _DataTab extends StatelessWidget {
  final MeetController c;
  const _DataTab({required this.c});

  @override
  Widget build(BuildContext context) {
    final log = c.dataLog.reversed.toList();
    if (log.isEmpty) {
      return const Center(child: Text('No data messages yet', style: TextStyle(color: Colors.white54)));
    }
    return ListView.separated(
      itemCount: log.length,
      separatorBuilder: (_, __) => const Divider(height: 1, color: Colors.white12),
      itemBuilder: (context, i) {
        final e = log[i];
        return ListTile(
          dense: true,
          leading: Icon(e.outgoing ? Icons.north_east : Icons.south_west, color: Colors.white54, size: 18),
          title: Text(e.description, style: const TextStyle(color: Colors.white)),
          subtitle: Text(
            '${_hm(e.at)} · ${e.outgoing ? 'to everyone' : 'from ${e.source}'}'
            '${e.topic != null ? ' · topic: ${e.topic}' : ''}',
            style: const TextStyle(color: Colors.white54, fontSize: 11),
          ),
          trailing: Text(fmtBytes(e.size), style: const TextStyle(color: Colors.white54, fontSize: 11)),
        );
      },
    );
  }
}

/// Settings: per-device on/off switches (primary; mute only pauses those on),
/// camera / microphone / speaker, recordings.
class SettingsSheet extends StatefulWidget {
  final MeetController controller;
  final VoidCallback onRecordings;
  const SettingsSheet({super.key, required this.controller, required this.onRecordings});
  @override
  State<SettingsSheet> createState() => _SettingsSheetState();
}

class _SettingsSheetState extends State<SettingsSheet> {
  List<MediaDevice> _cams = const [];
  String? _camId;

  @override
  void initState() {
    super.initState();
    _load();
    widget.controller.audio.refresh();
  }

  Future<void> _load() async {
    final cams = await widget.controller.videoInputs();
    final id = (await SharedPreferences.getInstance()).getString('device.videoinput');
    if (mounted) setState(() => (_cams = cams, _camId = id));
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.controller;
    return ListenableBuilder(
      listenable: c,
      builder: (context, _) => SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(20, 8, 20, 20),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Row(children: [
              const Expanded(child: Text('Settings', style: TextStyle(fontSize: 20))),
              IconButton(
                key: const Key('settingsClose'),
                tooltip: 'Close',
                icon: const Icon(Icons.close),
                onPressed: () => Navigator.of(context).pop(),
              ),
            ]),
            SwitchListTile(
              key: const Key('micSwitch'),
              contentPadding: EdgeInsets.zero,
              title: const Text('Microphone'),
              value: c.micEnabled,
              onChanged: c.setMicEnabled,
            ),
            SwitchListTile(
              key: const Key('camSwitch'),
              contentPadding: EdgeInsets.zero,
              title: const Text('Camera'),
              value: c.camEnabled,
              onChanged: c.setCamEnabled,
            ),
            const Text('Switched-off devices stay off; the mute button only pauses the ones switched on.',
                style: TextStyle(fontSize: 12, color: Color(0xFF9AA0A6))),
            const SizedBox(height: 16),
            DevicePickers(
              audio: c.audio,
              cameras: _cams,
              cameraId: _camId,
              onCamera: (d) async {
                setState(() => _camId = d.deviceId);
                await c.selectCamera(d);
              },
            ),
            if (c.recordingEnabled) ...[
              const SizedBox(height: 16),
              OutlinedButton(onPressed: widget.onRecordings, child: const Text('View recordings')),
            ],
          ]),
        ),
      ),
    );
  }
}
