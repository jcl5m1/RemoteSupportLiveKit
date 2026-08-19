import 'package:remote_support/services/support_auth.dart';

/// Test double that never touches Firebase. Used for widget tests that pump
/// widgets requiring [supportAuthProvider].
class FakeSupportAuth extends SupportAuth {
  FakeSupportAuth() : super(auth: null, google: null);

  @override
  bool get isSignedIn => false;

  @override
  Future<String> idToken({bool forceRefresh = false}) async => 'fake-id-token';

  @override
  Future<bool> isAdmin({bool forceRefresh = false}) async => false;
}
