// Support operator sign-in — Google SSO via Firebase.
// See docs/07-flutter-app.md § screen flow and docs/08 § trust model.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers.dart';
import '../services/support_auth.dart';
import 'support_join.dart';

class SupportSignInScreen extends ConsumerStatefulWidget {
  final String? pendingJoinCode;

  const SupportSignInScreen({super.key, this.pendingJoinCode});

  @override
  ConsumerState<SupportSignInScreen> createState() => _SupportSignInScreenState();
}

class _SupportSignInScreenState extends ConsumerState<SupportSignInScreen> {
  bool _loading = false;
  String? _error;

  Future<void> _signIn() async {
    setState(() {
      _loading = true;
      _error = null;
    });

    try {
      final auth = ref.read(supportAuthProvider);
      await auth.signIn();
      if (!mounted) return;

      Navigator.of(context).pushReplacement(
        MaterialPageRoute(
          builder: (_) => SupportJoinScreen(initialCode: widget.pendingJoinCode),
        ),
      );
    } on SupportAuthException catch (e) {
      String message;
      switch (e.code) {
        case 'cancelled':
          // Stay on screen silently.
          setState(() => _loading = false);
          return;
        case 'domain_not_allowed':
          message = 'Use your @lgitech.net account to sign in.';
          break;
        case 'email_not_verified':
          message = 'Verify your email with Google first, then try again.';
          break;
        case 'network':
          message = 'Network error. Check your connection and retry.';
          break;
        default:
          message = e.message;
      }
      setState(() => _error = message);
    } catch (e) {
      setState(() => _error = 'Sign-in failed. Please try again.');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Support sign-in')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24.0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Spacer(),
              Icon(Icons.support_agent_outlined, size: 80, color: Theme.of(context).colorScheme.primary),
              const SizedBox(height: 24),
              Text(
                'Sign in with Google',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.bold),
              ),
              const SizedBox(height: 8),
              Text(
                'Only authorized support operators can join calls.',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodyMedium?.copyWith(color: Colors.grey[600]),
              ),
              const Spacer(),
              if (_error != null) ...[
                Text(
                  _error!,
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
                const SizedBox(height: 16),
              ],
              FilledButton.icon(
                onPressed: _loading ? null : _signIn,
                icon: _loading
                    ? const SizedBox(
                        height: 18,
                        width: 18,
                        child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white),
                      )
                    : const Icon(Icons.login),
                label: Text(_loading ? 'Signing in…' : 'Continue with Google'),
              ),
              const SizedBox(height: 24),
            ],
          ),
        ),
      ),
    );
  }
}
