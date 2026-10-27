import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:remote_support/meet/event_log.dart';

void main() {
  test('batches after 2 s, sends sent_at, retries failures with the same ids', () {
    fakeAsync((async) {
      var now = 1000;
      final sent = <(List<Map<String, Object?>>, int)>[];
      var fail = true;
      final log = EventLog((events, sentAt) async {
        if (fail) throw Exception('offline');
        sent.add((events, sentAt));
      }, now: () => now);

      log.log('chat', {'text': 'hi'});
      now = 1500;
      log.log('mute', {'muted': true});
      async.elapse(const Duration(seconds: 2)); // first flush fails
      expect(sent, isEmpty);
      expect(log.pending, 2);

      fail = false;
      now = 9000;
      async.elapse(const Duration(seconds: 5)); // retry
      expect(sent, hasLength(1));
      final (events, sentAt) = sent.single;
      expect(events.map((e) => e['type']), ['chat', 'mute']);
      expect(events.map((e) => e['ts']), [1000, 1500]);
      expect(sentAt, 9000);
      expect(events.map((e) => e['id']).toSet(), hasLength(2));
    });
  });

  test('20 queued events flush right away', () {
    fakeAsync((async) {
      final sent = <int>[];
      final log = EventLog((events, _) async => sent.add(events.length));
      for (var i = 0; i < 20; i++) {
        log.log('pointer');
      }
      async.flushMicrotasks();
      expect(sent, [20]);
    });
  });
}
