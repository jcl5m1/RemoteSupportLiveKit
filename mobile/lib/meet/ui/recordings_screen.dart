// Recordings for a call: one video per person per recording, interaction logs,
// and phone photos. Anyone with the call link may view them (product decision).

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../meet_api.dart';
import '../protocol.dart';
import 'show_viewer.dart';

class RecordingsScreen extends StatefulWidget {
  final MeetApi api;
  final String callId;
  const RecordingsScreen({super.key, required this.api, required this.callId});
  @override
  State<RecordingsScreen> createState() => _RecordingsScreenState();
}

class _RecordingsScreenState extends State<RecordingsScreen> {
  late Future<CallRecordings> _load = widget.api.recordings(widget.callId);

  String _when(DateTime t) => t.toLocal().toString().substring(0, 16);

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('Recordings · ${widget.callId}'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: () => setState(() => _load = widget.api.recordings(widget.callId)),
          ),
        ],
      ),
      body: FutureBuilder<CallRecordings>(
        future: _load,
        builder: (context, snap) {
          if (snap.hasError) return Center(child: Text('Could not load recordings: ${snap.error}'));
          final r = snap.data;
          if (r == null) return const Center(child: CircularProgressIndicator());
          if (r.recordings.isEmpty && r.eventLogs.isEmpty && r.photos.isEmpty) {
            return const Center(child: Text('No recordings yet'));
          }
          return ListView(padding: const EdgeInsets.all(12), children: [
            for (final f in r.recordings)
              Card(
                child: ListTile(
                  leading: const Icon(Icons.videocam),
                  title: Text(f.participant),
                  subtitle: Text('${_when(f.startedAt)} · ${fmtBytes(f.sizeBytes)}'),
                  trailing: IconButton(
                    icon: const Icon(Icons.download),
                    onPressed: () => launchUrl(Uri.parse(f.url), mode: LaunchMode.externalApplication),
                  ),
                  onTap: () => Navigator.of(context).push(MaterialPageRoute<void>(
                    builder: (_) => Scaffold(
                      backgroundColor: Colors.black,
                      appBar: AppBar(title: Text(f.participant)),
                      body: VideoCard(url: f.url),
                    ),
                  )),
                ),
              ),
            for (final l in r.eventLogs)
              Card(
                child: ListTile(
                  leading: const Icon(Icons.receipt_long),
                  title: const Text('Interaction log'),
                  subtitle: Text('${_when(l.startedAt)} · chat, pointer, controls · ${fmtBytes(l.sizeBytes)}'),
                  trailing: const Icon(Icons.download),
                  onTap: () => launchUrl(Uri.parse(l.url), mode: LaunchMode.externalApplication),
                ),
              ),
            for (final p in r.photos)
              Card(
                clipBehavior: Clip.antiAlias,
                child: InkWell(
                  onTap: () => Navigator.of(context).push(MaterialPageRoute<void>(
                    builder: (_) => Scaffold(
                      backgroundColor: Colors.black,
                      appBar: AppBar(title: Text('Photo · ${p.participant}')),
                      body: ZoomableImage(url: p.url),
                    ),
                  )),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                    ListTile(
                      leading: const Icon(Icons.photo),
                      title: Text('Photo · ${p.participant}'),
                      subtitle: Text('${_when(p.takenAt)} · ${fmtBytes(p.sizeBytes)}'),
                      trailing: IconButton(
                        icon: const Icon(Icons.download),
                        onPressed: () => launchUrl(Uri.parse(p.url), mode: LaunchMode.externalApplication),
                      ),
                    ),
                    SizedBox(height: 220, child: Image.network(p.url, fit: BoxFit.contain)),
                  ]),
                ),
              ),
          ]);
        },
      ),
    );
  }
}
