// Test doubles for the meet client: native audio routing and the HTTP API.

import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:remote_support/meet/audio_route.dart';
import 'package:remote_support/meet/meet_api.dart';

class FakeAudioPlatform implements AudioRoutePlatform {
  AudioRoutes current;
  final selectedInputs = <String>[];
  final selectedOutputs = <String>[];
  final _changes = StreamController<void>.broadcast();

  FakeAudioPlatform(this.current);

  @override
  Future<AudioRoutes> routes() async => current;

  @override
  Future<bool> selectInput(String id) async {
    selectedInputs.add(id);
    current = AudioRoutes(
        inputs: current.inputs, outputs: current.outputs, input: id, output: current.output);
    return true;
  }

  @override
  Future<bool> selectOutput(String id) async {
    selectedOutputs.add(id);
    current = AudioRoutes(
        inputs: current.inputs, outputs: current.outputs, input: current.input, output: id);
    return true;
  }

  int cleared = 0;

  @override
  Future<void> clearOutput() async => cleared++;

  @override
  Stream<void> get changes => _changes.stream;

  void fireChange() => _changes.add(null);
}

const phoneRoutes = AudioRoutes(
  inputs: [
    AudioDevice('microphone-bottom', 'Phone microphone (bottom)', 'builtin_mic'),
    AudioDevice('bluetooth', 'Pixel Buds', 'bluetooth'),
  ],
  outputs: [
    AudioDevice('1', 'Phone', 'earpiece'),
    AudioDevice('2', 'Speaker', 'speaker'),
    AudioDevice('7', 'Pixel Buds', 'bluetooth'),
  ],
  input: 'microphone-bottom',
  output: '2',
);

/// Routes Dio requests to [handler] (method, path, query, body) -> (status, json).
class FakeAdapter implements HttpClientAdapter {
  final FutureOr<(int, Object?)> Function(RequestOptions o, Object? body) handler;
  final requests = <RequestOptions>[];
  FakeAdapter(this.handler);

  @override
  Future<ResponseBody> fetch(RequestOptions o, Stream<Uint8List>? requestStream, Future<void>? cancel) async {
    requests.add(o);
    Object? body = o.data;
    if (requestStream != null) {
      final bytes = <int>[];
      await for (final c in requestStream) {
        bytes.addAll(c);
      }
      body = bytes;
    }
    final (status, json) = await handler(o, body);
    return ResponseBody.fromString(jsonEncode(json), status, headers: {
      Headers.contentTypeHeader: [Headers.jsonContentType],
    });
  }

  @override
  void close({bool force = false}) {}
}

MeetApi fakeApi(FutureOr<(int, Object?)> Function(RequestOptions o, Object? body) handler,
    {String base = 'https://example.test/livekit'}) {
  final dio = Dio()..httpClientAdapter = FakeAdapter(handler);
  return MeetApi(base, dio: dio);
}
