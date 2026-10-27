// REST client for open "meet" calls (docs/04-api-contract.md, /v1/calls/...).
// The base URL includes the deployment's path prefix, e.g.
// https://remotesupport.lgitech.net/livekit (no trailing slash).

import 'dart:typed_data';

import 'package:dio/dio.dart';

import 'protocol.dart';

class MeetApiException implements Exception {
  final int? statusCode;
  final String code;
  final String message;
  const MeetApiException(this.statusCode, this.code, this.message);
  @override
  String toString() => message;
}

class CallInfo {
  final String callId;
  final bool recordingEnabled;
  final bool recordingActive;
  final bool roomActive;
  final int participantCount;
  final String? mapsApiKey;
  final String? mapsMapId;
  const CallInfo({
    required this.callId,
    this.recordingEnabled = false,
    this.recordingActive = false,
    this.roomActive = false,
    this.participantCount = 0,
    this.mapsApiKey,
    this.mapsMapId,
  });

  factory CallInfo.fromJson(Map<String, dynamic> j) => CallInfo(
        callId: j['call_id'] as String,
        recordingEnabled: j['recording_enabled'] == true,
        recordingActive: j['recording_active'] == true,
        roomActive: j['room_active'] == true,
        participantCount: (j['participant_count'] as num?)?.toInt() ?? 0,
        mapsApiKey: j['maps_api_key'] as String?,
        mapsMapId: j['maps_map_id'] as String?,
      );
}

class JoinResult {
  final String callId;
  final String wsUrl;
  final String token;
  final String identity;
  final bool recordingEnabled;
  const JoinResult(this.callId, this.wsUrl, this.token, this.identity, this.recordingEnabled);
}

class RecordingFile {
  final String name, participant, identity, contentType, url;
  final DateTime startedAt;
  final int sizeBytes;
  const RecordingFile(this.name, this.participant, this.identity, this.contentType, this.url, this.startedAt,
      this.sizeBytes);
}

class EventLogFile {
  final String name, url;
  final DateTime startedAt;
  final int sizeBytes;
  const EventLogFile(this.name, this.url, this.startedAt, this.sizeBytes);
}

class PhotoFile {
  final String name, participant, identity, contentType, path, url;
  final DateTime takenAt;
  final int sizeBytes;
  const PhotoFile(this.name, this.participant, this.identity, this.contentType, this.path, this.url, this.takenAt,
      this.sizeBytes);

  factory PhotoFile.fromJson(Map<String, dynamic> j) => PhotoFile(
        j['name'] as String,
        j['participant'] as String,
        j['identity'] as String,
        j['content_type'] as String,
        j['path'] as String,
        j['url'] as String,
        DateTime.parse(j['taken_at'] as String),
        (j['size_bytes'] as num).toInt(),
      );
}

class CallRecordings {
  final List<RecordingFile> recordings;
  final List<EventLogFile> eventLogs;
  final List<PhotoFile> photos;
  const CallRecordings(this.recordings, this.eventLogs, this.photos);
}

class MeetApi {
  final String baseUrl;
  final Dio _dio;

  MeetApi(String baseUrl, {Dio? dio})
      : baseUrl = baseUrl.replaceAll(RegExp(r'/+$'), ''),
        _dio = dio ??
            Dio(BaseOptions(
              connectTimeout: const Duration(seconds: 10),
              receiveTimeout: const Duration(seconds: 30),
            ));

  String _u(String path) => '$baseUrl$path';
  String _call(String callId) => '/v1/calls/${Uri.encodeComponent(callId)}';

  /// Shareable meeting link (same as the web lobby's copy button).
  String meetingLink(String callId) => '$baseUrl/?call=${Uri.encodeQueryComponent(callId)}';

  /// Absolute URL for an app path returned by the API (e.g. a photo path).
  String absolute(String path) => _u(path);

  Future<CallInfo> info(String callId) async =>
      CallInfo.fromJson(await _req('GET', _call(callId)) as Map<String, dynamic>);

  Future<JoinResult> join(String callId, String name) async {
    final j = await _req('POST', '${_call(callId)}/join', data: {'display_name': name}) as Map<String, dynamic>;
    final lk = j['livekit'] as Map<String, dynamic>;
    return JoinResult(j['call_id'] as String, lk['ws_url'] as String, lk['token'] as String,
        lk['identity'] as String, j['recording_enabled'] == true);
  }

  Future<CallRecordings> recordings(String callId) async {
    final j = await _req('GET', '${_call(callId)}/recordings') as Map<String, dynamic>;
    final recs = [
      for (final r in (j['recordings'] as List? ?? const []).cast<Map<String, dynamic>>())
        RecordingFile(r['name'] as String, r['participant'] as String, r['identity'] as String,
            r['content_type'] as String, r['url'] as String, DateTime.parse(r['started_at'] as String),
            (r['size_bytes'] as num).toInt()),
    ];
    final logs = [
      for (final r in (j['event_logs'] as List? ?? const []).cast<Map<String, dynamic>>())
        EventLogFile(r['name'] as String, r['url'] as String, DateTime.parse(r['started_at'] as String),
            (r['size_bytes'] as num).toInt()),
    ];
    final photos = [
      for (final r in (j['photos'] as List? ?? const []).cast<Map<String, dynamic>>()) PhotoFile.fromJson(r),
    ];
    return CallRecordings(recs, logs, photos);
  }

  /// action: start | stop | sync
  Future<bool> recording(String callId, String token, String action) async {
    final j = await _req('POST', '${_call(callId)}/recording/$action', token: token) as Map<String, dynamic>;
    return j['recording'] == true;
  }

  Future<void> end(String callId, String token) => _req('POST', '${_call(callId)}/end', token: token);

  /// Validates a "/show" link (resolves, public hosts only) and classifies it.
  Future<ShowCard> mediaCheck(String callId, String token, String url) async {
    final j = await _req('GET', '${_call(callId)}/media-check', token: token, query: {'url': url});
    final card = ShowCard.parse(j);
    if (card == null) throw const MeetApiException(null, 'media_unavailable', 'Unsupported link');
    return card;
  }

  Future<int> postEvents(String callId, String token, List<Map<String, Object?>> events, int sentAt) async {
    final j = await _req('POST', '${_call(callId)}/events',
        token: token, data: {'events': events, 'sent_at': sentAt}) as Map<String, dynamic>;
    return (j['stored'] as num?)?.toInt() ?? 0;
  }

  Future<PhotoFile> uploadPhoto(
    String callId,
    String token,
    Uint8List bytes, {
    required String contentType,
    required String requestedBy,
    int? width,
    int? height,
  }) async {
    final j = await _req(
      'POST',
      '${_call(callId)}/photos',
      token: token,
      query: {
        'requested_by': requestedBy,
        if (width != null) 'width': '$width',
        if (height != null) 'height': '$height',
      },
      data: Stream.fromIterable([bytes]),
      contentType: contentType,
      length: bytes.length,
    ) as Map<String, dynamic>;
    return PhotoFile.fromJson(j);
  }

  Future<dynamic> _req(
    String method,
    String path, {
    String? token,
    Object? data,
    Map<String, String>? query,
    String? contentType,
    int? length,
  }) async {
    try {
      final resp = await _dio.request<dynamic>(
        _u(path),
        data: data,
        queryParameters: query,
        options: Options(
          method: method,
          contentType: contentType ?? Headers.jsonContentType,
          headers: {
            if (token != null) 'Authorization': 'Bearer $token',
            if (length != null) Headers.contentLengthHeader: length,
          },
        ),
      );
      return resp.data;
    } on DioException catch (e) {
      final body = e.response?.data;
      final err = body is Map ? body['error'] : null;
      if (err is Map) {
        throw MeetApiException(e.response?.statusCode, '${err['code']}', '${err['message']}');
      }
      final status = e.response?.statusCode;
      throw MeetApiException(status, 'network', status != null ? 'HTTP $status' : (e.message ?? 'Network error'));
    }
  }
}
