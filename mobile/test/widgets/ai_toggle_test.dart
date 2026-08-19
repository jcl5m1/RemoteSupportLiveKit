import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:remote_support/models/call_state.dart';
import 'package:remote_support/widgets/ai_toggle.dart';

void main() {
  group('AiToggleWidget', () {
    testWidgets('shows on state and calls onChanged', (tester) async {
      bool? toggled;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AiToggleWidget(
              enabled: true,
              status: AiToggleStatus.idle,
              onChanged: (v) => toggled = v,
            ),
          ),
        ),
      );

      expect(find.text('Assistant on'), findsOneWidget);
      await tester.tap(find.byType(Switch));
      await tester.pump();
      expect(toggled, false);
    });

    testWidgets('shows pending state without calling onChanged repeatedly', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AiToggleWidget(
              enabled: false,
              status: AiToggleStatus.pending,
              onChanged: (_) {},
            ),
          ),
        ),
      );

      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(find.text('Assistant on'), findsOneWidget);
    });

    testWidgets('shows failed state', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: AiToggleWidget(
              enabled: true,
              status: AiToggleStatus.failed,
              onChanged: (_) {},
            ),
          ),
        ),
      );

      expect(find.byIcon(Icons.error_outline), findsOneWidget);
    });
  });
}
