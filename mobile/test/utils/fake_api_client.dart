import 'package:remote_support/services/api_client.dart';

/// Lightweight fake for widget tests. Extends the real client so it satisfies
/// the provider type without adding a mocking dependency.
class FakeApiClient extends ApiClient {
  String? callerToken;
  bool consentRecorded = false;
  bool? lastConsentAccepted;
  String? lastConsentVersion;

  FakeApiClient() : super('http://localhost:8000');

  @override
  void setCallerToken(String token) => callerToken = token;

  @override
  Future<Map<String, dynamic>> recordConsent({
    required String sessionId,
    required bool accepted,
    required String consentTextVersion,
  }) async {
    consentRecorded = true;
    lastConsentAccepted = accepted;
    lastConsentVersion = consentTextVersion;
    if (accepted) {
      // Return accepted but omit the livekit block so the widget under test does
      // not attempt a real Room.connect() call.
      return {
        'accepted': true,
        'session_state': 'active',
        'livekit': null,
        'recording_enabled': true,
      };
    }
    return {'accepted': false, 'session_state': 'consent_declined', 'livekit': null};
  }
}
