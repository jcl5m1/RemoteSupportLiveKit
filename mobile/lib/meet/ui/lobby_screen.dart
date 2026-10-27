// Lobby for open meetings (web: #prejoin), same order as the web:
// "Ready to join?", camera preview, Name, Meeting code (+ copy), Join now,
// View recordings, Camera, then Microphone and Speaker side by side.

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:livekit_client/livekit_client.dart' hide ChatMessage;
import 'package:permission_handler/permission_handler.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../audio_route.dart';
import '../meet_api.dart';
import '../meet_controller.dart';
import '../protocol.dart';
import 'device_pickers.dart';
import 'meet_screen.dart';

class LobbyScreen extends StatefulWidget {
  final MeetApi api;

  /// Meeting code from a link; null = new meeting ("Start meeting").
  final String? initialCode;

  /// Opens the older two-party support flow (role select).
  final VoidCallback? onSupportSession;

  /// Tests: skip camera/mic capture and native audio routing.
  final bool previewEnabled;
  final AudioRouter? audio;

  const LobbyScreen({
    super.key,
    required this.api,
    this.initialCode,
    this.onSupportSession,
    this.previewEnabled = true,
    this.audio,
  });

  @override
  State<LobbyScreen> createState() => _LobbyScreenState();
}

class _LobbyScreenState extends State<LobbyScreen> {
  late final AudioRouter _audio = widget.audio ?? AudioRouter();
  final _name = TextEditingController();
  late final _code = TextEditingController(text: widget.initialCode ?? newCallCode());
  late final bool _fresh = widget.initialCode == null;
  bool _codeEdited = false;
  bool _copied = false;
  CallInfo? _info;
  Timer? _infoTimer;
  LocalVideoTrack? _preview;
  List<MediaDevice> _cams = const [];
  String? _camId;
  bool _joining = false;

  String get _callId => normalizeCallCode(_code.text);
  bool get _valid => callCodeRe.hasMatch(_callId);

  @override
  void initState() {
    super.initState();
    _init();
  }

  Future<void> _init() async {
    final prefs = await SharedPreferences.getInstance();
    _name.text = prefs.getString('meet.name') ?? '';
    _camId = prefs.getString('device.videoinput');
    _refreshInfo();
    if (widget.previewEnabled) {
      await [Permission.camera, Permission.microphone].request();
      await _startPreview();
      await _audio.start();
    }
    if (mounted) setState(() {});
  }

  @override
  void didUpdateWidget(LobbyScreen old) {
    super.didUpdateWidget(old);
    // A new deep link while the lobby is open.
    if (widget.initialCode != null && widget.initialCode != old.initialCode) {
      _code.text = widget.initialCode!;
      _codeEdited = true;
      _refreshInfo();
    }
  }

  Future<void> _startPreview() async {
    if (!widget.previewEnabled || _preview != null) return;
    final cams = await Hardware.instance.videoInputs();
    final id = cams.any((c) => c.deviceId == _camId) ? _camId : null;
    try {
      _preview = await LocalVideoTrack.createCameraTrack(
        CameraCaptureOptions(params: cameraParams, deviceId: id),
      );
    } catch (_) {
      _preview = null; // no camera / denied: the lobby still works
    }
    if (mounted) setState(() => _cams = cams);
  }

  Future<void> _stopPreview() async {
    final p = _preview;
    _preview = null;
    await p?.stop();
    await p?.dispose();
  }

  void _refreshInfo() {
    _infoTimer?.cancel();
    if (!_valid) {
      setState(() => _info = null);
      return;
    }
    final id = _callId;
    _infoTimer = Timer(const Duration(milliseconds: 300), () async {
      try {
        final info = await widget.api.info(id);
        if (mounted && id == _callId) setState(() => _info = info);
      } catch (_) {}
    });
  }

  @override
  void dispose() {
    _infoTimer?.cancel();
    _name.dispose();
    _code.dispose();
    unawaited(_stopPreview());
    if (widget.audio == null) _audio.dispose();
    super.dispose();
  }

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.api.meetingLink(_callId)));
    setState(() => _copied = true);
    Timer(const Duration(milliseconds: 1500), () {
      if (mounted) setState(() => _copied = false);
    });
  }

  Future<void> _join() async {
    if (!_valid || _joining) return;
    final name = _name.text.trim().isEmpty ? 'Guest' : _name.text.trim();
    (await SharedPreferences.getInstance()).setString('meet.name', _name.text.trim());
    setState(() => _joining = true);
    await _stopPreview(); // the call takes over the camera
    final controller = MeetController(api: widget.api, callId: _callId, displayName: name, audio: _audio);
    if (!mounted) return;
    await Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => MeetScreen(controller: controller)));
    // Back from the call (the lobby keeps the shared audio router).
    controller.dispose();
    if (!mounted) return;
    setState(() => _joining = false);
    _refreshInfo();
    await _startPreview();
  }

  @override
  Widget build(BuildContext context) {
    final starting = _fresh && !_codeEdited;
    const label = TextStyle(fontSize: 12, color: Color(0xFF9AA0A6));
    return Scaffold(
      backgroundColor: const Color(0xFF202124),
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(16),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Card(
                color: const Color(0xFF292A2D),
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
                child: Padding(
                  padding: const EdgeInsets.all(20),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                    Text(
                      starting ? 'Start a new meeting' : 'Ready to join?',
                      key: const Key('prejoinTitle'),
                      textAlign: TextAlign.center,
                      style: const TextStyle(fontSize: 22, color: Colors.white),
                    ),
                    const SizedBox(height: 12),
                    AspectRatio(
                      aspectRatio: 16 / 9,
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(10),
                        child: Container(
                          key: const Key('prejoinPreview'),
                          color: Colors.black,
                          child: _preview == null
                              ? const Center(child: Icon(Icons.videocam_off, color: Colors.white38, size: 40))
                              : VideoTrackRenderer(_preview!, renderMode: VideoRenderMode.texture),
                        ),
                      ),
                    ),
                    const SizedBox(height: 16),
                    const Text('Name', key: Key('nameField'), style: label),
                    const SizedBox(height: 4),
                    TextField(
                      key: const Key('nameInput'),
                      controller: _name,
                      maxLength: 64,
                      textInputAction: TextInputAction.next,
                      autofillHints: const [AutofillHints.name],
                      decoration: const InputDecoration(
                          hintText: 'Your name', counterText: '', isDense: true, border: OutlineInputBorder()),
                    ),
                    const SizedBox(height: 12),
                    const Text('Meeting code', key: Key('codeField'), style: label),
                    const SizedBox(height: 4),
                    Row(children: [
                      Expanded(
                        child: TextField(
                          key: const Key('callCodeInput'),
                          controller: _code,
                          maxLength: 64,
                          autocorrect: false,
                          enableSuggestions: false,
                          style: const TextStyle(fontFamily: 'monospace', fontSize: 17),
                          decoration: InputDecoration(
                            hintText: 'e.g. team-standup',
                            counterText: '',
                            isDense: true,
                            border: const OutlineInputBorder(),
                            errorText: _code.text.isEmpty || _valid
                                ? null
                                : 'Use 3–64 letters, digits, or dashes (not starting or ending with a dash).',
                            errorMaxLines: 2,
                          ),
                          onChanged: (v) {
                            // Pasting a whole meeting link keeps just the code.
                            final fromLink = callCodeFromText(v);
                            if (fromLink != null && fromLink != v && (v.contains('/') || v.contains('?'))) {
                              _code.text = fromLink;
                            }
                            setState(() => _codeEdited = true);
                            _refreshInfo();
                          },
                        ),
                      ),
                      const SizedBox(width: 8),
                      IconButton.outlined(
                        key: const Key('copyCodeBtn'),
                        tooltip: 'Copy meeting link',
                        onPressed: _valid ? _copy : null,
                        icon: Icon(_copied ? Icons.check : Icons.copy),
                      ),
                    ]),
                    if (_info?.recordingActive ?? false) ...[
                      const SizedBox(height: 10),
                      const Text('● This call is being recorded.',
                          key: Key('recNotice'), style: TextStyle(color: Color(0xFFF28B82))),
                    ],
                    const SizedBox(height: 16),
                    FilledButton(
                      key: const Key('joinCallBtn'),
                      onPressed: _valid && !_joining ? _join : null,
                      style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(44)),
                      child: Text(starting ? 'Start meeting' : 'Join now'),
                    ),
                    if (_info?.recordingEnabled ?? false) ...[
                      const SizedBox(height: 10),
                      OutlinedButton(
                        key: const Key('viewRecsBtn'),
                        onPressed: () => openRecordings(context, widget.api, _callId),
                        style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(44)),
                        child: const Text('View recordings'),
                      ),
                    ],
                    const SizedBox(height: 18),
                    DevicePickers(
                      audio: _audio,
                      cameras: _cams,
                      cameraId: _camId,
                      onCamera: (d) async {
                        (await SharedPreferences.getInstance()).setString('device.videoinput', d.deviceId);
                        setState(() => _camId = d.deviceId);
                        await _stopPreview();
                        await _startPreview();
                      },
                    ),
                    if (widget.onSupportSession != null) ...[
                      const SizedBox(height: 12),
                      TextButton(onPressed: widget.onSupportSession, child: const Text('Remote support session')),
                    ],
                  ]),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
