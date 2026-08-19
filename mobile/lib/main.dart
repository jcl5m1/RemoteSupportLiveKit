// App entrypoint. One binary, both roles — role is chosen at runtime.
// Screen flow: docs/07-flutter-app.md.

import 'package:app_links/app_links.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'providers.dart';
import 'screens/role_select.dart';
import 'screens/support_join.dart';
import 'screens/support_sign_in.dart';

const _appLinkHost = String.fromEnvironment(
  'APP_LINK_HOST',
  defaultValue: 'remotesupport.lgitech.net',
);

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Firebase.initializeApp();
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

  void _handleLink(Uri uri) {
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
        home: const RoleSelectScreen(),
      ),
    );
  }
}
