// Blocking consent gate (FR-7.1). See docs/07-flutter-app.md § ConsentSheet.
//
// Non-dismissible. Renders `consent_text` and `consent_text_version` from the
// server response — never hardcode the wording, the server owns the version
// and old consent records must stay meaningful.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/call_state.dart';
import '../providers.dart';
import '../services/api_client.dart';
import 'waiting_room.dart';

class ConsentSheetScreen extends ConsumerStatefulWidget {
  final Map<String, dynamic> session;

  const ConsentSheetScreen({super.key, required this.session});

  @override
  ConsumerState<ConsentSheetScreen> createState() => _ConsentSheetScreenState();
}

class _ConsentSheetScreenState extends ConsumerState<ConsentSheetScreen> {
  bool _loading = false;
  String? _error;

  String get _sessionId => widget.session['session_id'] as String;
  String get _consentText => widget.session['consent_text'] as String? ?? '';
  String get _consentVersion => widget.session['consent_text_version'] as String? ?? '';
  String get _callerToken => widget.session['caller_session_token'] as String? ?? '';

  @override
  void initState() {
    super.initState();
    ref.read(apiClientProvider).setCallerToken(_callerToken);
  }

  Future<void> _accept() async => _submit(true);

  Future<void> _decline() async => _submit(false);

  Future<void> _submit(bool accepted) async {
    setState(() {
      _loading = true;
      _error = null;
    });

    try {
      final api = ref.read(apiClientProvider);
      final response = await api.recordConsent(
        sessionId: _sessionId,
        accepted: accepted,
        consentTextVersion: _consentVersion,
      );
      if (!mounted) return;

      if (accepted) {
        final livekit = response['livekit'] as Map<String, dynamic>?;
        if (livekit == null) {
          setState(() => _error = 'No room was created. Please try again.');
          return;
        }
        final controller = ref.read(callControllerProvider.notifier);
        await controller.connect(
          wsUrl: livekit['ws_url'] as String,
          token: livekit['token'] as String,
          role: Role.caller,
          sessionId: _sessionId,
          tokenExpiresAt: _parseIso(livekit['expires_at'] as String?),
          joinCode: widget.session['join_code'] as String?,
        );
        if (!mounted) return;
        Navigator.of(context).pushAndRemoveUntil(
          MaterialPageRoute(
            builder: (_) => WaitingRoomScreen(joinCode: widget.session['join_code'] as String? ?? ''),
          ),
          (_) => false,
        );
      } else {
        final recordingEnabled = widget.session['recording_enabled'] as bool? ?? true;
        if (recordingEnabled) {
          // Without the fallback flag the session ends here.
          Navigator.of(context).pushAndRemoveUntil(
            MaterialPageRoute(
              builder: (_) => const _DeclinedSummaryScreen(),
            ),
            (_) => false,
          );
        } else {
          // Fallback: proceed unrecorded. The server still creates a room but
          // disables egress. Live path identical to accepted for the caller.
          final livekit = response['livekit'] as Map<String, dynamic>?;
          if (livekit != null) {
            final controller = ref.read(callControllerProvider.notifier);
            await controller.connect(
              wsUrl: livekit['ws_url'] as String,
              token: livekit['token'] as String,
              role: Role.caller,
              sessionId: _sessionId,
              tokenExpiresAt: _parseIso(livekit['expires_at'] as String?),
              joinCode: widget.session['join_code'] as String?,
            );
          }
          if (!mounted) return;
          Navigator.of(context).pushAndRemoveUntil(
            MaterialPageRoute(
              builder: (_) => WaitingRoomScreen(
                joinCode: widget.session['join_code'] as String? ?? '',
                unrecorded: true,
              ),
            ),
            (_) => false,
          );
        }
      }
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } catch (e) {
      setState(() => _error = 'Could not record consent. Please try again.');
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

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Consent to record'),
          automaticallyImplyLeading: false,
        ),
        body: SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(24.0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Expanded(
                  child: SingleChildScrollView(
                    child: Text(
                      _consentText,
                      style: Theme.of(context).textTheme.bodyLarge,
                    ),
                  ),
                ),
                if (_error != null) ...[
                  const SizedBox(height: 16),
                  Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
                ],
                const SizedBox(height: 16),
                FilledButton(
                  onPressed: _loading ? null : _accept,
                  child: _loading
                      ? const SizedBox(
                          height: 20,
                          width: 20,
                          child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white),
                        )
                      : const Text('I agree — start the call'),
                ),
                const SizedBox(height: 12),
                OutlinedButton(
                  onPressed: _loading ? null : _decline,
                  child: const Text('I decline'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _DeclinedSummaryScreen extends StatelessWidget {
  const _DeclinedSummaryScreen();

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24.0),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Icon(Icons.mic_off_outlined, size: 64, color: Theme.of(context).colorScheme.error),
              const SizedBox(height: 16),
              Text(
                'This call needs your consent to be recorded.',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const SizedBox(height: 8),
              const Text(
                'You declined, so the session has ended. You can start a new one any time.',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 24),
              FilledButton(
                onPressed: () => Navigator.of(context).popUntil((route) => route.isFirst),
                child: const Text('Back to start'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
