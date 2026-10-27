// Wire protocol shared with the web client (backend/app/static/support-web/
// app.js) for open "meet" calls. Everything here must stay byte-compatible
// with the browser so phones and browsers can be in the same call.
//
// Data channel topics:
//   chat     {text, name, ts, show?}            -- chat; "/show" carries a media card
//   pointer  {target, x, y, color, ts}          -- tap on a video (0..1 of the frame)
//   camctl   {type: state|cmd|result, ...}      -- remote phone camera control

import 'dart:convert';
import 'dart:math';

const chatTopic = 'chat';
const pointerTopic = 'pointer';
const camctlTopic = 'camctl';

// ---------------------------------------------------------------------------
// Meeting codes (mirror routers/calls.py _CALL_ID_RE and the web lobby).

final callCodeRe = RegExp(r'^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$');

String normalizeCallCode(String raw) =>
    raw.trim().toLowerCase().replaceAll(RegExp(r'[\s_]+'), '-');

/// Meet-style `xxx-xxxx-xxx`, ~50 bits (no 'l', like the server and web).
String newCallCode([Random? random]) {
  const alphabet = 'abcdefghijkmnopqrstuvwxyz';
  final r = random ?? Random.secure();
  final c = List.generate(10, (_) => alphabet[r.nextInt(alphabet.length)]);
  return '${c.sublist(0, 3).join()}-${c.sublist(3, 7).join()}-${c.sublist(7).join()}';
}

/// A meeting code from whatever was pasted: a bare code, `<base>/?call=x`,
/// `?=x`, bare `?x`, `/call/x`, or `remotesupport://call?code=x`.
String? callCodeFromText(String text) {
  final t = text.trim();
  if (t.isEmpty) return null;
  final uri = Uri.tryParse(t);
  if (uri != null && (uri.hasScheme || t.contains('?') || t.contains('/'))) {
    // "?=team-standup": Uri drops the empty-named parameter, so read it raw.
    if (uri.query.startsWith('=')) {
      final v = Uri.decodeQueryComponent(uri.query.substring(1).split('&').first);
      if (v.isNotEmpty) return normalizeCallCode(v);
    }
    final q = uri.queryParameters;
    for (final key in ['call', 'code']) {
      final v = q[key];
      if (v != null && v.isNotEmpty) return normalizeCallCode(v);
    }
    // Bare "?team-standup": a single key with no value.
    if (q.length == 1 && q.values.first.isEmpty) return normalizeCallCode(q.keys.first);
    final segs = uri.pathSegments.where((s) => s.isNotEmpty).toList();
    final i = segs.indexOf('call');
    if (i >= 0 && i + 1 < segs.length) return normalizeCallCode(segs[i + 1]);
    if (uri.hasScheme) return null;
  }
  return normalizeCallCode(t);
}

// ---------------------------------------------------------------------------
// Participant colours: same palette and ordering as the web, so every screen
// agrees (Okabe-Ito first, then Paul Tol bright), ranked by server join time.

const participantColors = [
  '#E69F00', '#56B4E9', '#009E73', '#F0E442', '#0072B2', '#D55E00',
  '#CC79A7', '#EE6677', '#228833', '#AA3377', '#BBBBBB',
];

class ColorEntry {
  final String identity;
  final DateTime? joinedAt;
  const ColorEntry(this.identity, this.joinedAt);
}

/// identity -> colour. Agents are excluded (web does the same).
Map<String, String> assignColors(Iterable<ColorEntry> people) {
  final list = people.where((p) => p.identity.isNotEmpty && !p.identity.startsWith('agent')).toList()
    ..sort((a, b) {
      final ta = a.joinedAt?.millisecondsSinceEpoch ?? 0;
      final tb = b.joinedAt?.millisecondsSinceEpoch ?? 0;
      return ta != tb ? ta.compareTo(tb) : a.identity.compareTo(b.identity);
    });
  return {
    for (var i = 0; i < list.length; i++)
      list[i].identity: participantColors[i % participantColors.length],
  };
}

// ---------------------------------------------------------------------------
// "/show" media cards.

const showKinds = ['image', 'audio', 'video', 'youtube', 'map', 'webpage', 'link'];
const showKindLabels = {
  'image': 'image', 'audio': 'audio clip', 'video': 'video', 'youtube': 'YouTube video',
  'map': 'map', 'webpage': 'web page', 'link': 'link',
};
final showRe = RegExp(r'^/show\s+(\S+)\s*$', caseSensitive: false);

bool isHttpUrl(Object? v) {
  if (v is! String) return false;
  final u = Uri.tryParse(v);
  return u != null && (u.scheme == 'http' || u.scheme == 'https') && u.host.isNotEmpty;
}

class Geo {
  final double lat;
  final double lng;
  final double zoom;
  final String? label;
  const Geo(this.lat, this.lng, this.zoom, this.label);

  static Geo? parse(Object? g) {
    if (g is! Map) return null;
    final lat = _num(g['lat']);
    final lng = _num(g['lng']);
    if (lat == null || lng == null || lat.abs() > 90 || lng.abs() > 180) return null;
    final zoom = _num(g['zoom']);
    final label = g['label'];
    return Geo(
      lat,
      lng,
      zoom == null ? 16 : zoom.clamp(2, 19).toDouble(),
      label is String ? _cut(label, 200) : null,
    );
  }

  Map<String, Object?> toJson() => {'lat': lat, 'lng': lng, 'zoom': zoom, 'label': label};
}

class ShowCard {
  final String kind;
  final String url;
  final String finalUrl;
  final String? embedUrl;
  final Geo? geo;
  final String? title;
  const ShowCard({
    required this.kind,
    required this.url,
    required this.finalUrl,
    this.embedUrl,
    this.geo,
    this.title,
  });

  /// Same checks as the web's sanitizeShow: only known kinds, http(s) URLs.
  static ShowCard? parse(Object? s) {
    if (s is! Map || !showKinds.contains(s['kind'])) return null;
    if (!isHttpUrl(s['url']) || !isHttpUrl(s['final_url'])) return null;
    final embed = s['embed_url'];
    if (embed != null && !isHttpUrl(embed)) return null;
    final title = s['title'];
    return ShowCard(
      kind: s['kind'] as String,
      url: s['url'] as String,
      finalUrl: s['final_url'] as String,
      embedUrl: embed as String?,
      geo: Geo.parse(s['geo']),
      title: title is String ? _cut(title, 120) : null,
    );
  }

  /// What plays/loads: the embed URL when there is one.
  String get mediaUrl => embedUrl ?? finalUrl;
  String get label => showKindLabels[kind] ?? 'link';

  Map<String, Object?> toJson() => {
        'kind': kind,
        'url': url,
        'final_url': finalUrl,
        'embed_url': embedUrl,
        if (geo != null) 'geo': geo!.toJson(),
        if (title != null) 'title': title,
      };
}

// ---------------------------------------------------------------------------
// Chat.

class ChatMessage {
  final String identity;
  final String name;
  final String text;
  final int ts;
  final bool mine;
  final ShowCard? show;
  const ChatMessage({
    required this.identity,
    required this.name,
    required this.text,
    required this.ts,
    required this.mine,
    this.show,
  });
}

List<int> encodeChat({required String text, required String name, required int ts, ShowCard? show}) =>
    utf8.encode(jsonEncode({
      'text': text,
      'name': name,
      'ts': ts,
      if (show != null) 'show': show.toJson(),
    }));

/// Incoming chat payload -> message (null if malformed).
ChatMessage? decodeChat(List<int> payload, {required String identity, required String name}) {
  final msg = _json(payload);
  if (msg == null || msg['text'] is! String) return null;
  final ts = msg['ts'];
  return ChatMessage(
    identity: identity,
    name: name,
    text: msg['text'] as String,
    ts: ts is int ? ts : DateTime.now().millisecondsSinceEpoch,
    mine: false,
    show: ShowCard.parse(msg['show']),
  );
}

// ---------------------------------------------------------------------------
// Pointer.

class PointerMsg {
  final String target;
  final double x;
  final double y;
  final String? color;
  const PointerMsg(this.target, this.x, this.y, this.color);
}

double _round4(double v) => (v * 10000).roundToDouble() / 10000;

List<int> encodePointer({
  required String target,
  required double x,
  required double y,
  required String color,
  required int ts,
}) =>
    utf8.encode(jsonEncode({'target': target, 'x': _round4(x), 'y': _round4(y), 'color': color, 'ts': ts}));

PointerMsg? decodePointer(List<int> payload) {
  final m = _json(payload);
  if (m == null || m['target'] is! String) return null;
  final x = _num(m['x']);
  final y = _num(m['y']);
  if (x == null || y == null || x < 0 || x > 1 || y < 0 || y > 1) return null;
  final c = m['color'];
  // Only palette colours are accepted from the wire.
  return PointerMsg(m['target'] as String, x, y, participantColors.contains(c) ? c as String : null);
}

/// Where a video frame of [frameW]x[frameH] is drawn in a [viewW]x[viewH] box.
class FrameRect {
  final double x, y, w, h, viewW, viewH;
  const FrameRect(this.x, this.y, this.w, this.h, this.viewW, this.viewH);

  static FrameRect? fit(double frameW, double frameH, double viewW, double viewH, {required bool cover}) {
    if (frameW <= 0 || frameH <= 0 || viewW <= 0 || viewH <= 0) return null;
    final sx = viewW / frameW;
    final sy = viewH / frameH;
    final s = cover ? max(sx, sy) : min(sx, sy);
    final w = frameW * s;
    final h = frameH * s;
    return FrameRect((viewW - w) / 2, (viewH - h) / 2, w, h, viewW, viewH);
  }

  /// View point -> normalized, unmirrored frame point (null if off the frame).
  ({double x, double y})? toFrame(double px, double py, {required bool mirrored}) {
    var u = (px - x) / w;
    final v = (py - y) / h;
    if (mirrored) u = 1 - u;
    if (u < 0 || u > 1 || v < 0 || v > 1) return null;
    return (x: u, y: v);
  }

  /// Normalized frame point -> view point, clamped to what's visible.
  ({double x, double y}) toView(double fx, double fy, {required bool mirrored}) {
    final u = mirrored ? 1 - fx : fx;
    return (x: (x + u * w).clamp(0, viewW).toDouble(), y: (y + fy * h).clamp(0, viewH).toDouble());
  }

  /// Ring diameter: ~12% of the view, 18..96 px (web: same).
  double get ringSize => (0.12 * min(viewW, viewH)).clamp(18, 96).roundToDouble();
}

// ---------------------------------------------------------------------------
// Remote phone camera control.

class CamState {
  final bool phone;
  final bool cam;
  final int cams;
  final bool torch;
  final bool torchOn;
  const CamState({this.phone = false, this.cam = false, this.cams = 0, this.torch = false, this.torchOn = false});

  Map<String, Object?> toJson() =>
      {'type': 'state', 'phone': phone, 'cam': cam, 'cams': cams, 'torch': torch, 'torchOn': torchOn};

  static CamState fromJson(Map m) => CamState(
        phone: m['phone'] == true,
        cam: m['cam'] == true,
        cams: (_num(m['cams']) ?? 0).clamp(0, 16).toInt(),
        torch: m['torch'] == true,
        torchOn: m['torchOn'] == true,
      );

  @override
  bool operator ==(Object other) => other is CamState && jsonEncode(toJson()) == jsonEncode(other.toJson());
  @override
  int get hashCode => jsonEncode(toJson()).hashCode;
}

const camOps = ['cycle', 'torch', 'photo'];

sealed class CamCtl {
  const CamCtl();

  static CamCtl? decode(List<int> payload) {
    final m = _json(payload);
    if (m == null) return null;
    switch (m['type']) {
      case 'state':
        return CamStateMsg(CamState.fromJson(m));
      case 'cmd':
        final op = m['op'];
        if (!camOps.contains(op)) return null;
        return CamCmd(_cut('${m['id'] ?? ''}', 64), op as String, on: m['on'] == true);
      case 'result':
        final err = m['error'];
        return CamResult(
          _cut('${m['id'] ?? ''}', 64),
          '${m['op'] ?? ''}',
          ok: m['ok'] == true,
          error: err is String ? _cut(err, 200) : null,
          photo: m['photo'] is String ? m['photo'] as String : null,
        );
    }
    return null;
  }

  List<int> encode();
}

class CamStateMsg extends CamCtl {
  final CamState state;
  const CamStateMsg(this.state);
  @override
  List<int> encode() => utf8.encode(jsonEncode(state.toJson()));
}

class CamCmd extends CamCtl {
  final String id;
  final String op;
  final bool on;
  const CamCmd(this.id, this.op, {this.on = false});
  @override
  List<int> encode() =>
      utf8.encode(jsonEncode({'type': 'cmd', 'op': op, 'id': id, if (op == 'torch') 'on': on}));
}

class CamResult extends CamCtl {
  final String id;
  final String op;
  final bool ok;
  final String? error;
  final String? photo;
  const CamResult(this.id, this.op, {required this.ok, this.error, this.photo});
  @override
  List<int> encode() => utf8.encode(jsonEncode({
        'type': 'result',
        'id': id,
        'op': op,
        'ok': ok,
        if (error != null) 'error': error,
        if (photo != null) 'photo': photo,
      }));
}

// ---------------------------------------------------------------------------
// Recording state lives in room metadata (server-written).

class RecordingMeta {
  final bool recording;
  final String? startedBy;
  final String? stoppedBy;
  final int skipped;
  const RecordingMeta({this.recording = false, this.startedBy, this.stoppedBy, this.skipped = 0});

  static RecordingMeta parse(String? metadata) {
    if (metadata == null || metadata.isEmpty) return const RecordingMeta();
    try {
      final m = jsonDecode(metadata);
      if (m is! Map) return const RecordingMeta();
      return RecordingMeta(
        recording: m['recording'] == true,
        startedBy: m['started_by'] as String?,
        stoppedBy: m['stopped_by'] as String?,
        skipped: (_num(m['skipped']) ?? 0).toInt(),
      );
    } catch (_) {
      return const RecordingMeta();
    }
  }
}

// ---------------------------------------------------------------------------
// Data log (side panel "Data" tab): description of a packet, never the payload.

String describePayload(String? topic, List<int> bytes) {
  final m = _json(bytes);
  if (topic == chatTopic && m != null && m['text'] is String) {
    final show = ShowCard.parse(m['show']);
    final text = m['text'] as String;
    if (show != null) return 'Shared ${show.label}';
    return 'Chat message (${text.length} chars)';
  }
  if (topic == pointerTopic && m != null) return 'Pointer';
  if (topic == camctlTopic && m != null) {
    return switch (m['type']) {
      'state' => 'Camera state',
      'cmd' => 'Camera command (${m['op']})',
      'result' => 'Camera result (${m['op']})',
      _ => 'Camera control',
    };
  }
  if (m != null) return 'JSON (${m.keys.take(4).join(', ')})';
  return 'Binary data';
}

String fmtBytes(int n) => n >= 1000000
    ? '${(n / 1e6).toStringAsFixed(1)} MB'
    : n >= 1000
        ? '${(n / 1e3).round()} KB'
        : '$n B';

// ---------------------------------------------------------------------------

Map<String, dynamic>? _json(List<int> bytes) {
  try {
    final v = jsonDecode(utf8.decode(bytes));
    return v is Map<String, dynamic> ? v : null;
  } catch (_) {
    return null;
  }
}

double? _num(Object? v) {
  if (v is num) return v.isFinite ? v.toDouble() : null;
  if (v is String) return double.tryParse(v);
  return null;
}

String _cut(String s, int n) => s.length <= n ? s : s.substring(0, n);
