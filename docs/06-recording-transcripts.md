# 06 — Recording & Transcripts

## Egress plan

Five egresses per session:

| # | Type | Subject | Output | Container |
|---|---|---|---|---|
| 1 | Track Egress | caller camera track | `caller-video-{sid}` | `.mp4` (H.264) |
| 2 | Track Egress | caller mic track | `caller-audio-{sid}` | `.ogg` (Opus) |
| 3 | Track Egress | support camera track | `support-video-{sid}` | `.mp4` (H.264) |
| 4 | Track Egress | support mic track | `support-audio-{sid}` | `.ogg` (Opus) |
| 5 | Room Composite | whole room, grid layout | `composite` | `.mp4` |

1–4 satisfy FR-5.1 exactly: two separate video streams, two separate audio files.
5 exists because four unsynchronized files are miserable to review by hand.

The agent's published audio track is **excluded** from track egress but is
captured in the composite. Rationale: the agent's speech is fully reconstructible
from the transcript, and a fifth media file with a fifth clock to sync is not
worth it. If you later need agent audio as a discrete file, add a sixth track
egress keyed on `identity == "agent"` — the handler already has the branch point.

### ADR: force H.264

Track Egress does **no transcoding** — it muxes the track as received. Container
choice follows codec: VP8 → `.ivf`, H.264 → `.mp4`. `.ivf` is a raw frame
container that most players and post-processing tools won't open.

So the Flutter client publishes with H.264:

```dart
await room.localParticipant!.setCameraEnabled(
  true,
  cameraCaptureOptions: const CameraCaptureOptions(...),
);
// and in RoomOptions:
defaultVideoPublishOptions: const VideoPublishOptions(
  videoCodec: 'h264',
  simulcast: false,
),
```

Trade-off: we give up VP9/AV1 efficiency and simulcast layer flexibility. For a
fixed 1:1 call this costs almost nothing, and H.264 is hardware-encoded on every
iOS and Android device we care about, which is better for battery. Simulcast is
disabled because with exactly one subscriber there is no layer to choose, and
simulcast complicates which layer egress captures.

The webhook handler must still read the actual filename from `EgressInfo.file`
rather than assuming `.mp4` — a client that ignores the codec preference should
produce a wrong extension, not a broken record.

### Why webhook-driven

`TrackEgressRequest` requires `track_id`, and a track SID does not exist until
the track is published. There is no way to pre-arm a per-track egress at room
creation. Auto-egress in `CreateRoom` can record all tracks, but gives you no
control over filenames, no role labelling, and no per-track row in your database.

So: `track_published` webhook → look up the participant's role → start egress →
insert a `recordings` row keyed on the returned `egress_id`.

```python
# backend/app/services/egress.py  (shape)

async def on_track_published(session, participant_identity, track):
    role = role_from_identity(participant_identity)
    if role == "agent" or not session.recording_enabled:
        return
    kind = "track_video" if track.type == TrackType.VIDEO else "track_audio"
    ext  = "mp4" if track.type == TrackType.VIDEO else "ogg"
    filepath = f"sessions/{session.id}/media/{role}-{'video' if ... else 'audio'}-{track.sid}.{ext}"

    info = await lkapi.egress.start_track_egress(
        api.TrackEgressRequest(
            room_name=session.room_name,
            track_id=track.sid,
            file=api.DirectFileOutput(
                filepath=filepath,
                gcp=api.GCPUpload(
                    credentials=settings.gcp_credentials_json,
                    bucket=settings.gcs_bucket,
                ),
            ),
        )
    )
    await repo.insert_recording(session.id, info.egress_id, kind, role, track.sid)
```

Composite egress starts once both humans have published, and uses
`EncodedFileOutput` with `EncodedFileType.MP4` and a grid layout.

```python
await lkapi.egress.start_room_composite_egress(
    api.RoomCompositeEgressRequest(
        room_name=session.room_name,
        layout="grid",
        audio_only=False,
        file_outputs=[api.EncodedFileOutput(
            file_type=api.EncodedFileType.MP4,
            filepath=f"sessions/{session.id}/media/composite.mp4",
            gcp=api.GCPUpload(credentials=..., bucket=...),
        )],
    )
)
```

Verify these exact class and field names against the installed `livekit-api`
version before writing final code — the proto-generated Python names have
shifted between releases.

### Sync and clocks

The four track files have independent start times. Each `recordings` row records
`started_at`, and the composite provides a single-clock reference. For any
downstream alignment work, use `started_at` deltas relative to
`sessions.started_at` — the same zero point the transcript uses. This is why
`sessions.started_at` is written once and never updated.

Be aware that LiveKit Cloud participant/track egress has known A/V drift reports
on long calls. The composite is the mitigation for human review; the separate
tracks are the archival source of truth. If drift becomes a product problem, the
fix is post-processing against transcript timestamps, not a different egress
type.

### Lifecycle and failure

`egress_started` / `egress_updated` / `egress_ended` webhooks mirror state into
`recordings`. On `egress_ended`, read `EgressInfo.file_results[0]` for
`filename`, `size`, `duration`, and store `gs://{bucket}/{filename}`.

A failed egress sets `state='failed'` with the error text and **does not** end the
call (FR-5.6). The session record then shows a partial recording set, which is
strictly better than dropping a support call because a recorder hiccuped.

Egresses stop automatically when the room ends. Explicit `stop_egress` is only
needed for the "stop recording mid-call" feature, which is not in scope.

## Transcript pipeline

```
caller speech ─► AgentSession STT ─► user_input_transcribed (is_final) ─┐
support speech ► SupportTranscriber ─► FINAL_TRANSCRIPT ────────────────┼─► TranscriptSink
agent speech ──► conversation_item_added (role=assistant) ──────────────┘        │
                                                                                 │ batch
                                                                                 ▼
                                                          POST /v1/sessions/{id}/utterances
                                                                                 │
                                                                                 ▼
                                                                   transcript_utterances
                                                                                 │
                                                            room_finished        ▼
                                                            ──────────────► export to GCS
```

### Live captions on the client

The agent publishes transcriptions to the `lk.transcription` text stream topic
when `text_output=True`. Clients register a handler for that topic; each chunk
carries `lk.transcription_final` and `lk.transcribed_track_id` attributes, and
the stream's sender identity is the transcribed participant.

**Known upstream caveat:** there are open issues where transcription text streams
are only generated for one participant, and where the sender identity is reported
as the agent rather than the speaker. Do not build speaker attribution on the
client. The client stream is a *display* convenience only; the durable,
correctly-attributed transcript is the one the agent worker POSTs to the backend,
where attribution is structural. If the client stream misattributes, captions
look slightly wrong for a moment — the stored record stays correct.

The support transcript panel (FR-6.7) renders from the text stream for
low-latency interims, and reconciles against `GET /v1/sessions/{id}/transcript`
every 10 seconds for authoritative finals.

### Export

Triggered by `room_finished`, retryable via `POST .../transcript/export`.

1. Read all `transcript_utterances` for the session ordered by `start_ms`.
2. Write `transcript.jsonl` — one object per line, schema in [03](03-data-model.md).
3. Write `transcript.vtt` — WebVTT cues, speaker prefixed as
   `<v Caller>` / `<v Support>` / `<v Assistant>`, timings from `start_ms`/`end_ms`.
   This drops straight into a player alongside `composite.mp4`.
4. Write `transcript.txt` — `[mm:ss] SPEAKER: text`, one per line.
5. Write `session.json` — the header metadata, mode timeline, AI toggle timeline,
   and media manifest.
6. Set `sessions.transcript_exported_at`.

Export is idempotent: it overwrites the same five object paths. Re-running it
after a late-arriving utterance batch is safe and is the intended recovery path.

### Diarization

There is no diarization model in this system, and that is the design. Each
utterance's speaker is known because it arrived on a specific participant's
track. Acoustic diarization on a mixed stream would be strictly worse: it
introduces speaker-confusion errors, adds latency, and costs money to solve a
problem we do not have.

The one thing structural attribution cannot do is separate two people speaking
into the *same* microphone. If that becomes a requirement (a caller hands the
phone to someone else), it is a new feature — speaker-change detection on a
single track — not a change to this design.
