// Support join screen. Three entry paths, one resolution:
// 1) type a 6-char code, 2) scan a QR code, 3) deep link.
// See docs/07-flutter-app.md § SupportJoin.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../models/call_state.dart';
import '../providers.dart';
import '../services/api_client.dart';
import '../utils/join_code.dart';
import 'call_screen.dart';

class SupportJoinScreen extends ConsumerStatefulWidget {
  final String? initialCode;

  const SupportJoinScreen({super.key, this.initialCode});

  @override
  ConsumerState<SupportJoinScreen> createState() => _SupportJoinScreenState();
}

class _SupportJoinScreenState extends ConsumerState<SupportJoinScreen> {
  late final TextEditingController _codeController;
  bool _loading = false;
  String? _error;
  bool _showScanner = false;

  @override
  void initState() {
    super.initState();
    _codeController = TextEditingController(text: widget.initialCode ?? '');
  }

  @override
  void dispose() {
    _codeController.dispose();
    super.dispose();
  }

  String _normalizeCode(String raw) => normalizeJoinCode(raw);

  Future<void> _join() async {
    final code = _normalizeCode(_codeController.text.trim());
    if (code.length != 6) {
      setState(() => _error = 'Enter the 6-character code from the caller.');
      return;
    }

    setState(() {
      _loading = true;
      _error = null;
    });

    try {
      final auth = ref.read(supportAuthProvider);
      final api = ref.read(apiClientProvider);
      final idToken = await auth.idToken();
      final response = await api.joinSession(joinCode: code, idToken: idToken);
      if (!mounted) return;

      final livekit = response['livekit'] as Map<String, dynamic>?;
      if (livekit == null) {
        setState(() => _error = 'No room was returned. Please try again.');
        return;
      }

      final controller = ref.read(callControllerProvider.notifier);
      await controller.connect(
        wsUrl: livekit['ws_url'] as String,
        token: livekit['token'] as String,
        role: Role.support,
        sessionId: response['session_id'] as String,
        tokenExpiresAt: _parseIso(livekit['expires_at'] as String?),
        joinCode: code,
      );
      if (!mounted) return;
      Navigator.of(context).pushAndRemoveUntil(
        MaterialPageRoute(builder: (_) => const CallScreen()),
        (_) => false,
      );
    } on ApiException catch (e) {
      String message;
      switch (e.code) {
        case 'code_not_found':
          message = 'That code was not found. Check it and try again.';
          break;
        case 'code_expired':
          message = 'This code has expired. Ask the caller for a new one.';
          break;
        case 'role_occupied':
          message = 'Another support operator is already in this call.';
          break;
        case 'session_not_joinable':
          message = 'This session is not ready to join yet.';
          break;
        case 'domain_not_allowed':
          message = 'This account is not authorized for support access.';
          break;
        default:
          message = e.message;
      }
      setState(() => _error = message);
    } catch (e) {
      setState(() => _error = 'Could not join. Please try again.');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  DateTime? _parseIso(String? value) {
    if (value == null) return null;
    try {
      return DateTime.parse(value);
    } catch (_) {
      return null;
    }
  }

  void _onScan(BarcodeCapture capture) {
    final barcodes = capture.barcodes;
    final code = barcodes.isNotEmpty ? barcodes.first.rawValue : null;
    if (code == null) return;
    final uri = Uri.tryParse(code);
    final scanned = uri?.queryParameters['code'] ?? code;
    final normalized = _normalizeCode(scanned);
    setState(() {
      _codeController.text = normalized;
      _showScanner = false;
    });
    _join();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Join a call')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24.0),
          child: _showScanner ? _buildScanner() : _buildForm(),
        ),
      ),
    );
  }

  Widget _buildForm() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          'Enter the 6-character code',
          style: Theme.of(context).textTheme.headlineSmall,
        ),
        const SizedBox(height: 8),
        Text(
          'Or scan the QR code the caller is showing.',
          style: Theme.of(context).textTheme.bodyMedium?.copyWith(color: Colors.grey[600]),
        ),
        const SizedBox(height: 24),
        TextField(
          controller: _codeController,
          autofocus: true,
          textCapitalization: TextCapitalization.characters,
          maxLength: 6,
          style: const TextStyle(letterSpacing: 8, fontSize: 24, fontFamilyFallback: ['monospace']),
          decoration: InputDecoration(
            labelText: 'Join code',
            errorText: _error,
            border: const OutlineInputBorder(),
            counterText: '',
          ),
          onChanged: (value) {
            final normalized = _normalizeCode(value);
            if (normalized != value) {
              _codeController.text = normalized;
              _codeController.selection = TextSelection.fromPosition(
                TextPosition(offset: normalized.length),
              );
            }
          },
          onSubmitted: (_) => _join(),
        ),
        const SizedBox(height: 16),
        FilledButton(
          onPressed: _loading ? null : _join,
          child: _loading
              ? const SizedBox(
                  height: 20,
                  width: 20,
                  child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white),
                )
              : const Text('Join call'),
        ),
        const SizedBox(height: 12),
        OutlinedButton.icon(
          onPressed: _loading
              ? null
              : () => setState(() => _showScanner = true),
          icon: const Icon(Icons.qr_code_scanner),
          label: const Text('Scan QR code'),
        ),
      ],
    );
  }

  Widget _buildScanner() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Expanded(
          child: ClipRRect(
            borderRadius: BorderRadius.circular(12),
            child: MobileScanner(
              onDetect: _onScan,
            ),
          ),
        ),
        const SizedBox(height: 12),
        OutlinedButton(
          onPressed: () => setState(() => _showScanner = false),
          child: const Text('Cancel'),
        ),
      ],
    );
  }
}
