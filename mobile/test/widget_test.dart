// The two-party support flow is still reachable (home is now the meeting lobby).

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:remote_support/providers.dart';
import 'package:remote_support/screens/role_select.dart';

import 'utils/fake_support_auth.dart';

void main() {
  testWidgets('Role selection screen renders', (WidgetTester tester) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [supportAuthProvider.overrideWithValue(FakeSupportAuth())],
        child: const MaterialApp(home: RoleSelectScreen()),
      ),
    );
    expect(find.text('Remote Support'), findsOneWidget);
    expect(find.text('I need help'), findsOneWidget);
    expect(find.text("I'm support"), findsOneWidget);
  });
}
