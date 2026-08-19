// REST client for the backend. Contract: docs/04-api-contract.md.

import 'package:dio/dio.dart';

/// Generic backend error. Code matches the server's `error.code` field so the UI
/// can show targeted messages (e.g. 410 -> "This code has expired").
class ApiException implements Exception {
  final int? statusCode;
  final String code;
  final String message;
  final Map<String, dynamic>? details;

  const ApiException({
    this.statusCode,
    required this.code,
    required this.message,
    this.details,
  });

  @override
  String toString() => 'ApiException($statusCode, $code): $message';
}

class ApiClient {
  final Dio _dio;
  String? _callerToken;

  ApiClient(String baseUrl)
      : _dio = Dio(
          BaseOptions(
            baseUrl: baseUrl,
            connectTimeout: const Duration(seconds: 10),
            receiveTimeout: const Duration(seconds: 10),
            contentType: Headers.jsonContentType,
          ),
        );

  /// The caller session JWT is scoped to one session and lives 60 minutes.
  /// It must be attached to every caller-tier call after session creation.
  void setCallerToken(String token) => _callerToken = token;

  /// POST /v1/sessions
  ///
  /// Returns the join code, links, a caller session JWT and the consent text.
  /// Deliberately returns **no LiveKit token** — that only comes from
  /// [recordConsent]. This is the consent gate (FR-7.1) and is why there is no
  /// window of unrecorded audio at the head of a call.
  Future<Map<String, dynamic>> createSession({
    required String deviceId,
    String? displayName,
    String locale = 'en-US',
  }) async {
    final resp = await _post(
      '/v1/sessions',
      data: {
        'device_id': deviceId,
        if (displayName != null) 'display_name': displayName,
        'locale': locale,
      },
    );
    return resp.data as Map<String, dynamic>;
  }

  /// POST /v1/sessions/{id}/consent — the call that actually starts everything:
  /// creates the room, dispatches the agent, returns the LiveKit token.
  Future<Map<String, dynamic>> recordConsent({
    required String sessionId,
    required bool accepted,
    required String consentTextVersion,
  }) async {
    final resp = await _post(
      '/v1/sessions/$sessionId/consent',
      token: _callerToken,
      data: {
        'accepted': accepted,
        'consent_text_version': consentTextVersion,
      },
    );
    return resp.data as Map<String, dynamic>;
  }

  /// POST /v1/sessions/join — support only.
  /// Handle 404 code_not_found, 410 code_expired, 409 role_occupied with
  /// distinct plain-language messages.
  Future<Map<String, dynamic>> joinSession({
    required String joinCode,
    required String idToken,
    String? displayName,
  }) async {
    final resp = await _post(
      '/v1/sessions/join',
      token: idToken,
      data: {
        'join_code': joinCode,
        if (displayName != null) 'display_name': displayName,
      },
    );
    return resp.data as Map<String, dynamic>;
  }

  /// POST /v1/sessions/{id}/token/refresh
  Future<Map<String, dynamic>> refreshToken(
    String sessionId, {
    String? idToken,
  }) async {
    final resp = await _post(
      '/v1/sessions/$sessionId/token/refresh',
      token: idToken ?? _callerToken,
    );
    return resp.data as Map<String, dynamic>;
  }

  /// POST /v1/sessions/{id}/end
  Future<void> endSession(String sessionId, {String? idToken}) async {
    await _post(
      '/v1/sessions/$sessionId/end',
      token: idToken ?? _callerToken,
    );
  }

  /// POST /v1/sessions/{id}/agent — support only. 403 for callers.
  Future<Map<String, dynamic>> setAiEnabled(
    String sessionId,
    bool enabled, {
    required String idToken,
    String? reason,
  }) async {
    final resp = await _post(
      '/v1/sessions/$sessionId/agent',
      token: idToken,
      data: {
        'enabled': enabled,
        if (reason != null) 'reason': reason,
      },
    );
    return resp.data as Map<String, dynamic>;
  }

  /// GET /v1/sessions/{id}/transcript — authoritative finals; the panel
  /// reconciles against this every 10s.
  Future<Map<String, dynamic>> getTranscript(
    String sessionId, {
    int sinceMs = 0,
    int limit = 500,
    String? cursor,
    String? idToken,
  }) async {
    final resp = await _get(
      '/v1/sessions/$sessionId/transcript',
      token: idToken ?? _callerToken,
      queryParameters: {
        'since_ms': sinceMs,
        'limit': limit,
        if (cursor != null) 'cursor': cursor,
      },
    );
    return resp.data as Map<String, dynamic>;
  }

  /// GET /v1/sessions/{id}/recordings
  Future<Map<String, dynamic>> getRecordings(
    String sessionId, {
    String? idToken,
  }) async {
    final resp = await _get(
      '/v1/sessions/$sessionId/recordings',
      token: idToken ?? _callerToken,
    );
    return resp.data as Map<String, dynamic>;
  }

  Future<Response<dynamic>> _post(
    String path, {
    String? token,
    Object? data,
  }) async {
    try {
      return await _dio.post(
        path,
        data: data,
        options: Options(headers: _authHeaders(token)),
      );
    } on DioException catch (e) {
      throw _mapError(e);
    }
  }

  Future<Response<dynamic>> _get(
    String path, {
    String? token,
    Map<String, dynamic>? queryParameters,
  }) async {
    try {
      return await _dio.get(
        path,
        queryParameters: queryParameters,
        options: Options(headers: _authHeaders(token)),
      );
    } on DioException catch (e) {
      throw _mapError(e);
    }
  }

  Map<String, String>? _authHeaders(String? token) {
    if (token == null) return null;
    return {'Authorization': 'Bearer $token'};
  }

  ApiException _mapError(DioException e) {
    final response = e.response;
    if (response != null && response.data is Map<String, dynamic>) {
      final body = response.data as Map<String, dynamic>;
      final error = body['error'] as Map<String, dynamic>?;
      if (error != null) {
        return ApiException(
          statusCode: response.statusCode,
          code: error['code'] as String? ?? 'unknown',
          message: error['message'] as String? ?? e.message ?? 'Request failed',
          details: error['details'] as Map<String, dynamic>?,
        );
      }
    }
    return ApiException(
      statusCode: response?.statusCode,
      code: 'network',
      message: e.message ?? 'Network error',
    );
  }
}
