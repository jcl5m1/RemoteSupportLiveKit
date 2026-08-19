import 'package:flutter_test/flutter_test.dart';
import 'package:remote_support/utils/join_code.dart';

void main() {
  group('normalizeJoinCode', () {
    test('upper-cases input', () {
      expect(normalizeJoinCode('k7r2xm'), 'K7R2XM');
    });

    test('remaps ambiguous characters (FR-1.3)', () {
      expect(normalizeJoinCode('ILOU'), '110V');
    });

    test('remaps mixed ambiguous input', () {
      expect(normalizeJoinCode('iL0u'), '110V');
    });

    test('leaves valid codes untouched', () {
      expect(normalizeJoinCode('K7R2XM'), 'K7R2XM');
    });
  });
}
