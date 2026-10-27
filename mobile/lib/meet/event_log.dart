// Interaction log for open calls (same as the web's logEvent/flushEvents):
// events are queued with the device clock and posted in batches with
// `sent_at`, so the server can map them onto its own clock and store them with
// the recordings.

import 'dart:async';
import 'dart:math';

typedef PostEvents = Future<void> Function(List<Map<String, Object?>> events, int sentAt);

class EventLog {
  final PostEvents _post;
  final int Function() _now;
  final _queue = <Map<String, Object?>>[];
  final _rand = Random();
  Timer? _timer;
  bool _flushing = false;

  EventLog(this._post, {int Function()? now}) : _now = now ?? (() => DateTime.now().millisecondsSinceEpoch);

  int get pending => _queue.length;

  void log(String type, [Map<String, Object?> data = const {}]) {
    _queue.add({'id': _id(), 'type': type, 'ts': _now(), 'data': data});
    if (_queue.length >= 20) {
      unawaited(flush());
    } else {
      _timer ??= Timer(const Duration(seconds: 2), flush);
    }
  }

  /// Sends what's queued (up to 100 per batch). Failed batches are retried;
  /// event ids make retries idempotent server-side.
  Future<void> flush() async {
    _timer?.cancel();
    _timer = null;
    if (_flushing || _queue.isEmpty) return;
    _flushing = true;
    final batch = _queue.take(100).toList();
    _queue.removeRange(0, batch.length);
    try {
      await _post(batch, _now());
    } catch (_) {
      _queue.insertAll(0, batch);
      _timer ??= Timer(const Duration(seconds: 5), flush);
    } finally {
      _flushing = false;
    }
    if (_queue.isNotEmpty) _timer ??= Timer(const Duration(milliseconds: 500), flush);
  }

  void dispose() => _timer?.cancel();

  String _id() {
    final a = _now().toRadixString(36);
    final b = List.generate(12, (_) => _rand.nextInt(36).toRadixString(36)).join();
    return '$a-$b';
  }
}
