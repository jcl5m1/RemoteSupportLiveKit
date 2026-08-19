// Riverpod providers shared across the app. Kept separate from main.dart to
// avoid circular imports (screens need providers, main.dart needs screens).

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'models/call_state.dart';
import 'services/api_client.dart';
import 'services/call_controller.dart';
import 'services/support_auth.dart';

const _apiBaseUrl = String.fromEnvironment(
  'API_BASE_URL',
  defaultValue: 'http://localhost:8000',
);

final apiClientProvider = Provider<ApiClient>((ref) => ApiClient(_apiBaseUrl));
final supportAuthProvider = Provider<SupportAuth>((ref) => SupportAuth());

final callControllerProvider =
    StateNotifierProvider<CallController, CallState>((ref) {
  return CallController(ref.watch(apiClientProvider));
});
