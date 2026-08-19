import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:remote_support/providers.dart';
import 'package:remote_support/screens/consent_sheet.dart';

import '../utils/fake_api_client.dart';

void main() {
  group('ConsentSheetScreen', () {
    final session = {
      'session_id': 'test-session-id',
      'join_code': 'K7R2XM',
      'consent_text_version': 'v1.0',
      'consent_text': 'This call will be recorded and transcribed.',
      'caller_session_token': 'caller-jwt',
      'recording_enabled': true,
    };

    testWidgets('renders server-owned consent text', (tester) async {
      await tester.pumpWidget(
        ProviderScope(
          overrides: [apiClientProvider.overrideWithValue(FakeApiClient())],
          child: MaterialApp(
            home: ConsentSheetScreen(session: session),
          ),
        ),
      );

      expect(find.text('This call will be recorded and transcribed.'), findsOneWidget);
      expect(find.text('I agree — start the call'), findsOneWidget);
      expect(find.text('I decline'), findsOneWidget);
    });

    testWidgets('accept records consent and stores caller token', (tester) async {
      final fake = FakeApiClient();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [apiClientProvider.overrideWithValue(fake)],
          child: MaterialApp(
            home: ConsentSheetScreen(session: session),
          ),
        ),
      );

      expect(fake.callerToken, 'caller-jwt');

      await tester.tap(find.text('I agree — start the call'));
      await tester.pump();

      expect(fake.consentRecorded, true);
      expect(fake.lastConsentAccepted, true);
      expect(fake.lastConsentVersion, 'v1.0');
    });

    testWidgets('decline records declined consent', (tester) async {
      final fake = FakeApiClient();
      await tester.pumpWidget(
        ProviderScope(
          overrides: [apiClientProvider.overrideWithValue(fake)],
          child: MaterialApp(
            home: ConsentSheetScreen(session: session),
          ),
        ),
      );

      await tester.tap(find.text('I decline'));
      await tester.pump();

      expect(fake.consentRecorded, true);
      expect(fake.lastConsentAccepted, false);
    });
  });
}
