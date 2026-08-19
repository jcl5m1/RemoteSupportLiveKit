// Join-code normalization utilities. See docs/07-flutter-app.md § SupportJoin.

/// Maps ambiguous characters to unambiguous ones (FR-1.3):
///   I, L → 1; O → 0; U → V.
/// The result is upper-cased. Non-alphanumeric input is left in place so the
/// caller can decide whether to reject based on length.
String normalizeJoinCode(String raw) {
  return raw
      .toUpperCase()
      .replaceAll('I', '1')
      .replaceAll('L', '1')
      .replaceAll('O', '0')
      .replaceAll('U', 'V');
}
