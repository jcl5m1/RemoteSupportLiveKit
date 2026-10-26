import 'dart:async';

import 'package:app_links/app_links.dart';
import 'package:flutter/material.dart';

import 'call_screen.dart';
import 'src/room_controller.dart';

void main() {
  runApp(const TranslationApp());
}

/// Parses `translationmobile://join?url=&token=&identity=&lang=`.
/// Returns null if the link doesn't match (kept pure for unit tests).
JoinInfo? parseJoinLink(String link) {
  final uri = Uri.tryParse(link);
  if (uri == null || uri.scheme != 'translationmobile') return null;
  final url = uri.queryParameters['url'];
  final token = uri.queryParameters['token'];
  final identity = uri.queryParameters['identity'];
  if (url == null || token == null || identity == null) return null;
  return JoinInfo(
    serverUrl: url,
    token: token,
    identity: identity,
    language: uri.queryParameters['lang'] ?? 'en',
  );
}

class JoinInfo {
  final String serverUrl;
  final String token;
  final String identity;
  final String language;

  const JoinInfo({
    required this.serverUrl,
    required this.token,
    required this.identity,
    required this.language,
  });
}

class TranslationApp extends StatelessWidget {
  const TranslationApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Translation Call',
      theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true),
      home: const JoinScreen(),
    );
  }
}

class JoinScreen extends StatefulWidget {
  const JoinScreen({super.key});

  @override
  State<JoinScreen> createState() => _JoinScreenState();
}

class _JoinScreenState extends State<JoinScreen> {
  final _url = TextEditingController();
  final _token = TextEditingController();
  final _identity = TextEditingController(text: 'user-en');
  String _language = 'en';
  bool _busy = false;
  StreamSubscription<Uri>? _linkSub;
  final _appLinks = AppLinks();

  @override
  void initState() {
    super.initState();
    // Deep-link join: lets scripts/emulators launch the app pre-filled via
    //   adb shell am start -a android.intent.action.VIEW \
    //     -d "translationmobile://join?url=...&token=...&identity=...&lang=zh"
    _initDeepLink();
  }

  Future<void> _initDeepLink() async {
    try {
      final initial = await _appLinks.getInitialLink();
      if (initial != null) _applyLink(initial.toString());
    } catch (_) {}
    _linkSub = _appLinks.uriLinkStream.listen((uri) {
      _applyLink(uri.toString());
    });
  }

  void _applyLink(String link) {
    final info = parseJoinLink(link);
    if (info == null) return;
    setState(() {
      _url.text = info.serverUrl;
      _token.text = info.token;
      _identity.text = info.identity;
      _language = info.language;
    });
    _join(info);
  }

  @override
  void dispose() {
    _linkSub?.cancel();
    _url.dispose();
    _token.dispose();
    _identity.dispose();
    super.dispose();
  }

  Future<void> _join([JoinInfo? info]) async {
    setState(() => _busy = true);
    final client = LiveKitTranslationClient();
    try {
      await client.connect(
        serverUrl: info?.serverUrl ?? _url.text.trim(),
        token: info?.token ?? _token.text.trim(),
        identity: info?.identity ?? _identity.text.trim(),
        preferredLanguage: info?.language ?? _language,
      );
      if (!mounted) return;
      await Navigator.of(context).push(
        MaterialPageRoute(builder: (_) => CallScreen(client: client)),
      );
    } catch (_) {
      // Error state is surfaced by the client; stay on the join screen.
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Join translation call')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(
            controller: _url,
            decoration: const InputDecoration(
              labelText: 'LiveKit server URL',
              hintText: 'wss://your-project.livekit.cloud',
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _token,
            decoration: const InputDecoration(
              labelText: 'Access token',
            ),
            obscureText: true,
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _identity,
            decoration: const InputDecoration(
              labelText: 'Identity (must match the token)',
            ),
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            initialValue: _language,
            decoration: const InputDecoration(labelText: 'My language'),
            items: const [
              DropdownMenuItem(value: 'en', child: Text('English')),
              DropdownMenuItem(value: 'zh', child: Text('Mandarin 中文')),
              DropdownMenuItem(value: 'es', child: Text('Español')),
              DropdownMenuItem(value: 'tl', child: Text('Tagalog')),
            ],
            onChanged: (v) => setState(() => _language = v ?? 'en'),
          ),
          const SizedBox(height: 24),
          FilledButton.icon(
            onPressed: _busy ? null : () => _join(),
            icon: _busy
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.call),
            label: Text(_busy ? 'Joining…' : 'Join call'),
          ),
        ],
      ),
    );
  }
}
