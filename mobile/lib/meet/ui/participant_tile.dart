// One participant's video: name chip with their colour, speaking border,
// pointer rings and source flash, and (on a phone's big view) the remote
// camera controls. Tap: maximize a small/tiled video, or point on the single
// big one -- the same rules as the web client.

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:livekit_client/livekit_client.dart' hide ChatMessage;

import '../meet_controller.dart';
import '../protocol.dart';

Color hexColor(String hex) => Color(int.parse('FF${hex.substring(1)}', radix: 16));

class ParticipantTile extends StatefulWidget {
  final TileModel tile;
  final MeetController controller;

  /// Single big view on a phone: edge to edge, crop to fill when the shapes match.
  final bool solo;

  const ParticipantTile({super.key, required this.tile, required this.controller, this.solo = false});

  @override
  State<ParticipantTile> createState() => _ParticipantTileState();
}

class _Ring {
  final double x, y;
  final String color;
  final String? from;
  final Key key = UniqueKey();
  _Ring(this.x, this.y, this.color, this.from);
}

class _ParticipantTileState extends State<ParticipantTile> with TickerProviderStateMixin {
  final _renderer = rtc.RTCVideoRenderer();
  bool _rendererReady = false;
  final _rings = <_Ring>[];
  StreamSubscription<TileEffect>? _sub;
  late final AnimationController _flash =
      AnimationController(vsync: this, duration: const Duration(milliseconds: 1000));
  Color _flashColor = Colors.white;
  int _cams = 0;
  bool _disposed = false;
  (double, double) _size = (0, 0); // displayed frame size (rotation applied)

  @override
  void initState() {
    super.initState();
    _renderer.initialize().then((_) {
      if (_disposed) {
        _renderer.dispose(); // the tile went away while initializing
        return;
      }
      _renderer.addListener(_onVideoSize);
      setState(() => _rendererReady = true);
    });
    _sub = widget.controller.effects.where((e) => e.identity == widget.tile.identity).listen(_onEffect);
    _countCameras();
  }

  Future<void> _countCameras() async {
    if (!widget.tile.isLocal) return;
    final n = (await widget.controller.videoInputs()).length;
    if (mounted) setState(() => _cams = n);
  }

  /// The renderer notifies on every srcObject assignment (VideoTrackRenderer
  /// re-attaches on each build), so rebuild only when the size really changes.
  /// Frames arrive unrotated: swap for 90/270 so this is what is on screen.
  void _onVideoSize() {
    final v = _renderer.value;
    final turned = v.rotation == 90 || v.rotation == 270;
    final size = turned ? (v.height, v.width) : (v.width, v.height);
    if (size == _size || !mounted) return;
    setState(() => _size = size);
  }

  void _onEffect(TileEffect e) {
    if (!mounted) return;
    if (e.ring) {
      final ring = _Ring(e.x, e.y, e.color, e.from);
      setState(() => _rings.add(ring));
      Timer(const Duration(milliseconds: 1000), () {
        if (mounted) setState(() => _rings.remove(ring));
      });
    } else {
      _flashColor = hexColor(e.color);
      _flash.forward(from: 0);
    }
  }

  @override
  void dispose() {
    _disposed = true;
    _sub?.cancel();
    _flash.dispose();
    if (_rendererReady) {
      _renderer.removeListener(_onVideoSize);
      _renderer.srcObject = null;
      _renderer.dispose();
    }
    super.dispose();
  }

  double get _vw => _size.$1;
  double get _vh => _size.$2;

  /// Crop to fill only when the video's shape is within ~25% of the view's.
  bool _cover(Size view) {
    if (!widget.solo || _vw <= 0 || _vh <= 0 || view.isEmpty) return false;
    final ratio = (_vw / _vh) / (view.width / view.height);
    return ratio > 0.78 && ratio < 1.28;
  }

  void _onTap(TapUpDetails d, Size size) {
    final t = widget.tile;
    if (widget.controller.tapTile(t)) return;
    if (!t.videoOn) return;
    final rect = FrameRect.fit(_vw, _vh, size.width, size.height, cover: _cover(size));
    final pt = rect?.toFrame(d.localPosition.dx, d.localPosition.dy, mirrored: t.mirrored);
    if (pt != null) widget.controller.sendPointer(t.identity, pt.x, pt.y);
  }

  @override
  Widget build(BuildContext context) {
    final t = widget.tile;
    final c = widget.controller;
    final small = t.pip;
    return LayoutBuilder(builder: (context, box) {
      final size = Size(box.maxWidth, box.maxHeight);
      final cover = _cover(size);
      final rect = FrameRect.fit(_vw, _vh, size.width, size.height, cover: cover);
      final radius = widget.solo ? 0.0 : 12.0;
      return GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTapUp: (d) => _onTap(d, size),
        child: AnimatedBuilder(
          animation: _flash,
          builder: (context, child) {
            // Two pulses, like the web's tile-flash animation.
            final v = _flash.isAnimating ? (1 - ((_flash.value * 2) % 1 - 0.5).abs() * 2) : 0.0;
            final border = v > 0
                ? Border.all(color: _flashColor.withValues(alpha: v), width: 4)
                : t.speaking
                    ? Border.all(color: const Color(0xFF8AB4F8), width: 2)
                    : small
                        ? Border.all(color: Colors.white24, width: 1)
                        : null;
            return Container(
              key: Key('tile-${t.identity}'),
              decoration: BoxDecoration(
                color: widget.solo ? Colors.black : const Color(0xFF3C4043),
                borderRadius: BorderRadius.circular(radius),
                border: border,
                boxShadow: small ? const [BoxShadow(color: Colors.black54, blurRadius: 12)] : null,
              ),
              clipBehavior: Clip.antiAlias,
              child: child,
            );
          },
          child: Stack(fit: StackFit.expand, children: [
            if (t.videoOn && t.video != null && _rendererReady)
              VideoTrackRenderer(
                t.video!,
                key: ValueKey(t.video!.sid ?? t.video!.mediaStreamTrack.id),
                fit: cover ? VideoViewFit.cover : VideoViewFit.contain,
                mirrorMode: t.mirrored ? VideoViewMirrorMode.mirror : VideoViewMirrorMode.off,
                renderMode: VideoRenderMode.texture,
                cachedRenderer: _renderer,
                autoDisposeRenderer: false,
              )
            else
              _Avatar(name: t.name, small: small),
            if (rect != null)
              for (final r in _rings) _RingView(key: r.key, ring: r, rect: rect, mirrored: t.mirrored),
            Positioned(left: 6, bottom: 6, child: _NameChip(tile: t, small: small)),
            if (!small && t.isLocal && _cams > 1 && t.videoOn)
              Positioned(
                top: widget.solo ? 48 : 10,
                right: 10,
                child: _RoundButton(
                  key: const Key('camCycleBtn'),
                  icon: Icons.cameraswitch,
                  tooltip: 'Switch camera',
                  onPressed: c.cycleCamera,
                ),
              ),
            if (!small && !t.isLocal) _RemoteCamButtons(tile: t, controller: c, top: widget.solo ? 48 : 10),
          ]),
        ),
      );
    });
  }
}

class _RingView extends StatefulWidget {
  final _Ring ring;
  final FrameRect rect;
  final bool mirrored;
  const _RingView({super.key, required this.ring, required this.rect, required this.mirrored});

  @override
  State<_RingView> createState() => _RingViewState();
}

class _RingViewState extends State<_RingView> with SingleTickerProviderStateMixin {
  late final AnimationController _a =
      AnimationController(vsync: this, duration: const Duration(milliseconds: 500))..repeat(count: 2);

  @override
  void dispose() {
    _a.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final p = widget.rect.toView(widget.ring.x, widget.ring.y, mirrored: widget.mirrored);
    final size = widget.rect.ringSize;
    final color = hexColor(widget.ring.color);
    return Positioned(
      left: p.x - size / 2,
      top: p.y - size / 2,
      width: size,
      height: size,
      child: IgnorePointer(
        child: AnimatedBuilder(
          animation: _a,
          builder: (context, _) {
            final v = _a.value;
            return Opacity(
              opacity: v < 0.4 ? 1 : (1 - (v - 0.4) / 0.6),
              child: Transform.scale(
                scale: v < 0.4 ? 0.6 + v : 1 + (v - 0.4) * 0.4,
                child: Container(
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(color: color, width: (size / 12).clamp(3, 8)),
                    boxShadow: [BoxShadow(color: color, blurRadius: 18), const BoxShadow(color: Colors.black54, spreadRadius: 2)],
                  ),
                ),
              ),
            );
          },
        ),
      ),
    );
  }
}

class _Avatar extends StatelessWidget {
  final String name;
  final bool small;
  const _Avatar({required this.name, required this.small});

  @override
  Widget build(BuildContext context) {
    final initial = name.isNotEmpty ? name.characters.first.toUpperCase() : '?';
    final hue = (name.hashCode % 360).abs().toDouble();
    return Center(
      child: CircleAvatar(
        radius: small ? 20 : 44,
        backgroundColor: HSLColor.fromAHSL(1, hue, 0.45, 0.45).toColor(),
        child: Text(initial, style: TextStyle(fontSize: small ? 18 : 36, color: Colors.white)),
      ),
    );
  }
}

class _NameChip extends StatelessWidget {
  final TileModel tile;
  final bool small;
  const _NameChip({required this.tile, required this.small});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: EdgeInsets.symmetric(horizontal: small ? 6 : 8, vertical: small ? 1 : 3),
      decoration: BoxDecoration(color: Colors.black54, borderRadius: BorderRadius.circular(6)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(tile.micOn ? Icons.mic : Icons.mic_off, size: small ? 12 : 14, color: tile.micOn ? Colors.white : const Color(0xFFF28B82)),
        const SizedBox(width: 4),
        ConstrainedBox(
          constraints: BoxConstraints(maxWidth: small ? 90 : 220),
          child: Text(
            tile.isLocal ? '${tile.name} (You)' : tile.name,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(color: Colors.white, fontSize: small ? 11 : 13),
          ),
        ),
        const SizedBox(width: 5),
        Container(
          width: 9,
          height: 9,
          decoration: BoxDecoration(color: hexColor(tile.color), shape: BoxShape.circle),
        ),
      ]),
    );
  }
}

class _RoundButton extends StatelessWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;
  final bool active;
  const _RoundButton({super.key, required this.icon, required this.tooltip, this.onPressed, this.active = false});

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: Material(
        color: active ? const Color(0xFFFDD663) : const Color(0xBF202124),
        shape: const CircleBorder(),
        child: InkWell(
          customBorder: const CircleBorder(),
          onTap: onPressed,
          child: SizedBox(
            width: 44,
            height: 44,
            child: Icon(icon, color: onPressed == null ? Colors.white38 : (active ? const Color(0xFF202124) : Colors.white)),
          ),
        ),
      ),
    );
  }
}

/// Switch camera / take photo / flashlight on another caller's phone.
class _RemoteCamButtons extends StatelessWidget {
  final TileModel tile;
  final MeetController controller;
  final double top;
  const _RemoteCamButtons({required this.tile, required this.controller, required this.top});

  @override
  Widget build(BuildContext context) {
    final s = controller.remoteCams[tile.identity];
    if (s == null || !s.phone || !s.cam) return const SizedBox.shrink();
    final c = controller;
    final id = tile.identity;
    VoidCallback? act(String op) => c.camBusy(id, op) ? null : () => c.requestCam(id, op);
    return Positioned(
      top: top,
      right: 10,
      child: Column(key: Key('remoteCam-$id'), children: [
        if (s.cams > 1)
          _RoundButton(key: const Key('rcCycle'), icon: Icons.cameraswitch, tooltip: 'Switch their camera', onPressed: act('cycle')),
        const SizedBox(height: 8),
        _RoundButton(key: const Key('rcPhoto'), icon: Icons.photo_camera, tooltip: 'Take a photo with their camera', onPressed: act('photo')),
        if (s.torch) ...[
          const SizedBox(height: 8),
          _RoundButton(
            key: const Key('rcTorch'),
            icon: s.torchOn ? Icons.flashlight_on : Icons.flashlight_off,
            tooltip: s.torchOn ? 'Turn their flashlight off' : 'Turn their flashlight on',
            active: s.torchOn,
            onPressed: act('torch'),
          ),
        ],
      ]),
    );
  }
}
