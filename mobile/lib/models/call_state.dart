// Call state model. See docs/07-flutter-app.md § state management.
//
// NOTE: The spec shows a @freezed model, but we intentionally use a hand-written
// mutable-copy extension (`CallState.copyWith` in services/call_controller.dart)
// to avoid a build_runner dependency in phase 1. `freezed` remains in
// pubspec.yaml for future codegen if the model grows.

import 'package:livekit_client/livekit_client.dart';

enum Role { caller, support }

enum ConnectionStatus { idle, connecting, connected, reconnecting, disconnected }

enum AgentMode { solo, assisted, wrapUp }

/// Tri-state so the support switch can show an optimistic `pending` while it
/// waits for the room-metadata echo. Reverts after 2s with no confirmation.
enum AiToggleStatus { idle, pending, failed }

class CaptionLine {
  final Role? speakerRole;
  final String identity;
  final String text;
  final bool isFinal;

  const CaptionLine({
    required this.speakerRole,
    required this.identity,
    required this.text,
    required this.isFinal,
  });
}

class TranscriptEntry {
  final String role; // caller | support | agent
  final String text;
  final int startMs;

  const TranscriptEntry({
    required this.role,
    required this.text,
    required this.startMs,
  });
}

class CallState {
  final ConnectionStatus status;
  final Role myRole;
  final String? sessionId;
  final String? joinCode;
  final RemoteParticipant? remoteHuman;
  final RemoteParticipant? agent;
  final bool aiEnabled;
  final AiToggleStatus aiToggleStatus;
  final bool recording;
  final AgentMode agentMode;
  final int metadataVersion;
  final List<CaptionLine> captions;
  final List<TranscriptEntry> transcript;
  final String? error;

  const CallState({
    this.status = ConnectionStatus.idle,
    required this.myRole,
    this.sessionId,
    this.joinCode,
    this.remoteHuman,
    this.agent,
    this.aiEnabled = true,
    this.aiToggleStatus = AiToggleStatus.idle,
    this.recording = false,
    this.agentMode = AgentMode.solo,
    this.metadataVersion = 0,
    this.captions = const [],
    this.transcript = const [],
    this.error,
  });
}
