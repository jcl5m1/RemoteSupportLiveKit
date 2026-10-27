// App entrypoint. Home is the open-meeting lobby (same calls as the web client
// at <base>/?call=<code>); the two-party support flow (docs/07-flutter-app.md)
// is one tap away.

import 'package:app_links/app_links.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'meet/meet_api.dart';
import 'meet/protocol.dart';
import 'meet/ui/lobby_screen.dart';
import 'providers.dart';
import 'screens/role_select.dart';
import 'screens/support_join.dart';
import 'screens/support_sign_in.dart';

const _appLinkHost = String.fromEnvironment(
  'APP_LINK_HOST',
  defaultValue: 'remotesupport.lgitech.net',
);

/// Open-meeting server, including its path prefix (no trailing slash).
const meetBaseUrl = String.fromEnvironment(
  'MEET_BASE_URL',
  defaultValue: 'https://remotesupport.lgitech.net/livekit',
);

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    await Firebase.initializeApp();
  } catch (_) {
    // Only the support sign-in needs Firebase; meetings work without it.
  }
  runApp(const RemoteSupportApp());
}

class RemoteSupportApp extends ConsumerStatefulWidget {
  const RemoteSupportApp({super.key});

  @override
  ConsumerState<RemoteSupportApp> createState() => _RemoteSupportAppState();
}

class _RemoteSupportAppState extends ConsumerState<RemoteSupportApp> {
  final _appLinks = AppLinks();
  final _navigatorKey = GlobalKey<NavigatorState>();
  final _meetApi = MeetApi(meetBaseUrl);
  String? _meetCode;

  @override
  void initState() {
    super.initState();
    _initDeepLinks();
  }

  Future<void> _initDeepLinks() async {
    Uri? initial;
    try {
      initial = await _appLinks.getInitialLink();
    } catch (_) {
      // Deep-link failure is not fatal; the user can still type the code.
    }

    // Defer link handling until after the first frame so ProviderScope is
    // available and the Navigator exists.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (initial != null) _handleLink(initial);
      _appLinks.uriLinkStream.listen(
        (uri) => WidgetsBinding.instance.addPostFrameCallback((_) => _handleLink(uri)),
        onError: (_) {},
      );
    });
  }

  /// Meeting links: <host>/livekit/?call=<code> (and the web's shorthands),
  /// remotesupport://call?code=<code>.
  String? _meetingCode(Uri uri) {
    if (uri.scheme == 'remotesupport') {
      return uri.host == 'call' ? callCodeFromText(uri.queryParameters['code'] ?? '') : null;
    }
    if (uri.host != _appLinkHost || uri.path.startsWith('/j/')) return null;
    final code = callCodeFromText(uri.toString());
    return code != null && callCodeRe.hasMatch(code) ? code : null;
  }

  void _handleLink(Uri uri) {
    final meeting = _meetingCode(uri);
    if (meeting != null) {
      _navigatorKey.currentState?.popUntil((r) => r.isFirst);
      setState(() => _meetCode = meeting);
      return;
    }
    if (uri.host != _appLinkHost && uri.scheme != 'remotesupport') return;
    final code = uri.queryParameters['code'];
    if (code == null || code.isEmpty) return;

    final nav = _navigatorKey.currentState;
    if (nav == null) return;

    // If the support operator is not signed in, send them through sign-in first
    // and carry the code forward.
    final auth = ref.read(supportAuthProvider);
    if (!auth.isSignedIn) {
      nav.pushAndRemoveUntil(
        MaterialPageRoute(
          builder: (_) => SupportSignInScreen(pendingJoinCode: code),
        ),
        (route) => route.isFirst,
      );
      return;
    }

    nav.pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => SupportJoinScreen(initialCode: code)),
      (route) => route.isFirst,
    );
  }

  @override
  Widget build(BuildContext context) {
    return ProviderScope(
      child: MaterialApp(
        navigatorKey: _navigatorKey,
        title: 'Remote Support',
        theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.indigo),
        darkTheme: ThemeData(
          useMaterial3: true,
          brightness: Brightness.dark,
          colorSchemeSeed: const Color(0xFF8AB4F8),
        ),
        themeMode: ThemeMode.dark,
        home: LobbyScreen(
          api: _meetApi,
          initialCode: _meetCode,
          onSupportSession: () => _navigatorKey.currentState?.push(
            MaterialPageRoute<void>(builder: (_) => const RoleSelectScreen()),
          ),
        ),
      ),
    );
  }
}
