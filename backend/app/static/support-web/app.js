/**
 * LiveKit web client for the automated regression harness.
 *
 * Reads query parameters:
 *   ?token=<jwt>           LiveKit room token (required)
 *   ?ws_url=<wss://...>    LiveKit websocket URL (required)
 *   ?identity=<name>       Display label for the synthetic video stream
 *   ?audio_url=<url>       Optional speech audio file to publish instead of a tone
 *   ?audio_tone_hz=<n>     Tone frequency for synthetic audio (default 440)
 *
 * Publishes:
 *   - a synthetic video track: canvas drawing a running clock with milliseconds
 *     and the identity label
 *   - a synthetic audio track: either a fetched speech file played on loop, or
 *     a sine tone whose frequency slowly modulates
 *
 * Exposes state on window.testState and actions on window.testActions for
 * Playwright to drive and inspect.
 */

const { Room, RoomEvent, Track } = window.LivekitClient;

const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const localVideo = document.getElementById('localVideo');
const remoteVideo = document.getElementById('remoteVideo');

function setStatus(text) {
  statusEl.textContent = text;
  console.log('[support-web]', text);
}

function setError(text) {
  errorEl.textContent = text;
  console.error('[support-web]', text);
}

const params = new URLSearchParams(window.location.search);
const token = params.get('token');
const wsUrl = params.get('ws_url');
const identity = params.get('identity') || 'unknown';
const audioUrl = params.get('audio_url');
const audioToneHz = parseInt(params.get('audio_tone_hz') || '440', 10);

const identityEl = document.getElementById('identity');
if (identityEl) {
  identityEl.textContent = `Identity: ${identity}`;
}

window.testState = {
  connected: false,
  connectionState: 'disconnected',
  reconnectCount: 0,
  remoteVideoReady: false,
  remoteAudioReady: false,
  localVideoTrack: null,
  localAudioTrack: null,
  remoteVideoTrack: null,
  remoteAudioTrack: null,
  room: null,
  error: null,
};

if (!token) {
  setError('Missing ?token= query parameter');
  window.testState.error = 'missing_token';
  throw new Error('Missing token');
}

if (!wsUrl) {
  setError('Missing ?ws_url= query parameter');
  window.testState.error = 'missing_ws_url';
  throw new Error('Missing ws_url');
}

const room = new Room({
  adaptiveStream: true,
  dynacast: true,
  reconnectPolicy: {
    maxRetries: 10,
    retryDelay: 1000,
  },
});

window.testState.room = room;

room.on(RoomEvent.Connected, () => {
  setStatus('Connected');
  window.testState.connected = true;
  window.testState.connectionState = 'connected';
});

room.on(RoomEvent.Disconnected, (reason) => {
  setStatus(`Disconnected: ${reason || 'unknown'}`);
  window.testState.connected = false;
  window.testState.connectionState = 'disconnected';
});

room.on(RoomEvent.ConnectionStateChanged, (state) => {
  setStatus(`Connection state: ${state}`);
  window.testState.connectionState = state;
  if (state === 'connected') {
    window.testState.reconnectCount += 1;
  }
});

room.on(RoomEvent.Reconnecting, () => {
  setStatus('Reconnecting…');
  window.testState.connectionState = 'reconnecting';
});

room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
  const kind = track.kind;
  setStatus(`Subscribed ${kind} from ${participant.identity}`);
  if (kind === Track.Kind.Video) {
    track.attach(remoteVideo);
    window.testState.remoteVideoTrack = track;
    window.testState.remoteVideoReady = true;
  } else if (kind === Track.Kind.Audio) {
    track.attach();
    window.testState.remoteAudioTrack = track;
    window.testState.remoteAudioReady = true;
  }
});

room.on(RoomEvent.TrackUnsubscribed, (track, publication, participant) => {
  const kind = track.kind;
  setStatus(`Unsubscribed ${kind} from ${participant.identity}`);
  track.detach();
  if (kind === Track.Kind.Video) {
    window.testState.remoteVideoReady = false;
    window.testState.remoteVideoTrack = null;
  } else if (kind === Track.Kind.Audio) {
    window.testState.remoteAudioReady = false;
    window.testState.remoteAudioTrack = null;
  }
});

function createClockVideoTrack(label) {
  const canvas = document.createElement('canvas');
  canvas.width = 640;
  canvas.height = 480;
  const ctx = canvas.getContext('2d');

  function draw() {
    const now = performance.now();
    const date = new Date();
    const iso = date.toISOString();
    const ms = Math.floor(now % 1000).toString().padStart(3, '0');

    ctx.fillStyle = '#0a0a0a';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.strokeStyle = '#00ff00';
    ctx.lineWidth = 6;
    ctx.strokeRect(20, 20, canvas.width - 40, canvas.height - 40);

    ctx.fillStyle = '#00ff00';
    ctx.textAlign = 'center';
    ctx.font = 'bold 40px monospace';
    ctx.fillText(label, canvas.width / 2, 90);

    ctx.font = 'bold 64px monospace';
    ctx.fillText(`${iso}.${ms}`, canvas.width / 2, canvas.height / 2 + 10);

    ctx.font = '24px monospace';
    ctx.fillText('Synthetic regression stream', canvas.width / 2, canvas.height - 60);
  }

  draw();
  const interval = setInterval(draw, 50);
  const stream = canvas.captureStream(20);
  const track = stream.getVideoTracks()[0];
  return { track, stop: () => clearInterval(interval) };
}

async function ensureAudioContextRunning(audioCtx) {
  // Browsers suspend AudioContexts until a user gesture. In headless automation
  // we resume explicitly; if that fails the harness can call resumeAudio().
  if (audioCtx.state === 'suspended') {
    try {
      await audioCtx.resume();
    } catch (err) {
      console.warn('[support-web] audioCtx.resume() failed:', err);
    }
  }
}

function createToneAudioTrack(baseHz) {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  const audioCtx = new AudioContext();

  const oscillator = audioCtx.createOscillator();
  oscillator.type = 'sine';
  oscillator.frequency.setValueAtTime(baseHz, audioCtx.currentTime);

  // Slowly modulate frequency so the byte stream is not static.
  const lfo = audioCtx.createOscillator();
  lfo.type = 'sine';
  lfo.frequency.setValueAtTime(2, audioCtx.currentTime);
  const lfoGain = audioCtx.createGain();
  lfoGain.gain.setValueAtTime(30, audioCtx.currentTime);
  lfo.connect(lfoGain);
  lfoGain.connect(oscillator.frequency);
  lfo.start();

  const gain = audioCtx.createGain();
  gain.gain.setValueAtTime(0.05, audioCtx.currentTime);
  oscillator.connect(gain);

  const dest = audioCtx.createMediaStreamDestination();
  gain.connect(dest);
  oscillator.start();

  const track = dest.stream.getAudioTracks()[0];
  return {
    track,
    audioCtx,
    stop: () => { oscillator.stop(); lfo.stop(); audioCtx.close(); },
  };
}

async function createSpeechAudioTrack(url) {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  const audioCtx = new AudioContext();

  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`Failed to fetch audio: ${resp.status} ${resp.statusText}`);
  }
  const arrayBuffer = await resp.arrayBuffer();
  const speechBuffer = await audioCtx.decodeAudioData(arrayBuffer);

  // Repeat the speech clip twice with an 8-second silence gap in between.
  // The agent may join after the first utterance has already started, so the
  // second utterance gives the STT pipeline a fresh chance to transcribe.
  const sampleRate = speechBuffer.sampleRate;
  const silenceSeconds = 8;
  const channelCount = speechBuffer.numberOfChannels;
  const speechFrames = speechBuffer.length;
  const silenceFrames = Math.floor(sampleRate * silenceSeconds);
  const totalFrames = speechFrames + silenceFrames + speechFrames;
  const loopBuffer = audioCtx.createBuffer(channelCount, totalFrames, sampleRate);
  for (let ch = 0; ch < channelCount; ch++) {
    const src = speechBuffer.getChannelData(ch);
    const dst = loopBuffer.getChannelData(ch);
    dst.set(src, 0);
    dst.set(src, speechFrames + silenceFrames);
  }

  const source = audioCtx.createBufferSource();
  source.buffer = loopBuffer;
  source.loop = false;

  const gain = audioCtx.createGain();
  gain.gain.setValueAtTime(0.6, audioCtx.currentTime);
  source.connect(gain);

  const dest = audioCtx.createMediaStreamDestination();
  gain.connect(dest);
  source.start();

  const track = dest.stream.getAudioTracks()[0];
  return {
    track,
    audioCtx,
    stop: () => { source.stop(); audioCtx.close(); },
  };
}

async function main() {
  try {
    setStatus('Connecting…');
    await room.connect(wsUrl, token);

    setStatus('Publishing synthetic tracks…');
    const video = createClockVideoTrack(identity);
    const audio = audioUrl
      ? await createSpeechAudioTrack(audioUrl)
      : createToneAudioTrack(audioToneHz);

    await ensureAudioContextRunning(audio.audioCtx);

    window.testState.localVideoTrack = video;
    window.testState.localAudioTrack = audio;

    // Attach local preview.
    if (video.track) {
      const previewStream = new MediaStream([video.track.clone()]);
      localVideo.srcObject = previewStream;
    }

    await room.localParticipant.publishTrack(video.track, {
      name: 'camera',
      source: Track.Source.Camera,
      simulcast: false,
    });
    await room.localParticipant.publishTrack(audio.track, {
      name: 'microphone',
      source: Track.Source.Microphone,
    });

    setStatus('Waiting for remote tracks…');
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    setError(msg);
    window.testState.error = msg;
    setStatus('Failed');
  }
}

window.testActions = {
  disconnect: async () => {
    setStatus('Action: disconnect');
    await room.disconnect();
  },
  reconnect: async () => {
    setStatus('Action: reconnect');
    await room.connect(wsUrl, token);
  },
  getState: () => ({ ...window.testState }),
  getConnectionState: () => window.testState.connectionState,
  getReconnectCount: () => window.testState.reconnectCount,
  resumeAudio: async () => {
    const audio = window.testState.localAudioTrack;
    if (audio && audio.audioCtx && audio.audioCtx.state === 'suspended') {
      await audio.audioCtx.resume();
    }
  },
};

main();
