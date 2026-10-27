import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:remote_support/meet/audio_route.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'fakes.dart';

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('choosing outputs mirrors the choice into LiveKit speaker routing', () async {
    final platform = FakeAudioPlatform(phoneRoutes);
    final speaker = <(bool, bool)>[];
    final router = AudioRouter(
      platform: platform,
      setSpeakerPreferred: (prefer, {bool force = false}) async => speaker.add((prefer, force)),
    );
    await router.setActive(true);
    expect(router.currentOutput!.name, 'Speaker');

    await router.selectOutput(phoneRoutes.outputs[0]); // earpiece
    expect(speaker.last, (false, false));
    await router.selectOutput(phoneRoutes.outputs[1]); // speaker, even with headsets on
    expect(speaker.last, (true, true));
    await router.selectOutput(phoneRoutes.outputs[2]); // Bluetooth
    expect(speaker.last, (true, false));
    expect(platform.selectedOutputs, ['1', '2', '7']);
    expect(router.currentOutput!.isBluetooth, isTrue);
  });

  test('a Bluetooth mic brings the output with it; choice is re-applied after hot-plug', () {
    fakeAsync((async) {
      final platform = FakeAudioPlatform(phoneRoutes);
      final router = AudioRouter(platform: platform, setSpeakerPreferred: (p, {bool force = false}) async {});
      router.start();
      async.flushMicrotasks();
      router.setActive(true);
      async.flushMicrotasks();
      router.selectInput(phoneRoutes.inputs[1]);
      async.flushMicrotasks();
      expect(platform.selectedInputs.last, 'bluetooth');

      // The system (or LiveKit) moves audio back to the speaker on a device change...
      platform.current = const AudioRoutes(
        inputs: [AudioDevice('microphone-bottom', 'Phone microphone (bottom)', 'builtin_mic'),
                 AudioDevice('bluetooth', 'Pixel Buds', 'bluetooth')],
        outputs: [AudioDevice('1', 'Phone', 'earpiece'), AudioDevice('2', 'Speaker', 'speaker'),
                  AudioDevice('9', 'Pixel Buds', 'bluetooth')],
        input: 'microphone-bottom',
        output: '2',
      );
      platform.fireChange();
      async.elapse(const Duration(seconds: 1));
      // ...and the user's headset choice is restored (new id, same device).
      expect(platform.selectedOutputs.last, '9');
      expect(platform.selectedInputs.last, 'bluetooth');

      // Leaving the call releases the route.
      router.setActive(false);
      async.flushMicrotasks();
      expect(platform.cleared, 1);
      router.dispose();
    });
  });

  test('in the lobby choices are saved and shown, but the route is not touched', () async {
    final platform = FakeAudioPlatform(phoneRoutes);
    final router = AudioRouter(platform: platform, setSpeakerPreferred: (p, {bool force = false}) async {});
    await router.refresh();
    await router.selectOutput(phoneRoutes.outputs[2]); // Bluetooth, before joining
    await router.selectInput(phoneRoutes.inputs[1]);
    expect(platform.selectedOutputs, isEmpty);
    expect(platform.selectedInputs, isEmpty);
    expect(router.currentOutput!.name, 'Pixel Buds'); // shown as chosen
    await router.setActive(true); // joined: applied now
    expect(platform.selectedOutputs, ['7']);
    expect(platform.selectedInputs, ['bluetooth']);
  });
}
