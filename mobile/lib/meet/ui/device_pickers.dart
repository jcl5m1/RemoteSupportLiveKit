// Camera, then microphone and speaker side by side (same order as the web
// lobby/settings). Speaker choices include Bluetooth headsets; on iOS the
// system route picker covers output-only devices (AirPods for music, AirPlay,
// Bluetooth speakers, car audio).

import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:livekit_client/livekit_client.dart' show MediaDevice;

import '../audio_route.dart';

IconData audioIcon(String type) => switch (type) {
      'bluetooth' => Icons.bluetooth_audio,
      'wired' => Icons.headset,
      'usb' => Icons.usb,
      'speaker' => Icons.volume_up,
      'earpiece' => Icons.phone_in_talk,
      'airplay' => Icons.airplay,
      'car' => Icons.directions_car,
      _ => Icons.mic,
    };

class DevicePickers extends StatelessWidget {
  final AudioRouter audio;
  final List<MediaDevice> cameras;
  final String? cameraId;
  final ValueChanged<MediaDevice> onCamera;

  const DevicePickers({
    super.key,
    required this.audio,
    required this.cameras,
    required this.cameraId,
    required this.onCamera,
  });

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: audio,
      builder: (context, _) {
        final r = audio.routes;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (cameras.isNotEmpty)
              _Picker<MediaDevice>(
                key: const Key('cameraPicker'),
                label: 'Camera',
                icon: Icons.videocam,
                items: cameras,
                value: cameras.where((c) => c.deviceId == cameraId).firstOrNull ?? cameras.first,
                text: (d) => d.label.isNotEmpty ? d.label : 'Camera ${cameras.indexOf(d) + 1}',
                onChanged: onCamera,
              ),
            const SizedBox(height: 12),
            Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                if (r.inputs.isNotEmpty)
                  Expanded(
                    child: _Picker<AudioDevice>(
                      key: const Key('micPicker'),
                      label: 'Microphone',
                      icon: Icons.mic,
                      items: r.inputs,
                      value: audio.currentInput ?? r.inputs.first,
                      text: (d) => d.name,
                      itemIcon: (d) => audioIcon(d.type == 'builtin_mic' ? 'mic' : d.type),
                      onChanged: audio.selectInput,
                    ),
                  ),
                if (r.inputs.isNotEmpty && r.outputs.isNotEmpty) const SizedBox(width: 10),
                if (r.outputs.isNotEmpty)
                  Expanded(
                    child: _Picker<AudioDevice>(
                      key: const Key('speakerPicker'),
                      label: 'Speaker',
                      icon: Icons.volume_up,
                      items: r.outputs,
                      value: audio.currentOutput ?? r.outputs.first,
                      text: (d) => d.name,
                      itemIcon: (d) => audioIcon(d.type),
                      onChanged: audio.selectOutput,
                    ),
                  ),
                if (r.hasRoutePicker && !kIsWeb && Platform.isIOS) ...[
                  const SizedBox(width: 6),
                  const RoutePickerButton(),
                ],
              ],
            ),
          ],
        );
      },
    );
  }
}

class _Picker<T> extends StatelessWidget {
  final String label;
  final IconData icon;
  final List<T> items;
  final T value;
  final String Function(T) text;
  final IconData Function(T)? itemIcon;
  final ValueChanged<T> onChanged;

  const _Picker({
    super.key,
    required this.label,
    required this.icon,
    required this.items,
    required this.value,
    required this.text,
    required this.onChanged,
    this.itemIcon,
  });

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(label, style: const TextStyle(fontSize: 12, color: Color(0xFF9AA0A6))),
        const SizedBox(height: 4),
        DropdownButtonFormField<T>(
          key: ValueKey(value), // follow outside changes (hot-plug, reroutes)
          initialValue: value,
          isExpanded: true,
          decoration: const InputDecoration(
            isDense: true,
            border: OutlineInputBorder(),
            contentPadding: EdgeInsets.symmetric(horizontal: 10, vertical: 10),
          ),
          items: [
            for (final d in items)
              DropdownMenuItem<T>(
                value: d,
                child: Row(children: [
                  Icon(itemIcon?.call(d) ?? icon, size: 18),
                  const SizedBox(width: 6),
                  Expanded(child: Text(text(d), overflow: TextOverflow.ellipsis)),
                ]),
              ),
          ],
          onChanged: (v) {
            if (v != null) onChanged(v);
          },
        ),
      ],
    );
  }
}

/// iOS system audio route picker (AVRoutePickerView).
class RoutePickerButton extends StatelessWidget {
  const RoutePickerButton({super.key});

  @override
  Widget build(BuildContext context) {
    return const Tooltip(
      message: 'More audio devices (AirPods, Bluetooth speakers, AirPlay)',
      child: SizedBox(
        width: 44,
        height: 44,
        child: UiKitView(
          viewType: 'net.lgitech.remotesupport/route_picker',
          creationParamsCodec: StandardMessageCodec(),
        ),
      ),
    );
  }
}
