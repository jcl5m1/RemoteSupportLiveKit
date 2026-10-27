// Full-screen "/show" viewer (web: openShowOverlay). Image: pinch / double-tap
// zoom. Audio: play/pause, repeat (off by default), timeline. Video: player.
// YouTube / web page: web view. Map: Google Maps (one-finger drag, map /
// satellite, my-location dot) in a web view whose origin is the app's base
// URL so the referrer-restricted browser key works; OpenStreetMap otherwise.

import 'dart:convert';
import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:video_player/video_player.dart';
import 'package:webview_flutter/webview_flutter.dart';
import 'package:webview_flutter_android/webview_flutter_android.dart';
import 'package:webview_flutter_wkwebview/webview_flutter_wkwebview.dart';

import '../protocol.dart';

class ShowViewer extends StatelessWidget {
  final ShowCard card;
  final String from;

  /// Base URL the map page loads under (Maps key referrer), e.g. https://host/livekit/
  final String baseUrl;
  final String? mapsApiKey;
  final String? mapsMapId;

  const ShowViewer({
    super.key,
    required this.card,
    required this.from,
    required this.baseUrl,
    this.mapsApiKey,
    this.mapsMapId,
  });

  static Future<void> open(BuildContext context, ShowCard card, String from,
          {required String baseUrl, String? mapsApiKey, String? mapsMapId}) =>
      Navigator.of(context).push(MaterialPageRoute<void>(
        fullscreenDialog: true,
        builder: (_) =>
            ShowViewer(card: card, from: from, baseUrl: baseUrl, mapsApiKey: mapsApiKey, mapsMapId: mapsMapId),
      ));

  String get title {
    if (card.title != null) return card.title!;
    final label = card.label;
    return '$from shared ${RegExp('^[aeiou]', caseSensitive: false).hasMatch(label) ? 'an' : 'a'} $label';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: const Color(0xFF202124),
        foregroundColor: Colors.white,
        automaticallyImplyLeading: false,
        title: Text(title, key: const Key('showTitle'), overflow: TextOverflow.ellipsis),
        actions: [
          TextButton(
            onPressed: () => launchUrl(Uri.parse(card.finalUrl), mode: LaunchMode.externalApplication),
            child: const Text('Open'),
          ),
          IconButton(
            key: const Key('showCloseBtn'),
            tooltip: 'Close',
            icon: const Icon(Icons.close),
            onPressed: () => Navigator.of(context).pop(),
          ),
        ],
      ),
      body: SafeArea(child: _body()),
    );
  }

  Widget _body() => switch (card.kind) {
        'image' => ZoomableImage(url: card.mediaUrl),
        'audio' => AudioCard(url: card.mediaUrl, name: Uri.parse(card.url).pathSegments.lastOrNull ?? card.url),
        'video' => VideoCard(url: card.mediaUrl),
        'map' when card.geo != null => WebCard(
            html: mapHtml(card.geo!, apiKey: mapsApiKey, mapId: mapsMapId),
            baseUrl: baseUrl,
            allowLocation: true,
          ),
        'youtube' || 'webpage' || 'map' => WebCard(url: card.mediaUrl),
        _ => Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(mainAxisSize: MainAxisSize.min, children: [
                SelectableText(card.finalUrl, style: const TextStyle(color: Colors.white)),
                const SizedBox(height: 16),
                FilledButton(
                  onPressed: () => launchUrl(Uri.parse(card.finalUrl), mode: LaunchMode.externalApplication),
                  child: const Text('Open link'),
                ),
              ]),
            ),
          ),
      };
}

/// Pinch to zoom (1-6x), pan, double-tap toggles 1x / 2.5x at the tap point.
class ZoomableImage extends StatefulWidget {
  final String url;
  const ZoomableImage({super.key, required this.url});
  @override
  State<ZoomableImage> createState() => _ZoomableImageState();
}

class _ZoomableImageState extends State<ZoomableImage> {
  final _tc = TransformationController();
  TapDownDetails? _tap;

  @override
  void dispose() {
    _tc.dispose();
    super.dispose();
  }

  void _toggle() {
    if (_tc.value.getMaxScaleOnAxis() > 1.01) {
      _tc.value = Matrix4.identity();
      return;
    }
    final p = _tap?.localPosition ?? Offset.zero;
    const s = 2.5;
    _tc.value = Matrix4.identity()
      ..translateByDouble(-p.dx * (s - 1), -p.dy * (s - 1), 0, 1)
      ..scaleByDouble(s, s, 1, 1);
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onDoubleTapDown: (d) => _tap = d,
      onDoubleTap: _toggle,
      child: InteractiveViewer(
        transformationController: _tc,
        minScale: 1,
        maxScale: 6,
        child: SizedBox.expand(
          child: Image.network(
            widget.url,
            key: const Key('showImage'),
            fit: BoxFit.contain,
            loadingBuilder: (context, child, p) =>
                p == null ? child : const Center(child: CircularProgressIndicator()),
            errorBuilder: (context, e, _) =>
                const Center(child: Text("Couldn't load the image", style: TextStyle(color: Colors.white70))),
          ),
        ),
      ),
    );
  }
}

String _fmt(Duration d) {
  final m = d.inMinutes;
  final s = d.inSeconds % 60;
  return '$m:${s.toString().padLeft(2, '0')}';
}

class _PlayerBar extends StatelessWidget {
  final VideoPlayerController c;
  final bool showRepeat;
  const _PlayerBar({required this.c, this.showRepeat = true});

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<VideoPlayerValue>(
      valueListenable: c,
      builder: (context, v, _) {
        final total = v.duration;
        final pos = v.position > total ? total : v.position;
        return Row(children: [
          IconButton(
            key: const Key('playPause'),
            color: Colors.white,
            iconSize: 36,
            icon: Icon(v.isPlaying ? Icons.pause_circle : Icons.play_circle),
            onPressed: () => v.isPlaying ? c.pause() : c.play(),
          ),
          if (showRepeat)
            IconButton(
              key: const Key('repeat'),
              tooltip: v.isLooping ? 'Repeat on' : 'Repeat off',
              color: v.isLooping ? const Color(0xFF8AB4F8) : Colors.white54,
              icon: const Icon(Icons.repeat),
              onPressed: () => c.setLooping(!v.isLooping),
            ),
          Expanded(
            child: Slider(
              value: total.inMilliseconds == 0 ? 0 : pos.inMilliseconds / total.inMilliseconds,
              onChanged: total.inMilliseconds == 0
                  ? null
                  : (f) => c.seekTo(Duration(milliseconds: (f * total.inMilliseconds).round())),
            ),
          ),
          Text('${_fmt(pos)} / ${_fmt(total)}', style: const TextStyle(color: Colors.white70)),
          const SizedBox(width: 12),
        ]);
      },
    );
  }
}

class AudioCard extends StatefulWidget {
  final String url;
  final String name;
  const AudioCard({super.key, required this.url, required this.name});
  @override
  State<AudioCard> createState() => _AudioCardState();
}

class _AudioCardState extends State<AudioCard> {
  late final VideoPlayerController _c = VideoPlayerController.networkUrl(Uri.parse(widget.url));
  String? _error;

  @override
  void initState() {
    super.initState();
    _c.initialize().then((_) {
      if (mounted) setState(() {});
      _c.play();
    }).catchError((Object e) {
      if (mounted) setState(() => _error = '$e');
    });
  }

  @override
  void dispose() {
    _c.dispose(); // closing stops playback
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Container(
        margin: const EdgeInsets.all(20),
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(color: const Color(0xFF292A2D), borderRadius: BorderRadius.circular(16)),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const Icon(Icons.audiotrack, color: Colors.white70, size: 40),
          const SizedBox(height: 8),
          Text(widget.name, style: const TextStyle(color: Colors.white), overflow: TextOverflow.ellipsis),
          const SizedBox(height: 8),
          if (_error != null)
            Text("Couldn't play: $_error", style: const TextStyle(color: Color(0xFFF28B82)))
          else
            _PlayerBar(c: _c),
        ]),
      ),
    );
  }
}

class VideoCard extends StatefulWidget {
  final String url;
  const VideoCard({super.key, required this.url});
  @override
  State<VideoCard> createState() => _VideoCardState();
}

class _VideoCardState extends State<VideoCard> {
  late final VideoPlayerController _c = VideoPlayerController.networkUrl(Uri.parse(widget.url));

  @override
  void initState() {
    super.initState();
    _c.initialize().then((_) {
      if (mounted) setState(() {});
      _c.play();
    });
  }

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(children: [
      Expanded(
        child: Center(
          child: _c.value.isInitialized
              ? AspectRatio(aspectRatio: _c.value.aspectRatio, child: VideoPlayer(_c))
              : const CircularProgressIndicator(),
        ),
      ),
      _PlayerBar(c: _c, showRepeat: false),
    ]);
  }
}

class WebCard extends StatefulWidget {
  final String? url;
  final String? html;
  final String? baseUrl;
  final bool allowLocation;
  const WebCard({super.key, this.url, this.html, this.baseUrl, this.allowLocation = false});
  @override
  State<WebCard> createState() => _WebCardState();
}

class _WebCardState extends State<WebCard> {
  late final WebViewController _c;

  @override
  void initState() {
    super.initState();
    final PlatformWebViewControllerCreationParams params =
        !kIsWeb && Platform.isIOS
            ? WebKitWebViewControllerCreationParams(
                allowsInlineMediaPlayback: true,
                mediaTypesRequiringUserAction: const <PlaybackMediaTypes>{},
              )
            : const PlatformWebViewControllerCreationParams();
    _c = WebViewController.fromPlatformCreationParams(params)
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(Colors.black);
    final platform = _c.platform;
    if (platform is AndroidWebViewController) {
      platform.setMediaPlaybackRequiresUserGesture(false);
      if (widget.allowLocation) {
        // The map's "my location" button: ask for location only when tapped.
        platform.setGeolocationPermissionsPromptCallbacks(onShowPrompt: (req) async {
          final status = await Permission.locationWhenInUse.request();
          return GeolocationPermissionsResponse(allow: status.isGranted, retain: false);
        });
      }
    }
    if (widget.html != null) {
      _c.loadHtmlString(widget.html!, baseUrl: widget.baseUrl);
    } else {
      _c.loadRequest(Uri.parse(widget.url!));
    }
  }

  @override
  Widget build(BuildContext context) => WebViewWidget(controller: _c);
}

/// Map page: Google Maps JS (greedy gestures = one-finger drag) when a key is
/// configured, else Leaflet + OpenStreetMap. "Locate" draws a blue dot.
String mapHtml(Geo geo, {String? apiKey, String? mapId}) {
  final cfg = jsonEncode({
    'lat': geo.lat,
    'lng': geo.lng,
    'zoom': geo.zoom,
    'label': geo.label,
    'key': apiKey,
    'mapId': mapId,
  });
  return '''<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>html,body,#map{margin:0;height:100%;background:#000}
#ctl{position:absolute;right:10px;bottom:24px;z-index:1000;display:flex;flex-direction:column;gap:8px}
#ctl button{width:44px;height:44px;border-radius:22px;border:none;background:#fff;font-size:20px;box-shadow:0 2px 6px #0006}
#type{position:absolute;left:10px;top:10px;z-index:1000;display:flex;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 2px 6px #0006}
#type button{border:none;background:#fff;padding:8px 12px;font-size:14px}#type button.on{background:#e8eaed;font-weight:600}</style>
</head><body><div id="map"></div>
<div id="type"><button id="tMap" class="on">Map</button><button id="tSat">Satellite</button></div>
<div id="ctl"><button id="locate" title="My location">&#x1F4CD;</button></div>
<script>
const C = $cfg;
let api = null;
function setType(sat){ document.getElementById('tMap').classList.toggle('on',!sat);
  document.getElementById('tSat').classList.toggle('on',sat); api && api.setType(sat); }
document.getElementById('tMap').onclick=()=>setType(false);
document.getElementById('tSat').onclick=()=>setType(true);
document.getElementById('locate').onclick=()=>{
  if(!navigator.geolocation) return;
  navigator.geolocation.watchPosition(p=>api&&api.me(p.coords.latitude,p.coords.longitude,p.coords.accuracy),
    ()=>{}, {enableHighAccuracy:true});
};
function leaflet(){
  const s=document.createElement('script'); s.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
  s.onload=()=>{ const m=L.map('map',{zoomControl:false}).setView([C.lat,C.lng],C.zoom);
    const street=L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap'}).addTo(m);
    const sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Esri'});
    const mk=L.marker([C.lat,C.lng]).addTo(m); if(C.label) mk.bindPopup(C.label).openPopup();
    let dot=null, acc=null, framed=false;
    api={setType:(on)=>{ if(on){m.removeLayer(street);sat.addTo(m);}else{m.removeLayer(sat);street.addTo(m);} },
      me:(la,ln,a)=>{ if(!dot){dot=L.circleMarker([la,ln],{radius:8,color:'#fff',weight:2,fillColor:'#1a73e8',fillOpacity:1}).addTo(m);
        acc=L.circle([la,ln],{radius:a,color:'#1a73e8',weight:1,fillOpacity:0.12}).addTo(m);} else {dot.setLatLng([la,ln]);acc.setLatLng([la,ln]).setRadius(a);}
        if(!framed){framed=true;m.fitBounds(L.latLngBounds([[la,ln],[C.lat,C.lng]]).pad(0.3));} }};
  }; document.head.appendChild(s);
}
function google(){
  window.gm_authFailure=()=>{ document.getElementById('map').innerHTML=''; api=null; leaflet(); };
  window.initMap=()=>{ const m=new google.maps.Map(document.getElementById('map'),{center:{lat:C.lat,lng:C.lng},zoom:C.zoom,
      gestureHandling:'greedy',disableDefaultUI:true,mapId:C.mapId||undefined});
    const mk=new google.maps.Marker({position:{lat:C.lat,lng:C.lng},map:m});
    if(C.label){ const iw=new google.maps.InfoWindow({content:'<div style="color:#202124">'+C.label.replace(/</g,'&lt;')+'</div>'}); iw.open({map:m,anchor:mk}); }
    let dot=null, acc=null, framed=false;
    api={setType:(on)=>m.setMapTypeId(on?'hybrid':'roadmap'),
      me:(la,ln,a)=>{ const p={lat:la,lng:ln};
        if(!dot){dot=new google.maps.Marker({position:p,map:m,icon:{path:google.maps.SymbolPath.CIRCLE,scale:8,fillColor:'#1a73e8',fillOpacity:1,strokeColor:'#fff',strokeWeight:2}});
          acc=new google.maps.Circle({map:m,center:p,radius:a,strokeColor:'#1a73e8',strokeWeight:1,fillColor:'#1a73e8',fillOpacity:0.12});}
        else {dot.setPosition(p);acc.setCenter(p);acc.setRadius(a);}
        if(!framed){framed=true;const b=new google.maps.LatLngBounds();b.extend(p);b.extend({lat:C.lat,lng:C.lng});m.fitBounds(b,60);} }};
  };
  const s=document.createElement('script');
  s.src='https://maps.googleapis.com/maps/api/js?key='+encodeURIComponent(C.key)+'&callback=initMap';
  s.onerror=()=>leaflet(); document.head.appendChild(s);
}
C.key ? google() : leaflet();
</script></body></html>''';
}
