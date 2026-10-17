import 'package:flutter_test/flutter_test.dart';
import 'package:translation_mobile/main.dart';

void main() {
  test('parses a full join link', () {
    final info = parseJoinLink(
      'translationmobile://join?url=wss://example.livekit.cloud'
      '&token=abc.def.ghi&identity=user-zh&lang=zh',
    );
    expect(info, isNotNull);
    expect(info!.serverUrl, 'wss://example.livekit.cloud');
    expect(info.token, 'abc.def.ghi');
    expect(info.identity, 'user-zh');
    expect(info.language, 'zh');
  });

  test('defaults language to en', () {
    final info = parseJoinLink(
      'translationmobile://join?url=wss://x&token=t&identity=u',
    );
    expect(info!.language, 'en');
  });

  test('rejects missing params and wrong schemes', () {
    expect(parseJoinLink('translationmobile://join?url=wss://x'), isNull);
    expect(parseJoinLink('https://join?url=a&token=b&identity=c'), isNull);
    expect(parseJoinLink('not a uri at all'), isNull);
  });
}
