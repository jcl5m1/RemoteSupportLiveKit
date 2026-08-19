// Shared in-call controls: mute mic, toggle camera, switch camera,
// speaker/earpiece, hang up. See docs/07-flutter-app.md § CallScreen.

import 'package:flutter/material.dart';

import '../services/call_controller.dart';

class CallControlsWidget extends StatefulWidget {
  final CallController controller;

  const CallControlsWidget({super.key, required this.controller});

  @override
  State<CallControlsWidget> createState() => _CallControlsWidgetState();
}

class _CallControlsWidgetState extends State<CallControlsWidget> {
  bool _speakerOn = true;

  @override
  Widget build(BuildContext context) {
    final local = widget.controller.room?.localParticipant;
    final micEnabled = local?.isMicrophoneEnabled() ?? false;
    final camEnabled = local?.isCameraEnabled() ?? false;

    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
      children: [
        _ControlButton(
          icon: micEnabled ? Icons.mic : Icons.mic_off,
          label: micEnabled ? 'Mute' : 'Unmute',
          onPressed: widget.controller.toggleMicrophone,
        ),
        _ControlButton(
          icon: camEnabled ? Icons.videocam : Icons.videocam_off,
          label: camEnabled ? 'Camera' : 'Camera off',
          onPressed: widget.controller.toggleCamera,
        ),
        _ControlButton(
          icon: Icons.flip_camera_ios,
          label: 'Switch',
          onPressed: widget.controller.switchCamera,
        ),
        _ControlButton(
          icon: _speakerOn ? Icons.volume_up : Icons.hearing,
          label: _speakerOn ? 'Speaker' : 'Earpiece',
          onPressed: () async {
            final next = !_speakerOn;
            await widget.controller.setSpeakerphone(next);
            setState(() => _speakerOn = next);
          },
        ),
        _ControlButton(
          icon: Icons.call_end,
          label: 'End',
          backgroundColor: Colors.red,
          onPressed: () => widget.controller.endCall(),
        ),
      ],
    );
  }
}

class _ControlButton extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onPressed;
  final Color? backgroundColor;

  const _ControlButton({
    required this.icon,
    required this.label,
    required this.onPressed,
    this.backgroundColor,
  });

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        CircleAvatar(
          backgroundColor: backgroundColor ?? Colors.white24,
          child: IconButton(
            icon: Icon(icon, color: Colors.white),
            onPressed: onPressed,
          ),
        ),
        const SizedBox(height: 4),
        Text(label, style: const TextStyle(color: Colors.white, fontSize: 12)),
      ],
    );
  }
}
