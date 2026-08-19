// Renders a LiveKit participant's video track, or their initials if no video.
// See docs/07-flutter-app.md § CallScreen.

import 'package:flutter/material.dart';
import 'package:livekit_client/livekit_client.dart';

class ParticipantVideoWidget extends StatelessWidget {
  final Participant participant;

  const ParticipantVideoWidget({super.key, required this.participant});

  @override
  Widget build(BuildContext context) {
    final videoPubs = participant.videoTrackPublications;
    final videoTrack = videoPubs.isNotEmpty ? videoPubs.first.track : null;

    if (videoTrack != null && videoTrack is VideoTrack) {
      return VideoTrackRenderer(
        videoTrack,
        fit: VideoViewFit.cover,
      );
    }

    return Container(
      color: Colors.grey[900],
      child: Center(
        child: CircleAvatar(
          radius: 40,
          backgroundColor: Theme.of(context).colorScheme.primary,
          child: Text(
            _initials(participant.identity),
            style: const TextStyle(color: Colors.white, fontSize: 24),
          ),
        ),
      ),
    );
  }

  String _initials(String identity) {
    if (identity.isEmpty) return '?';
    final parts = identity.split('-');
    if (parts.length >= 2) {
      return parts.first[0].toUpperCase();
    }
    return identity[0].toUpperCase();
  }
}
