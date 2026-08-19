// Owns the LiveKit Room. See docs/07-flutter-app.md.

import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:livekit_client/livekit_client.dart';

import '../models/call_state.dart';
import 'api_client.dart';

const kControlTopic = 'rs.agent.control';
const kTranscriptionTopic = 'lk.transcription';

/// Version guard: drop metadata payloads whose `v` is not newer than the last
/// applied version. This prevents stale or out-of-order metadata from
/// resurrecting an old toggle state (docs/02 § room metadata schema).
int? _versionFrom(String? raw) {
  if (raw == null) return null;
  try {
    final m = jsonDecode(raw) as Map<String, dynamic>;
    return (m['v'] as int?) ?? 0;
  } catch (_) {
    return null;
  }
}

Map<String, dynamic>? _parseMetadata(String? raw) {
  if (raw == null) return null;
  try {
    return jsonDecode(raw) as Map<String, dynamic>;
  } catch (_) {
    return null;
  }
}

class CallController extends StateNotifier<CallState> {
  final ApiClient _apiClient;
  Room? _room;
  final List<CancelListenFunc> _subscriptions = [];
  Timer? _tokenRefreshTimer;
  Timer? _toggleRevertTimer;
  Timer? _transcriptPollTimer;
  String? _sessionId;
  String? _lastWsUrl;

  CallController(this._apiClient) : super(const CallState(myRole: Role.caller));

  /// Connect and publish.
  ///
  /// H.264 + no simulcast is deliberate (FR-2.4): Track Egress does no
  /// transcoding, so the codec decides the container. VP8 would give us .ivf
  /// files that most tooling cannot open. See docs/06 ADR.
  Future<void> connect({
    required String wsUrl,
    required String token,
    required Role role,
    required String sessionId,
    DateTime? tokenExpiresAt,
    String? joinCode,
  }) async {
    _sessionId = sessionId;
    _lastWsUrl = wsUrl;
    state = state.copyWith(
      status: ConnectionStatus.connecting,
      myRole: role,
      sessionId: sessionId,
      joinCode: joinCode,
    );

    _room = Room(
      roomOptions: const RoomOptions(
        adaptiveStream: true,
        dynacast: false, // 1:1 call; no layer to pause
        defaultVideoPublishOptions: VideoPublishOptions(
          videoCodec: 'h264',
          simulcast: false,
        ),
        defaultAudioCaptureOptions: AudioCaptureOptions(
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        ),
      ),
    );

    try {
      await _room!.connect(wsUrl, token);
      await _room!.localParticipant?.setCameraEnabled(true);
      await _room!.localParticipant?.setMicrophoneEnabled(true);
      _listen();
      _classifyExistingParticipants();
      state = state.copyWith(status: ConnectionStatus.connected);
      if (tokenExpiresAt != null) {
        scheduleTokenRefresh(tokenExpiresAt);
      }
      if (role == Role.support) {
        _startTranscriptPoll();
      }
    } catch (e) {
      state = state.copyWith(
        status: ConnectionStatus.disconnected,
        error: 'Could not connect: $e',
      );
      rethrow;
    }
  }

  void _listen() {
    final room = _room;
    if (room == null) return;

    _subscriptions.add(
      room.events.listen((event) async {
        if (event is RoomMetadataChangedEvent) {
          _onMetadataChanged(event.metadata);
        } else if (event is ParticipantConnectedEvent) {
          _onParticipantConnected(event.participant);
        } else if (event is ParticipantDisconnectedEvent) {
          _onParticipantDisconnected(event.participant);
        } else if (event is TrackSubscribedEvent) {
          // Force a rebuild so new renderers attach.
          state = state.copyWith();
        } else if (event is RoomDisconnectedEvent) {
          state = state.copyWith(
            status: ConnectionStatus.disconnected,
            error: event.reason != null ? 'Disconnected: ${event.reason}' : null,
          );
          _clearTimers();
        } else if (event is RoomReconnectingEvent) {
          state = state.copyWith(status: ConnectionStatus.reconnecting);
        } else if (event is RoomReconnectedEvent) {
          state = state.copyWith(status: ConnectionStatus.connected);
        } else if (event is DataReceivedEvent) {
          // lk.transcription text stream arrives here in some LiveKit client
          // versions; handle both paths for robustness.
          if (event.topic == kTranscriptionTopic) {
            _onTranscriptionText(event.data);
          }
        }
      }),
    );
  }

  void _classifyExistingParticipants() {
    final room = _room;
    if (room == null) return;
    for (final p in room.remoteParticipants.values) {
      _onParticipantConnected(p);
    }
  }

  void _onMetadataChanged(String? raw) {
    final v = _versionFrom(raw);
    if (v == null || v <= state.metadataVersion) return;

    final m = _parseMetadata(raw);
    if (m == null) return;

    final aiEnabled = m['ai_enabled'] as bool? ?? state.aiEnabled;
    final recording = m['recording'] as bool? ?? state.recording;
    final modeString = m['mode'] as String?;
    final agentMode = AgentMode.values.firstWhere(
      (e) => e.name.toUpperCase() == (modeString ?? '').toUpperCase(),
      orElse: () => state.agentMode,
    );

    final pending = state.aiToggleStatus == AiToggleStatus.pending;
    final resolvedToggle = pending && aiEnabled == state.aiEnabled
        ? AiToggleStatus.failed
        : AiToggleStatus.idle;

    state = state.copyWith(
      metadataVersion: v,
      aiEnabled: aiEnabled,
      recording: recording,
      agentMode: agentMode,
      aiToggleStatus: resolvedToggle,
    );
  }

  void _onParticipantConnected(RemoteParticipant participant) {
    final identity = participant.identity;
    if (identity.startsWith('support-')) {
      state = state.copyWith(remoteHuman: participant);
    } else if (identity == 'agent') {
      state = state.copyWith(agent: participant);
    }
  }

  void _onParticipantDisconnected(RemoteParticipant participant) {
    final identity = participant.identity;
    if (identity.startsWith('support-')) {
      state = state.copyWith(remoteHuman: null);
    } else if (identity == 'agent') {
      state = state.copyWith(agent: null);
    }
  }

  void _onTranscriptionText(List<int> data) {
    try {
      final json = jsonDecode(utf8.decode(data)) as Map<String, dynamic>;
      _appendTranscription(json);
    } catch (_) {
      // Ignore malformed transcription payloads.
    }
  }

  void _appendTranscription(Map<String, dynamic> json) {
    final segments = json['segments'] as List<dynamic>? ?? [];
    if (segments.isEmpty) return;

    final identity = json['participant_identity'] as String? ?? '';
    final role = _roleFromIdentity(identity);

    final text = segments.map((s) => s['text'] as String? ?? '').join(' ');
    final isFinal = (json['final'] as bool?) ?? true;

    if (text.trim().isEmpty) return;

    final captions = [...state.captions];
    final transcript = [...state.transcript];

    if (isFinal) {
      final startMs = (segments.first['start_time'] as num?)?.toInt() ?? 0;
      transcript.add(TranscriptEntry(role: role, text: text.trim(), startMs: startMs));
      // Replace any interim captions from this speaker with the final.
      captions.removeWhere((c) => c.identity == identity && !c.isFinal);
    } else {
      // Keep only the last two interim caption lines.
      captions.removeWhere((c) => c.identity == identity && !c.isFinal);
      captions.add(CaptionLine(
        speakerRole: _displayRole(role),
        identity: identity,
        text: text.trim(),
        isFinal: false,
      ));
      if (captions.length > 2) {
        captions.removeAt(0);
      }
    }

    state = state.copyWith(captions: captions, transcript: transcript);
  }

  String _roleFromIdentity(String identity) {
    if (identity.startsWith('caller-')) return 'caller';
    if (identity.startsWith('support-')) return 'support';
    if (identity == 'agent') return 'agent';
    return 'unknown';
  }

  Role? _displayRole(String role) {
    switch (role) {
      case 'caller':
        return Role.caller;
      case 'support':
        return Role.support;
      default:
        return null;
    }
  }

  /// Support-only. Sends both paths at once (docs/02 § control plane):
  /// the REST call is authoritative and audited; the data message is the
  /// latency hedge. UI goes `pending` until the metadata echo confirms, and
  /// reverts after 2s.
  Future<void> setAiEnabled(bool enabled, {required String idToken}) async {
    if (state.myRole != Role.support) return;
    if (state.sessionId == null) return;

    state = state.copyWith(aiToggleStatus: AiToggleStatus.pending);

    // Fast path: data message to the agent.
    unawaited(_sendControlMessage(enabled));

    // Authoritative path: REST call.
    try {
      await _apiClient.setAiEnabled(
        state.sessionId!,
        enabled,
        idToken: idToken,
        reason: 'support_toggle',
      );
    } on ApiException catch (e) {
      state = state.copyWith(aiToggleStatus: AiToggleStatus.failed, error: e.message);
      return;
    }

    // Revert if the metadata echo does not arrive within 2 seconds.
    _toggleRevertTimer?.cancel();
    _toggleRevertTimer = Timer(const Duration(seconds: 2), () {
      if (state.aiToggleStatus == AiToggleStatus.pending) {
        state = state.copyWith(aiToggleStatus: AiToggleStatus.failed);
      }
    });
  }

  Future<void> _sendControlMessage(bool enabled) async {
    try {
      await _room?.localParticipant?.publishData(
        utf8.encode(jsonEncode({'ai_enabled': enabled})),
        reliable: true,
        topic: kControlTopic,
      );
    } catch (_) {
      // The REST path will still drive the authoritative state.
    }
  }

  /// Schedule at (expires_at - 2min), call POST /v1/sessions/{id}/token/refresh,
  /// and store the refreshed token. The LiveKit Flutter SDK does not expose a
  /// public `setToken` while connected; it relies on server-sent token refresh
  /// over the signal connection. If that fails, the stored token is available
  /// for a reconnect.
  void scheduleTokenRefresh(DateTime expiresAt) {
    _tokenRefreshTimer?.cancel();
    final refreshAt = expiresAt.subtract(const Duration(minutes: 2));
    final delay = refreshAt.difference(DateTime.now());
    if (delay.isNegative) return;

    _tokenRefreshTimer = Timer(delay, () async {
      await _refreshToken();
    });
  }

  Future<void> _refreshToken() async {
    if (_sessionId == null) return;
    try {
      final result = await _apiClient.refreshToken(_sessionId!);
      final livekit = result['livekit'] as Map<String, dynamic>?;
      final newToken = livekit?['token'] as String?;
      final expiresAt = _parseIso(livekit?['expires_at'] as String?);
      if (newToken != null && _lastWsUrl != null) {
        // Prepare the connection with the new token so the SDK can use it if a
        // reconnect is required. (There is no public setToken on the connected
        // Room in livekit_client 2.x.)
        await _room?.prepareConnection(_lastWsUrl!, newToken);
        if (expiresAt != null) {
          scheduleTokenRefresh(expiresAt);
        }
      }
    } catch (e) {
      // The room will try to reconnect; if that fails the user lands on the
      // summary screen. Do not crash here.
      if (kDebugMode) print('Token refresh failed: $e');
    }
  }

  DateTime? _parseIso(String? value) {
    if (value == null) return null;
    try {
      return DateTime.parse(value);
    } catch (_) {
      return null;
    }
  }

  void _startTranscriptPoll() {
    _transcriptPollTimer?.cancel();
    _transcriptPollTimer = Timer.periodic(const Duration(seconds: 10), (_) async {
      await _pollTranscript();
    });
  }

  Future<void> _pollTranscript() async {
    if (_sessionId == null) return;
    try {
      final result = await _apiClient.getTranscript(_sessionId!);
      final utterances = result['utterances'] as List<dynamic>? ?? [];
      final existingIds = state.transcript.map((t) => '${t.role}:${t.startMs}:${t.text}').toSet();
      final additions = <TranscriptEntry>[];
      for (final u in utterances) {
        final role = u['role'] as String? ?? 'unknown';
        final text = u['text'] as String? ?? '';
        final startMs = (u['start_ms'] as num?)?.toInt() ?? 0;
        final key = '$role:$startMs:$text';
        if (!existingIds.contains(key)) {
          additions.add(TranscriptEntry(role: role, text: text, startMs: startMs));
        }
      }
      if (additions.isNotEmpty) {
        state = state.copyWith(transcript: [...state.transcript, ...additions]);
      }
    } catch (e) {
      if (kDebugMode) print('Transcript poll failed: $e');
    }
  }

  // Shared in-call controls.

  Future<void> toggleMicrophone() async {
    final local = _room?.localParticipant;
    if (local == null) return;
    await local.setMicrophoneEnabled(!local.isMicrophoneEnabled());
    state = state.copyWith();
  }

  Future<void> toggleCamera() async {
    final local = _room?.localParticipant;
    if (local == null) return;
    await local.setCameraEnabled(!local.isCameraEnabled());
    state = state.copyWith();
  }

  Future<void> switchCamera() async {
    final local = _room?.localParticipant;
    if (local == null) return;
    final videoPub = local.videoTrackPublications.isNotEmpty
        ? local.videoTrackPublications.first
        : null;
    final track = videoPub?.track;
    if (track is! LocalVideoTrack) return;

    final devices = await Hardware.instance.videoInputs();
    if (devices.length < 2) return;

    final currentOptions = track.currentOptions;
    String? currentDeviceId;
    if (currentOptions is CameraCaptureOptions) {
      currentDeviceId = currentOptions.deviceId;
    }

    final currentIndex = devices.indexWhere((d) => d.deviceId == currentDeviceId);
    final nextIndex = currentIndex < 0 ? 0 : (currentIndex + 1) % devices.length;
    await track.switchCamera(devices[nextIndex].deviceId);
  }

  Future<void> setSpeakerphone(bool enabled) async {
    await AudioManager.instance.setSpeakerOutputPreferred(enabled);
    state = state.copyWith();
  }

  Future<void> disconnect() async {
    _clearTimers();
    await _room?.disconnect();
  }

  Future<void> endCall({String? idToken}) async {
    if (_sessionId != null) {
      try {
        await _apiClient.endSession(_sessionId!, idToken: idToken);
      } catch (e) {
        if (kDebugMode) print('End session call failed: $e');
      }
    }
    await disconnect();
  }

  void clearError() => state = state.copyWith(error: null);

  void _clearTimers() {
    _tokenRefreshTimer?.cancel();
    _toggleRevertTimer?.cancel();
    _transcriptPollTimer?.cancel();
    for (final s in _subscriptions) {
      s();
    }
    _subscriptions.clear();
  }

  Room? get room => _room;

  @override
  void dispose() {
    _clearTimers();
    _room?.dispose();
    super.dispose();
  }
}

extension CallStateCopy on CallState {
  CallState copyWith({
    ConnectionStatus? status,
    Role? myRole,
    String? sessionId,
    String? joinCode,
    Object? remoteHuman = _sentinel,
    Object? agent = _sentinel,
    bool? aiEnabled,
    AiToggleStatus? aiToggleStatus,
    bool? recording,
    AgentMode? agentMode,
    int? metadataVersion,
    List<CaptionLine>? captions,
    List<TranscriptEntry>? transcript,
    Object? error = _sentinel,
  }) {
    return CallState(
      status: status ?? this.status,
      myRole: myRole ?? this.myRole,
      sessionId: sessionId ?? this.sessionId,
      joinCode: joinCode ?? this.joinCode,
      remoteHuman: remoteHuman == _sentinel ? this.remoteHuman : remoteHuman as RemoteParticipant?,
      agent: agent == _sentinel ? this.agent : agent as RemoteParticipant?,
      aiEnabled: aiEnabled ?? this.aiEnabled,
      aiToggleStatus: aiToggleStatus ?? this.aiToggleStatus,
      recording: recording ?? this.recording,
      agentMode: agentMode ?? this.agentMode,
      metadataVersion: metadataVersion ?? this.metadataVersion,
      captions: captions ?? this.captions,
      transcript: transcript ?? this.transcript,
      error: error == _sentinel ? this.error : error as String?,
    );
  }
}

const _sentinel = Object();
