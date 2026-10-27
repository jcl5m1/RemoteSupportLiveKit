// Call audio devices: pick the microphone and the speaker/headset, including
// Bluetooth, on iOS and Android.
//
// Native side: android/.../AudioRoutePlugin.kt and ios/Runner/AppDelegate.swift
// (channel net.lgitech.remotesupport/audio_route). LiveKit owns the audio
// session and its own speaker preference, so each choice is mirrored into
// AudioManager.setSpeakerOutputPreferred -- otherwise LiveKit would route back
// to its default (headset > speaker > earpiece) the next time it reconfigures.

import 'dart:async';
import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:livekit_client/livekit_client.dart' show AudioManager;
import 'package:permission_handler/permission_handler.dart';
import 'package:shared_preferences/shared_preferences.dart';

class AudioDevice {
  final String id;
  final String name;

  /// earpiece | speaker | builtin_mic | bluetooth | wired | usb | airplay | car | other
  final String type;
  const AudioDevice(this.id, this.name, this.type);

  bool get isBluetooth => type == 'bluetooth';

  static AudioDevice? fromMap(Object? m) {
    if (m is! Map) return null;
    final id = m['id'];
    final name = m['name'];
    if (id is! String || name is! String) return null;
    return AudioDevice(id, name, (m['type'] as String?) ?? 'other');
  }

  @override
  bool operator ==(Object other) => other is AudioDevice && other.id == id;
  @override
  int get hashCode => id.hashCode;
}

class AudioRoutes {
  final List<AudioDevice> inputs;
  final List<AudioDevice> outputs;
  final String? input;
  final String? output;

  /// iOS: the system route picker is available for output-only devices.
  final bool hasRoutePicker;
  const AudioRoutes({
    this.inputs = const [],
    this.outputs = const [],
    this.input,
    this.output,
    this.hasRoutePicker = false,
  });

  static AudioRoutes fromMap(Map<Object?, Object?> m) => AudioRoutes(
        inputs: (m['inputs'] as List? ?? const []).map(AudioDevice.fromMap).whereType<AudioDevice>().toList(),
        outputs: (m['outputs'] as List? ?? const []).map(AudioDevice.fromMap).whereType<AudioDevice>().toList(),
        input: m['input'] as String?,
        output: m['output'] as String?,
        hasRoutePicker: m['hasRoutePicker'] == true,
      );
}

/// Platform calls, separated so tests can fake them.
abstract class AudioRoutePlatform {
  Future<AudioRoutes> routes();
  Future<bool> selectInput(String id);
  Future<bool> selectOutput(String id);

  /// Release the call route (Android: communication device / Bluetooth SCO).
  Future<void> clearOutput();
  Stream<void> get changes;
}

class NativeAudioRoutePlatform implements AudioRoutePlatform {
  static const _method = MethodChannel('net.lgitech.remotesupport/audio_route');
  static const _events = EventChannel('net.lgitech.remotesupport/audio_route/events');

  @override
  Future<AudioRoutes> routes() async {
    final m = await _method.invokeMapMethod<Object?, Object?>('routes');
    return m == null ? const AudioRoutes() : AudioRoutes.fromMap(m);
  }

  @override
  Future<bool> selectInput(String id) async {
    final ok = await _method.invokeMethod<bool>('selectInput', {'id': id}) ?? false;
    // Android: also tell WebRTC which device to record from (ids match
    // flutter_webrtc's). iOS: the preferred input above is all it takes.
    if (!kIsWeb && Platform.isAndroid) {
      try {
        await rtc.Helper.selectAudioInput(id);
      } catch (_) {}
    }
    return ok;
  }

  @override
  Future<bool> selectOutput(String id) async =>
      await _method.invokeMethod<bool>('selectOutput', {'id': id}) ?? false;

  @override
  Future<void> clearOutput() => _method.invokeMethod<bool>('clearOutput');

  @override
  Stream<void> get changes => _events.receiveBroadcastStream().map((_) {});
}

/// Keeps the device lists current and applies (and re-applies) the user's
/// choices. Choices are remembered per device type+name.
class AudioRouter extends ChangeNotifier {
  final AudioRoutePlatform platform;
  final Future<void> Function(bool preferSpeaker, {bool force})? setSpeakerPreferred;
  AudioRoutes routes = const AudioRoutes();
  String? _wantInput; // name of the chosen mic
  String? _wantOutput; // type|name of the chosen output
  StreamSubscription<void>? _sub;
  Timer? _debounce;
  bool _applying = false;
  bool _active = false; // in a call: routes are applied only then
  final _reapplies = <DateTime>[];

  AudioRouter({AudioRoutePlatform? platform, this.setSpeakerPreferred})
      : platform = platform ?? NativeAudioRoutePlatform();

  static Future<void> liveKitSpeaker(bool preferSpeaker, {bool force = false}) =>
      AudioManager.instance.setSpeakerOutputPreferred(preferSpeaker, force: force);

  /// Before a call the lists show the saved choice; the platform route is
  /// only changed during a call (otherwise e.g. Android would open a Bluetooth
  /// call link while idle in the lobby).
  AudioDevice? get currentInput =>
      (!_active ? _byName(routes.inputs, _wantInput) : null) ?? _find(routes.inputs, routes.input);
  AudioDevice? get currentOutput =>
      (!_active ? _byKey(routes.outputs, _wantOutput) : null) ?? _find(routes.outputs, routes.output);

  bool get active => _active;

  /// Call started (apply the saved choices) or ended (release the route).
  Future<void> setActive(bool active) async {
    if (_active == active) return;
    _active = active;
    if (active) {
      await refresh(reapply: true);
    } else {
      try {
        await platform.clearOutput();
      } catch (_) {}
      await refresh();
    }
  }

  /// Idempotent: the lobby starts it, the call re-applies after LiveKit has
  /// set up its audio session (which picks its own default route).
  Future<void> start() async {
    if (_sub != null) {
      await refresh(reapply: true);
      return;
    }
    final prefs = await SharedPreferences.getInstance();
    _wantInput = prefs.getString('audio.input');
    _wantOutput = prefs.getString('audio.output');
    if (!kIsWeb && Platform.isAndroid) {
      // Android 12+: Bluetooth devices are only visible with this permission.
      await Permission.bluetoothConnect.request();
    }
    _sub = platform.changes.listen((_) {
      _debounce?.cancel();
      _debounce = Timer(const Duration(milliseconds: 400), () => refresh(reapply: true));
    });
    await refresh(reapply: true);
  }

  Future<void> refresh({bool reapply = false}) async {
    try {
      routes = await platform.routes();
    } catch (_) {
      return;
    }
    notifyListeners();
    if (reapply && _active && !_applying) await _reapply();
  }

  /// A newly connected Bluetooth/wired device wins (like the system does);
  /// otherwise keep the user's last choice if it is still there.
  Future<void> _reapply() async {
    // LiveKit's own audio switch may keep moving the route back; don't fight
    // it forever (at most 4 corrections in 10 s).
    final now = DateTime.now();
    _reapplies.removeWhere((t) => now.difference(t) > const Duration(seconds: 10));
    if (_reapplies.length >= 4) return;
    _reapplies.add(now);
    _applying = true;
    try {
      final out = _wantOutput == null
          ? null
          : routes.outputs.where((d) => '${d.type}|${d.name}' == _wantOutput).firstOrNull;
      if (out != null && out.id != routes.output) await _applyOutput(out);
      final input = _wantInput == null ? null : routes.inputs.where((d) => d.name == _wantInput).firstOrNull;
      if (input != null && input.id != routes.input) await platform.selectInput(input.id);
      if (out != null || input != null) routes = await platform.routes();
      notifyListeners();
    } catch (_) {
      // Best effort: the platform keeps its own routing.
    } finally {
      _applying = false;
    }
  }

  Future<void> _applyOutput(AudioDevice d) async {
    // Mirror the choice into LiveKit's routing policy first, so its next
    // reconfiguration agrees with ours.
    final speaker = setSpeakerPreferred ?? liveKitSpeaker;
    if (d.type == 'speaker') {
      await speaker(true, force: true);
    } else if (d.type == 'earpiece') {
      await speaker(false);
    } else {
      await speaker(true);
    }
    await platform.selectOutput(d.id);
  }

  Future<void> selectOutput(AudioDevice d) async {
    _wantOutput = '${d.type}|${d.name}';
    _reapplies.clear(); // a deliberate choice resets the correction budget
    (await SharedPreferences.getInstance()).setString('audio.output', _wantOutput!);
    if (_active) await _applyOutput(d);
    await refresh();
  }

  Future<void> selectInput(AudioDevice d) async {
    _wantInput = d.name;
    _reapplies.clear();
    final prefs = await SharedPreferences.getInstance();
    prefs.setString('audio.input', d.name);
    // A headset mic brings the call audio with it: follow in the output pick
    // (and in LiveKit's preference, so it doesn't route back).
    AudioDevice? out;
    if (d.type != 'builtin_mic') {
      out = routes.outputs.where((o) => o.type == d.type && o.name == d.name).firstOrNull ??
          routes.outputs.where((o) => o.type == d.type).firstOrNull;
      if (out != null) {
        _wantOutput = '${out.type}|${out.name}';
        prefs.setString('audio.output', _wantOutput!);
      }
    }
    if (_active) {
      if (out != null) await _applyOutput(out);
      await platform.selectInput(d.id);
    }
    await refresh();
  }

  @override
  void dispose() {
    _sub?.cancel();
    _debounce?.cancel();
    super.dispose();
  }

  static AudioDevice? _find(List<AudioDevice> list, String? id) =>
      id == null ? null : list.where((d) => d.id == id).firstOrNull;
  static AudioDevice? _byName(List<AudioDevice> list, String? name) =>
      name == null ? null : list.where((d) => d.name == name).firstOrNull;
  static AudioDevice? _byKey(List<AudioDevice> list, String? key) =>
      key == null ? null : list.where((d) => '${d.type}|${d.name}' == key).firstOrNull;
}
