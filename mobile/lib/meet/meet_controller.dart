// Open "meet" call: the native counterpart of the web client's open-call mode
// (backend/app/static/support-web/app.js). Same server API, same data-channel
// protocol (protocol.dart), same behaviour: layout focus, chat + "/show",
// pointer, recording, remote phone camera control, interaction log.

import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:livekit_client/livekit_client.dart' hide ChatMessage;
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wakelock_plus/wakelock_plus.dart';

import 'audio_route.dart';
import 'event_log.dart';
import 'meet_api.dart';
import 'protocol.dart';

/// Small (floating) remote videos are received at ~1/4 frame rate; big ones
/// ask for everything (an unset fps is ignored by the SFU).
const pipFps = 7;
const fullFps = 60;

/// Normal camera capture; photos briefly raise it to [photoParams].
const cameraParams = VideoParametersPresets.h720_169;
const photoParams = VideoParameters(dimensions: VideoDimensions(4032, 3024));

class TileModel {
  final String identity;
  final String name;
  final bool isLocal;
  final VideoTrack? video;
  final bool videoOn;
  final bool micOn;
  final bool speaking;
  final String color;
  final bool mirrored;
  final bool pip;
  const TileModel({
    required this.identity,
    required this.name,
    required this.isLocal,
    required this.video,
    required this.videoOn,
    required this.micOn,
    required this.speaking,
    required this.color,
    required this.mirrored,
    required this.pip,
  });
}

/// A pointer ring (on [identity]'s video) or a border flash (on the pointer's).
class TileEffect {
  final String identity;
  final bool ring;
  final double x;
  final double y;
  final String color;
  final String? from;
  const TileEffect.ring(this.identity, this.x, this.y, this.color, this.from) : ring = true;
  const TileEffect.flash(this.identity, this.color)
      : ring = false,
        x = 0,
        y = 0,
        from = null;
}

class DataLogEntry {
  final DateTime at;
  final bool outgoing;
  final String source;
  final String? topic;
  final String description;
  final int size;
  const DataLogEntry(this.at, this.outgoing, this.source, this.topic, this.description, this.size);
}

class Notice {
  final String text;
  final bool error;
  const Notice(this.text, {this.error = false});
}

class ShowRequest {
  final ShowCard card;
  final String from;
  const ShowRequest(this.card, this.from);
}

class _Pending {
  final String op;
  final String identity;
  final Timer timer;
  _Pending(this.op, this.identity, this.timer);
}

class MeetController extends ChangeNotifier {
  final MeetApi api;
  final String callId;
  final String displayName;
  final AudioRouter audio;
  final bool isPhone;

  MeetController({
    required this.api,
    required this.callId,
    required this.displayName,
    AudioRouter? audio,
    bool? isPhone,
  })  : audio = audio ?? AudioRouter(),
        _ownsAudio = audio == null,
        isPhone = isPhone ?? (!kIsWeb && (Platform.isAndroid || Platform.isIOS));

  final bool _ownsAudio;
  bool _disposed = false;

  @override
  void notifyListeners() {
    if (!_disposed) super.notifyListeners();
  }

  Room? room;
  EventsListener<RoomEvent>? _listener;
  JoinResult? _join;
  CallInfo? info;
  EventLog? _events;

  bool connecting = false;
  bool connected = false;
  String? fatalError;

  // Settings (persisted) and the mute button (secondary to settings).
  bool micEnabled = true;
  bool camEnabled = true;
  bool muted = false;

  String? focusIdentity;
  RecordingMeta recording = const RecordingMeta();
  final chat = <ChatMessage>[];
  int unread = 0;
  bool chatVisible = false;
  final dataLog = <DataLogEntry>[];
  final remoteCams = <String, CamState>{};
  final _pending = <String, _Pending>{};
  bool torchOn = false;
  String _lastCamState = '';
  Map<String, String> colors = {};
  Set<String> _speaking = {};

  final _effects = StreamController<TileEffect>.broadcast();
  final _notices = StreamController<Notice>.broadcast();
  final _shows = StreamController<ShowRequest>.broadcast();
  final _rand = Random();

  Stream<TileEffect> get effects => _effects.stream;
  Stream<Notice> get notices => _notices.stream;
  Stream<ShowRequest> get shows => _shows.stream;

  String? get identity => room?.localParticipant?.identity;
  String? get token => _join?.token;
  bool get recordingEnabled => _join?.recordingEnabled ?? info?.recordingEnabled ?? false;
  String get meetingLink => api.meetingLink(callId);

  // --------------------------------------------------------------------------
  // Connect / leave

  Future<void> connect() async {
    if (connecting || connected) return;
    connecting = true;
    fatalError = null;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    micEnabled = prefs.getBool('media.mic') ?? true;
    camEnabled = prefs.getBool('media.cam') ?? true;
    try {
      info = await api.info(callId).catchError((_) => CallInfo(callId: callId));
      final join = await api.join(callId, displayName);
      _join = join;
      _events = EventLog((events, sentAt) => api.postEvents(callId, join.token, events, sentAt));
      final r = Room(
        roomOptions: const RoomOptions(
          adaptiveStream: true,
          dynacast: true,
          defaultCameraCaptureOptions: CameraCaptureOptions(params: cameraParams),
          // Mute without stopping capture: on iOS a stopped mic switches the
          // session to playback, which can't use the earpiece or a Bluetooth
          // call headset, so audio would jump to the loudspeaker.
          defaultAudioCaptureOptions: AudioCaptureOptions(stopAudioCaptureOnMute: false),
          defaultVideoPublishOptions: VideoPublishOptions(simulcast: true),
        ),
      );
      room = r;
      _listen(r);
      await r.connect(join.wsUrl, join.token);
      if (_disposed) {
        // Left the screen while connecting.
        await r.disconnect();
        await r.dispose();
        return;
      }
      connected = true;
      if (!kIsWeb && Platform.isIOS) {
        try {
          await AudioManager.instance.setMicrophoneMuteMode(MicrophoneMuteMode.inputMixer); // silent, fast
        } catch (_) {}
      }
      await _applyMediaState();
      // LiveKit activates its audio session (and default route) once audio
      // flows; re-apply the user's mic/speaker choice after that.
      await audio.start();
      await audio.setActive(true);
      Timer(const Duration(milliseconds: 1500), () {
        if (connected) audio.refresh(reapply: true);
      });
      recording = RecordingMeta.parse(r.metadata);
      await WakelockPlus.enable();
      _refreshColors();
      unawaited(_announceCamState());
    } catch (e) {
      fatalError = e is MeetApiException ? e.message : 'Could not join: $e';
      await room?.dispose();
      room = null;
    } finally {
      connecting = false;
      notifyListeners();
    }
  }

  /// Hang up. The last person to leave ends the meeting (stops recording and
  /// closes the room right away).
  Future<void> leave() async {
    final r = room;
    if (r == null) return;
    final last = !r.remoteParticipants.values.any((p) => !p.identity.startsWith('agent'));
    _log('leave', {'last': last});
    await _events?.flush();
    if (last && token != null) {
      try {
        await api.end(callId, token!);
      } catch (_) {}
    }
    await _teardown();
  }

  Future<void> _teardown() async {
    final r = room;
    room = null;
    connected = false;
    for (final p in _pending.values) {
      p.timer.cancel();
    }
    _pending.clear();
    await _listener?.dispose();
    _listener = null;
    await r?.disconnect();
    await r?.dispose();
    await audio.setActive(false); // release the call route (SCO, comm device)
    await WakelockPlus.disable();
    notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    unawaited(_teardown());
    _events?.dispose();
    if (_ownsAudio) audio.dispose();
    _effects.close();
    _notices.close();
    _shows.close();
    super.dispose();
  }

  void _listen(Room r) {
    final l = r.createListener();
    _listener = l;
    l
      ..on<ParticipantConnectedEvent>((e) {
        _refreshColors();
        // Tell the newcomer what this phone's camera can do.
        Timer(const Duration(seconds: 1), () => _announceCamState(to: e.participant.identity));
        notifyListeners();
      })
      ..on<ParticipantDisconnectedEvent>((e) {
        remoteCams.remove(e.participant.identity);
        if (focusIdentity == e.participant.identity) focusIdentity = null;
        _refreshColors();
        notifyListeners();
      })
      ..on<TrackSubscribedEvent>((_) {
        _applyTileQuality();
        notifyListeners();
      })
      ..on<TrackUnsubscribedEvent>((_) => notifyListeners())
      ..on<TrackMutedEvent>((_) => notifyListeners())
      ..on<TrackUnmutedEvent>((_) => notifyListeners())
      ..on<LocalTrackPublishedEvent>((_) {
        unawaited(_announceCamState());
        if (recording.recording) unawaited(_recordingRequest('sync'));
        notifyListeners();
      })
      ..on<LocalTrackUnpublishedEvent>((_) {
        unawaited(_announceCamState());
        notifyListeners();
      })
      ..on<ParticipantNameUpdatedEvent>((_) => notifyListeners())
      ..on<ActiveSpeakersChangedEvent>((e) {
        _speaking = e.speakers.map((p) => p.identity).toSet();
        notifyListeners();
      })
      ..on<RoomMetadataChangedEvent>((e) => _applyRecording(RecordingMeta.parse(e.metadata)))
      ..on<DataReceivedEvent>(_onData)
      ..on<RoomReconnectingEvent>((_) => _notice('Reconnecting…'))
      ..on<RoomReconnectedEvent>((_) => _notice('Reconnected'))
      ..on<RoomDisconnectedEvent>((e) {
        if (room != null) {
          _notice('Disconnected${e.reason != null ? ' (${e.reason!.name})' : ''}', error: true);
          unawaited(_teardown());
        }
      });
  }

  // --------------------------------------------------------------------------
  // Tiles and layout (same rules as the web's layoutGrid)

  List<Participant> get _everyone {
    final r = room;
    if (r == null) return const [];
    return [if (r.localParticipant != null) r.localParticipant!, ...r.remoteParticipants.values];
  }

  List<TileModel> get tiles {
    final r = room;
    final local = r?.localParticipant;
    if (r == null || local == null) return const [];
    final others = r.remoteParticipants.values.map((p) => p.identity).toList();
    if (focusIdentity != null &&
        (!_everyone.any((p) => p.identity == focusIdentity) ||
            (others.length == 1 && focusIdentity == others.first) ||
            others.isEmpty)) {
      focusIdentity = null;
    }
    final big = focusIdentity != null ? [focusIdentity!] : (others.isNotEmpty ? others : [local.identity]);
    return [
      for (final p in _everyone)
        TileModel(
          identity: p.identity,
          name: p.name.isNotEmpty ? p.name : p.identity,
          isLocal: p is LocalParticipant,
          video: _videoTrack(p),
          videoOn: _videoTrack(p) != null && !(_videoPub(p)?.muted ?? true),
          micOn: p.isMicrophoneEnabled(),
          speaking: _speaking.contains(p.identity),
          color: colors[p.identity] ?? participantColors.first,
          mirrored: p is LocalParticipant && _isFrontCamera(),
          pip: !big.contains(p.identity),
        ),
    ];
  }

  int get bigCount => tiles.where((t) => !t.pip).length;
  bool get showEveryoneVisible => focusIdentity != null && (room?.remoteParticipants.length ?? 0) >= 2;

  TrackPublication? _videoPub(Participant p) =>
      p.videoTrackPublications.where((t) => t.source == TrackSource.camera).firstOrNull;

  VideoTrack? _videoTrack(Participant p) {
    final t = _videoPub(p)?.track;
    return t is VideoTrack ? t : null;
  }

  LocalVideoTrack? get _localCamera {
    final t = room?.localParticipant?.videoTrackPublications
        .where((t) => t.source == TrackSource.camera)
        .firstOrNull
        ?.track;
    return t is LocalVideoTrack ? t : null;
  }

  bool _isFrontCamera() {
    final t = _localCamera;
    if (t == null) return false;
    final opts = t.currentOptions;
    final facing = t.mediaStreamTrack.getSettings()['facingMode'];
    if (facing is String) return facing == 'user';
    return opts is CameraCaptureOptions && opts.deviceId == null && opts.cameraPosition == CameraPosition.front;
  }

  /// Tap on a tile: a small window or one of several big tiles is maximized;
  /// returns false when the tap should point instead (the single big view).
  bool tapTile(TileModel t) {
    if (t.pip || bigCount > 1) {
      setFocus(t.identity);
      return true;
    }
    return false;
  }

  void setFocus(String? identity) {
    focusIdentity = identity;
    _log('stage_focus', {
      'focus': identity == this.identity ? 'self' : 'remote',
      'target': identity,
    });
    _applyTileQuality();
    notifyListeners();
  }

  void _applyTileQuality() {
    final r = room;
    if (r == null) return;
    final small = {for (final t in tiles) if (t.pip) t.identity};
    for (final p in r.remoteParticipants.values) {
      final pub = p.videoTrackPublications.where((t) => t.source == TrackSource.camera).firstOrNull;
      if (pub == null || !pub.subscribed) continue;
      unawaited(pub.setVideoFPS(small.contains(p.identity) ? pipFps : fullFps));
    }
  }

  void _refreshColors() {
    colors = assignColors(_everyone.map((p) => ColorEntry(p.identity, p.joinedAt)));
  }

  // --------------------------------------------------------------------------
  // Microphone / camera. Settings switches are primary; mute pauses those on.

  Future<void> setMicEnabled(bool on) async {
    micEnabled = on;
    (await SharedPreferences.getInstance()).setBool('media.mic', on);
    _log('media_toggle', {'kind': 'mic', 'enabled': on});
    await _applyMediaState();
  }

  Future<void> setCamEnabled(bool on) async {
    camEnabled = on;
    (await SharedPreferences.getInstance()).setBool('media.cam', on);
    _log('media_toggle', {'kind': 'cam', 'enabled': on});
    await _applyMediaState();
  }

  Future<void> toggleMute() async {
    muted = !muted;
    _log('mute', {'muted': muted});
    await _applyMediaState();
  }

  Future<void> _applyMediaState() async {
    final lp = room?.localParticipant;
    if (lp == null) return;
    final prefs = await SharedPreferences.getInstance();
    final camId = prefs.getString('device.videoinput');
    try {
      await lp.setMicrophoneEnabled(micEnabled && !muted);
    } catch (e) {
      _notice('Could not start microphone: $e', error: true);
    }
    try {
      await lp.setCameraEnabled(
        camEnabled && !muted,
        cameraCaptureOptions: CameraCaptureOptions(params: cameraParams, deviceId: camId),
      );
    } catch (e) {
      _notice('Could not start camera: $e', error: true);
    }
    notifyListeners();
    unawaited(_announceCamState());
  }

  Future<List<MediaDevice>> videoInputs() => Hardware.instance.videoInputs();

  Future<void> selectCamera(MediaDevice d) async {
    (await SharedPreferences.getInstance()).setString('device.videoinput', d.deviceId);
    final cam = _localCamera;
    if (cam != null) {
      await cam.switchCamera(d.deviceId);
      torchOn = false;
    }
    _log('device_switch', {'kind': 'videoinput'});
    notifyListeners();
    unawaited(_announceCamState());
  }

  /// Front -> rear -> USB...: the switch button on your own big view.
  Future<void> cycleCamera() async {
    final cams = await videoInputs();
    final cam = _localCamera;
    if (cams.length < 2 || cam == null) return;
    final current = (cam.currentOptions as CameraCaptureOptions?)?.deviceId ??
        (await SharedPreferences.getInstance()).getString('device.videoinput');
    var idx = cams.indexWhere((d) => d.deviceId == current);
    if (idx < 0) {
      // Never picked one: the default is the front camera (or the first).
      // Labels: Android "..., Facing front, ...", iOS "Front Camera".
      final front = cams.indexWhere((d) => d.label.toLowerCase().contains('front'));
      idx = front < 0 ? 0 : front;
    }
    final next = cams[(idx + 1) % cams.length];
    try {
      await selectCamera(next);
      _log('camera_switch', {'label': next.label});
      _notice('Camera: ${next.label}');
    } catch (e) {
      _notice('Could not switch camera: $e', error: true);
    }
  }

  // --------------------------------------------------------------------------
  // Recording

  void _applyRecording(RecordingMeta meta) {
    final changed = meta.recording != recording.recording;
    recording = meta;
    if (changed) {
      final who = meta.recording ? meta.startedBy : meta.stoppedBy;
      _notice('Recording ${meta.recording ? 'started' : 'stopped'}${who != null ? ' by $who' : ''}');
    }
    if (meta.recording && meta.skipped > 0) {
      _notice('Recording limit reached: ${meta.skipped} not being recorded', error: true);
    }
    notifyListeners();
  }

  Future<void> toggleRecording() async {
    try {
      final on = await _recordingRequest(recording.recording ? 'stop' : 'start');
      _applyRecording(RecordingMeta(recording: on, startedBy: on ? displayName : null, stoppedBy: on ? null : displayName));
    } catch (e) {
      _notice('Recording: $e', error: true);
    }
  }

  Future<bool> _recordingRequest(String action) => api.recording(callId, token!, action);

  // --------------------------------------------------------------------------
  // Data channel

  Future<void> _publish(List<int> payload, String topic, {List<String>? to}) async {
    final lp = room?.localParticipant;
    if (lp == null) return;
    await lp.publishData(payload, reliable: true, topic: topic, destinationIdentities: to);
    _logData(true, 'You', topic, payload);
  }

  void _logData(bool out, String source, String? topic, List<int> payload) {
    dataLog.add(DataLogEntry(DateTime.now(), out, source, topic, describePayload(topic, payload), payload.length));
    if (dataLog.length > 500) dataLog.removeRange(0, dataLog.length - 500);
  }

  void _onData(DataReceivedEvent e) {
    final p = e.participant;
    final name = p == null ? 'Server' : (p.name.isNotEmpty ? p.name : p.identity);
    _logData(false, name, e.topic, e.data);
    switch (e.topic) {
      case pointerTopic:
        _onPointer(e.data, p);
      case camctlTopic:
        if (p != null) unawaited(_onCamCtl(e.data, p));
      case chatTopic:
        final msg = decodeChat(e.data, identity: p?.identity ?? 'unknown', name: name);
        if (msg == null) return;
        _addChat(msg);
        if (msg.show != null) _shows.add(ShowRequest(msg.show!, name));
    }
    notifyListeners();
  }

  // --------------------------------------------------------------------------
  // Chat and "/show"

  void _addChat(ChatMessage m) {
    chat.add(m);
    if (!m.mine) {
      if (!chatVisible) unread++;
      SystemSound.play(SystemSoundType.alert);
      HapticFeedback.lightImpact();
    }
    notifyListeners();
  }

  void setChatVisible(bool visible) {
    chatVisible = visible;
    if (visible) unread = 0;
    notifyListeners();
  }

  /// Returns false (keep the text) when a "/show" link can't be shown.
  Future<bool> sendChat(String text) async {
    final trimmed = text.trim();
    if (trimmed.isEmpty || room?.localParticipant == null) return false;
    ShowCard? show;
    final m = showRe.firstMatch(trimmed);
    if (m != null) {
      var url = m.group(1)!;
      if (!RegExp(r'^[a-z][a-z0-9+.-]*:', caseSensitive: false).hasMatch(url)) url = 'https://$url';
      try {
        show = await api.mediaCheck(callId, token!, url);
      } catch (e) {
        _notice("Can't show ${m.group(1)}: $e", error: true);
        return false;
      }
    }
    final ts = DateTime.now().millisecondsSinceEpoch;
    await _publish(encodeChat(text: trimmed, name: displayName, ts: ts, show: show), chatTopic);
    _log('chat', {'text': trimmed, if (show != null) 'show': show.toJson()});
    _addChat(ChatMessage(identity: identity ?? '', name: displayName, text: trimmed, ts: ts, mine: true, show: show));
    return true;
  }

  void logShowClosed(ShowCard card) => _log('show_close', {'kind': card.kind});

  // --------------------------------------------------------------------------
  // Pointer

  /// [x], [y]: normalized, unmirrored frame coordinates on [target]'s video.
  Future<void> sendPointer(String target, double x, double y) async {
    final me = identity;
    if (me == null) return;
    final color = colors[me] ?? participantColors.first;
    _effect(TileEffect.ring(target, x, y, color, 'You'));
    _effect(TileEffect.flash(me, color));
    try {
      await _publish(
          encodePointer(target: target, x: x, y: y, color: color, ts: DateTime.now().millisecondsSinceEpoch),
          pointerTopic);
      _log('pointer', {'target': target, 'x': _r4(x), 'y': _r4(y), 'color': color});
    } catch (_) {}
  }

  void _onPointer(List<int> payload, RemoteParticipant? from) {
    final msg = decodePointer(payload);
    if (msg == null || !_everyone.any((p) => p.identity == msg.target)) return;
    final color = msg.color ?? colors[from?.identity] ?? participantColors.first;
    final fromName = from == null ? 'Someone' : (from.name.isNotEmpty ? from.name : from.identity);
    if (from != null) _effect(TileEffect.flash(from.identity, color));
    _effect(TileEffect.ring(msg.target, msg.x, msg.y, color, fromName));
    if (msg.target == identity) _notice('$fromName pointed at your camera');
  }

  // --------------------------------------------------------------------------
  // Remote phone camera control (topic "camctl")

  Future<void> _announceCamState({String? to}) async {
    if (!isPhone || !connected || room?.localParticipant == null) return;
    final cam = _localCamera;
    final lp = room!.localParticipant!;
    final live = cam != null && lp.isCameraEnabled() && !(cam.muted);
    var hasTorch = false;
    if (live) {
      try {
        hasTorch = await cam.mediaStreamTrack.hasTorch();
      } catch (_) {}
    }
    final state = CamState(
      phone: true,
      cam: live,
      cams: live ? (await videoInputs()).length : 0,
      torch: live && hasTorch,
      torchOn: live && torchOn,
    );
    final key = state.toJson().toString();
    if (to == null && key == _lastCamState) return;
    if (to == null) _lastCamState = key;
    try {
      await _publish(CamStateMsg(state).encode(), camctlTopic, to: to == null ? null : [to]);
    } catch (_) {}
  }

  Future<void> _onCamCtl(List<int> payload, RemoteParticipant from) async {
    final msg = CamCtl.decode(payload);
    final name = from.name.isNotEmpty ? from.name : from.identity;
    switch (msg) {
      case CamStateMsg(:final state):
        remoteCams[from.identity] = state;
        notifyListeners();
      case CamResult():
        final pending = _pending.remove(msg.id);
        if (pending == null || pending.identity != from.identity) return;
        pending.timer.cancel();
        if (!msg.ok) {
          _notice("$name's phone: ${msg.error ?? 'failed'}", error: true);
        } else if (pending.op == 'photo') {
          _notice('Photo received from $name');
        }
        notifyListeners();
      case CamCmd():
        if (!isPhone) return;
        var result = CamResult(msg.id, msg.op, ok: true);
        try {
          switch (msg.op) {
            case 'cycle':
              _notice('$name switched your camera');
              await cycleCamera();
            case 'torch':
              await _setTorch(msg.on);
              _notice('$name turned your flashlight ${torchOn ? 'on' : 'off'}');
            case 'photo':
              _notice('$name is taking a photo with your camera');
              _effect(TileEffect.flash(identity ?? '', '#FFFFFF'));
              final photo = await _takeAndSendPhoto(from);
              result = CamResult(msg.id, msg.op, ok: true, photo: photo.name);
              _notice('Photo sent to $name');
          }
        } catch (e) {
          result = CamResult(msg.id, msg.op, ok: false, error: '$e');
          _notice('Could not ${msg.op == 'photo' ? 'take photo' : msg.op == 'torch' ? 'switch flashlight' : 'switch camera'}: $e',
              error: true);
        }
        _log('camera_control', {'op': msg.op, 'by': from.identity, 'ok': result.ok});
        await _announceCamState();
        try {
          await _publish(result.encode(), camctlTopic, to: [from.identity]);
        } catch (_) {}
      case null:
        return;
    }
  }

  bool camBusy(String identity, String op) => _pending.values.any((p) => p.identity == identity && p.op == op);

  /// Viewer: switch / photo / flashlight on another caller's phone.
  Future<void> requestCam(String target, String op) async {
    final p = room?.remoteParticipants.values.where((p) => p.identity == target).firstOrNull;
    final state = remoteCams[target];
    if (p == null || state == null) return;
    final name = p.name.isNotEmpty ? p.name : p.identity;
    final id = '${DateTime.now().microsecondsSinceEpoch}-${_rand.nextInt(1 << 30)}';
    final on = !state.torchOn;
    final timer = Timer(Duration(seconds: op == 'photo' ? 60 : 15), () {
      _pending.remove(id);
      _notice("$name's phone did not respond", error: true);
      notifyListeners();
    });
    _pending[id] = _Pending(op, target, timer);
    if (op == 'photo') _notice("Taking a photo with $name's camera…");
    notifyListeners();
    try {
      await _publish(CamCmd(id, op, on: on).encode(), camctlTopic, to: [target]);
      _log('camera_control', {'op': op, 'target': target, if (op == 'torch') 'on': on});
    } catch (e) {
      timer.cancel();
      _pending.remove(id);
      _notice("Could not reach $name's phone: $e", error: true);
      notifyListeners();
    }
  }

  Future<void> _setTorch(bool on) async {
    final cam = _localCamera;
    if (cam == null || !await cam.mediaStreamTrack.hasTorch()) {
      throw Exception('this camera has no flashlight');
    }
    await cam.mediaStreamTrack.setTorch(on);
    torchOn = on;
    _log('torch', {'on': on});
  }

  /// Full-resolution still: briefly restart the camera at its largest size,
  /// grab a JPEG frame natively, then restore the normal call resolution.
  Future<({Uint8List bytes, int? width, int? height})> capturePhoto() async {
    final cam = _localCamera;
    if (cam == null || !(room?.localParticipant?.isCameraEnabled() ?? false)) {
      throw Exception('camera is off');
    }
    final before = cam.currentOptions;
    if (before is! CameraCaptureOptions) throw Exception('not a camera');
    final torch = torchOn;
    // restartTrack stops the camera before opening the new size, so whatever
    // happens the normal size must be restored afterwards.
    var raised = false;
    try {
      raised = true;
      await cam.restartTrack(before.copyWith(params: photoParams));
      if (torch) await cam.mediaStreamTrack.setTorch(true);
      await Future<void>.delayed(const Duration(milliseconds: 900)); // exposure/focus settle
    } catch (_) {
      // Can't go bigger: back to the normal size and take it at that.
      try {
        await cam.restartTrack(before);
        if (torch) await cam.mediaStreamTrack.setTorch(true);
        raised = false;
      } catch (e) {
        _notice('Camera could not restart after the photo: $e', error: true);
        rethrow;
      }
    }
    try {
      final dir = await getTemporaryDirectory();
      final path = '${dir.path}/meet-photo-${DateTime.now().millisecondsSinceEpoch}.jpg';
      await rtc.WebRTC.invokeMethod('captureFrame', <String, dynamic>{
        'trackId': cam.mediaStreamTrack.id,
        'peerConnectionId': 'local',
        'path': path,
      });
      final file = File(path);
      final bytes = await file.readAsBytes();
      unawaited(file.delete().catchError((_) => file));
      final size = jpegSize(bytes);
      return (bytes: bytes, width: size?.$1, height: size?.$2);
    } finally {
      if (raised) {
        try {
          await cam.restartTrack(before);
          if (torch) await cam.mediaStreamTrack.setTorch(true);
        } catch (e) {
          _notice('Camera could not restart after the photo: $e', error: true);
        }
      }
    }
  }

  Future<PhotoFile> _takeAndSendPhoto(RemoteParticipant requester) async {
    final shot = await capturePhoto();
    final ctype = isJpeg(shot.bytes) ? 'image/jpeg' : 'image/png';
    final photo = await api.uploadPhoto(callId, token!, shot.bytes,
        contentType: ctype, requestedBy: requester.identity, width: shot.width, height: shot.height);
    final url = api.absolute(photo.path);
    final card = ShowCard(kind: 'image', url: url, finalUrl: url, title: 'Photo from $displayName');
    await _publish(
      encodeChat(text: 'Photo from $displayName', name: displayName, ts: DateTime.now().millisecondsSinceEpoch, show: card),
      chatTopic,
      to: [requester.identity],
    );
    return photo;
  }

  // --------------------------------------------------------------------------

  void _log(String type, [Map<String, Object?> data = const {}]) => _events?.log(type, data);

  void _effect(TileEffect e) {
    if (!_effects.isClosed) _effects.add(e);
  }

  void _notice(String text, {bool error = false}) {
    if (!_notices.isClosed) _notices.add(Notice(text, error: error));
  }

  static double _r4(double v) => (v * 10000).roundToDouble() / 10000;
}

bool isJpeg(List<int> b) => b.length > 3 && b[0] == 0xFF && b[1] == 0xD8 && b[2] == 0xFF;

/// (width, height) from a JPEG's SOF marker, or null.
(int, int)? jpegSize(List<int> b) {
  if (!isJpeg(b)) return null;
  var i = 2;
  while (i + 9 < b.length) {
    if (b[i] != 0xFF) {
      i++;
      continue;
    }
    final marker = b[i + 1];
    final len = (b[i + 2] << 8) | b[i + 3];
    // SOF0..SOF15 except DHT (C4), JPG (C8), DAC (CC).
    if (marker >= 0xC0 && marker <= 0xCF && marker != 0xC4 && marker != 0xC8 && marker != 0xCC) {
      final h = (b[i + 5] << 8) | b[i + 6];
      final w = (b[i + 7] << 8) | b[i + 8];
      return (w, h);
    }
    if (marker == 0xD8 || marker == 0x01 || (marker >= 0xD0 && marker <= 0xD7)) {
      i += 2;
    } else {
      i += 2 + len;
    }
  }
  return null;
}
