// Google SSO for support operators. See docs/08-security-compliance.md.
//
// Callers never touch this — they stay anonymous. Only the support role signs
// in, because support is the trusted role that controls recording and the AI.
//
// Two tokens, don't confuse them:
//   * Firebase ID token — authenticates the operator to OUR BACKEND
//   * LiveKit JWT       — minted by the backend afterwards, authorizes the room
//
// The Firebase ID token is never sent to LiveKit.

import 'package:firebase_auth/firebase_auth.dart';
import 'package:google_sign_in/google_sign_in.dart';

class SupportAuthException implements Exception {
  final String code;
  final String message;
  const SupportAuthException(this.code, this.message);
  @override
  String toString() => 'SupportAuthException($code): $message';
}

class SupportAuth {
  final FirebaseAuth? _authOverride;
  final GoogleSignIn? _googleOverride;

  SupportAuth({FirebaseAuth? auth, GoogleSignIn? google})
      : _authOverride = auth,
        // hostedDomain gives a better UX — Google filters the account picker
        // to the Workspace domain. It is NOT a security control: the backend
        // re-checks the verified email domain on every request, because
        // anything the client asserts is untrusted.
        _googleOverride = google;

  FirebaseAuth get _auth => _authOverride ?? FirebaseAuth.instance;
  GoogleSignIn get _google => _googleOverride ?? GoogleSignIn(hostedDomain: 'lgitech.net');

  User? get currentUser => _auth.currentUser;
  bool get isSignedIn => _auth.currentUser != null;

  /// Interactive Google sign-in. Returns the Firebase user on success.
  Future<User> signIn() async {
    final googleUser = await _google.signIn();
    if (googleUser == null) {
      throw const SupportAuthException('cancelled', 'Sign-in was cancelled.');
    }
    final googleAuth = await googleUser.authentication;
    final credential = GoogleAuthProvider.credential(
      accessToken: googleAuth.accessToken,
      idToken: googleAuth.idToken,
    );
    final result = await _auth.signInWithCredential(credential);
    final user = result.user;
    if (user == null) {
      throw const SupportAuthException('no_user', 'Sign-in produced no user.');
    }
    return user;
  }

  /// Bearer token for `Authorization` on every support-tier backend call.
  ///
  /// Firebase ID tokens expire after one hour. `getIdToken()` refreshes
  /// automatically when the cached token is near expiry — which is the main
  /// reason we chose Firebase over raw Google Sign-In, since a support
  /// operator will routinely be on a call longer than an hour.
  ///
  /// Pass `forceRefresh: true` after the backend returns 401, to handle a
  /// revoked session or a freshly-granted custom claim.
  Future<String> idToken({bool forceRefresh = false}) async {
    final user = _auth.currentUser;
    if (user == null) {
      throw const SupportAuthException('not_signed_in', 'No signed-in operator.');
    }
    final token = await user.getIdToken(forceRefresh);
    if (token == null) {
      throw const SupportAuthException('no_token', 'Could not obtain an ID token.');
    }
    return token;
  }

  /// Whether this operator carries the `admin` custom claim (gates data purge).
  ///
  /// Read from the token's claims rather than a local flag — the claim is set
  /// server-side via the Admin SDK and is the authoritative source.
  Future<bool> isAdmin({bool forceRefresh = false}) async {
    final user = _auth.currentUser;
    if (user == null) return false;
    final result = await user.getIdTokenResult(forceRefresh);
    return result.claims?['admin'] == true;
  }

  Future<void> signOut() async {
    await _google.signOut();
    await _auth.signOut();
  }
}
