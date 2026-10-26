/**
 * LiveKit web client — Meet-style call UI, dual mode.
 *
 * Test-harness mode (synthetic media, no auth):
 *   ?token=<jwt>&ws_url=<wss://...>  join directly, publish synthetic tracks
 *   ?identity=<name>                  Display label for the synthetic video
 *   ?audio_url=<url>                  Speech audio file to publish instead of tone
 *   ?audio_tone_hz=<n>                Tone frequency for synthetic audio
 *
 * Authenticated support mode (real camera/mic, Google sign-in):
 *   ?session=<session UUID>           show "Sign in with Google"; after sign-in
 *                                     the page calls POST /v1/sessions/join with
 *                                     the Firebase ID token and connects with
 *                                     the returned LiveKit credentials.
 *
 * Exposes state on window.testState and actions on window.testActions for
 * Playwright to drive and inspect.
 */

const { Room, RoomEvent, Track, DisconnectReason } = window.LivekitClient;

const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const identityEl = document.getElementById('identity');
const callUI = document.getElementById('callUI');
const tileGrid = document.getElementById('tileGrid');
const peoplePanel = document.getElementById('peoplePanel');
const peopleList = document.getElementById('peopleList');
const micBtn = document.getElementById('micBtn');
const camBtn = document.getElementById('camBtn');
const hangupBtn = document.getElementById('hangupBtn');
const peopleBtn = document.getElementById('peopleBtn');
const chatBtn = document.getElementById('chatBtn');
const tabPeople = document.getElementById('tabPeople');
const tabChatBtn = document.getElementById('tabChatBtn');
const closePanelBtn = document.getElementById('closePanelBtn');
const chatTab = document.getElementById('chatTab');
const chatMessages = document.getElementById('chatMessages');
const chatForm = document.getElementById('chatForm');
const chatInput = document.getElementById('chatInput');
const authEl = document.getElementById('auth');
const signInButton = document.getElementById('googleSignIn');
const userInfoEl = document.getElementById('userInfo');
const endCard = document.getElementById('endCard');
const endTitle = document.getElementById('endTitle');
const endDetail = document.getElementById('endDetail');

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
const wsUrlParam = params.get('ws_url');
const sessionId = params.get('session');
const callId = params.get('call');
// Path prefix the app is served under ('' or e.g. '/livekit'), derived from
// this page's own URL so API calls work behind a shared hostname.
const BASE = window.location.pathname.replace(/\/support-web(\/.*)?$/, '');
const recBtn = document.getElementById('recBtn');
const muteAllBtn = document.getElementById('muteAllBtn');
const settingsBtn = document.getElementById('settingsBtn');
const recBadge = document.getElementById('recBadge');
// Open-call recording: the join token doubles as auth for /recording/*.
let callToken = null;
let recordingOn = false;
const identity = params.get('identity') || 'unknown';
const audioUrl = params.get('audio_url');
const audioToneHz = parseInt(params.get('audio_tone_hz') || '440', 10);

if (identityEl && identity !== 'unknown') {
  identityEl.textContent = identity;
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
  signedInEmail: null,
  participants: [],
  chatMessages: [],
};

const room = new Room({
  adaptiveStream: true,
  dynacast: true,
  reconnectPolicy: {
    // Exponential backoff consumes attempts during an outage; keep a generous
    // budget so reconnection still has retries left afterwards (FR: a WiFi
    // drop must recover). ReconnectContext carries retryCount/elapsedMs;
    // returning null stops reconnecting.
    nextRetryDelayInMs: (context) =>
      context.retryCount >= 25
        ? null
        : Math.min(1000 * 2 ** context.retryCount, 10000),
  },
});

window.testState.room = room;

/* -------------------------------------------------------------------------
 * Tiles + people panel
 * ----------------------------------------------------------------------- */

// participant key -> {root, videoEl, nameEl, circleEl, hasVideoTrack}
const tiles = new Map();
let localTileKey = null;
const speakingKeys = new Set();
let userLeft = false;

function participantKey(p) {
  return p.sid || p.identity;
}

function displayName(p) {
  return (p && p.name) || (p && p.identity) || 'unknown';
}

function roleOf(p, isLocal) {
  const id = ((p && p.identity) || identity).toLowerCase();
  if (id.startsWith('agent')) return 'Agent';
  if (id.includes('support')) return 'Support';
  if (id.startsWith('guest')) return isLocal ? 'You' : 'Guest';
  return isLocal ? 'You' : 'Caller';
}

function avatarColor(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) | 0;
  }
  return `hsl(${Math.abs(hash) % 360}, 55%, 45%)`;
}

function initialOf(name) {
  return (name.trim()[0] || '?').toUpperCase();
}

function setTileVideoVisible(key, visible) {
  const t = tiles.get(key);
  if (!t) return;
  t.root.classList.toggle('cam-off', !visible);
  t.root.classList.toggle('no-video', !visible);
}

function setTileMic(key, enabled) {
  const t = tiles.get(key);
  if (t) t.root.classList.toggle('mic-off', !enabled);
}

/** The e2e harness inspects a stable #remoteVideo element; keep the id on
 *  whichever remote tile currently has a live video track. */
function ensureRemoteVideoId() {
  if (document.getElementById('remoteVideo')) return;
  for (const [key, t] of tiles) {
    if (key !== localTileKey && t.hasVideoTrack && t.videoEl) {
      t.videoEl.id = 'remoteVideo';
      return;
    }
  }
}

function layoutGrid() {
  const n = Math.max(tileGrid.children.length, 1);
  // Portrait phones stack tiles; landscape/desktop fills a grid.
  const portrait = window.innerHeight > window.innerWidth;
  const cols = portrait ? (n <= 2 ? 1 : 2) : n <= 1 ? 1 : n <= 4 ? 2 : 3;
  tileGrid.style.setProperty('--cols', String(cols));
  tileGrid.style.setProperty('--rows', String(Math.ceil(n / cols)));
}

window.addEventListener('resize', () => layoutGrid());

function addTile(p, { isLocal }) {
  const key = participantKey(p);
  if (tiles.has(key)) return tiles.get(key);

  const name = displayName(p);
  const root = document.createElement('div');
  root.className = 'tile no-video';
  root.dataset.participant = p.identity || '';

  const videoEl = document.createElement('video');
  videoEl.autoplay = true;
  videoEl.playsInline = true;
  if (isLocal) {
    videoEl.muted = true;
    videoEl.id = 'localVideo';
  }
  root.appendChild(videoEl);

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  const circle = document.createElement('div');
  circle.className = 'circle';
  circle.style.background = avatarColor(p.identity || name);
  circle.textContent = initialOf(name);
  avatar.appendChild(circle);
  root.appendChild(avatar);

  const chip = document.createElement('div');
  chip.className = 'namechip';
  chip.innerHTML =
    '<svg class="mic-on-ic"><use href="#i-mic"/></svg>' +
    '<svg class="mic-off-ic"><use href="#i-mic-off"/></svg>' +
    '<span class="bars"><i></i><i></i><i></i></span>' +
    '<span class="nm"></span>';
  chip.querySelector('.nm').textContent = isLocal ? `${name} (You)` : name;
  root.appendChild(chip);

  tileGrid.appendChild(root);
  const entry = { root, videoEl, nameEl: chip.querySelector('.nm'), circleEl: circle, hasVideoTrack: false };
  tiles.set(key, entry);
  if (isLocal) localTileKey = key;
  layoutGrid();
  renderPeople();
  return entry;
}

function removeTile(key) {
  const t = tiles.get(key);
  if (!t) return;
  const hadRemoteId = t.videoEl && t.videoEl.id === 'remoteVideo';
  if (hadRemoteId) t.videoEl.id = '';
  t.root.remove();
  tiles.delete(key);
  layoutGrid();
  if (hadRemoteId) ensureRemoteVideoId();
  renderPeople();
}

function applySpeaking() {
  for (const [key, t] of tiles) {
    t.root.classList.toggle('speaking', speakingKeys.has(key));
  }
  for (const li of peopleList.children) {
    li.classList.toggle('speaking', speakingKeys.has(li.dataset.key));
  }
}

function participantSnapshot() {
  const out = [];
  const lp = room.localParticipant;
  if (lp && lp.identity) {
    out.push({
      identity: lp.identity,
      name: displayName(lp),
      role: roleOf(lp, true),
      isLocal: true,
      mic: lp.isMicrophoneEnabled,
      cam: lp.isCameraEnabled,
      speaking: speakingKeys.has(participantKey(lp)),
    });
  }
  room.remoteParticipants.forEach((p) => {
    out.push({
      identity: p.identity,
      name: displayName(p),
      role: roleOf(p, false),
      isLocal: false,
      mic: p.isMicrophoneEnabled,
      cam: p.isCameraEnabled,
      speaking: speakingKeys.has(participantKey(p)),
    });
  });
  return out;
}

function renderPeople() {
  const people = participantSnapshot();
  window.testState.participants = people;

  peopleList.innerHTML = '';
  for (const person of people) {
    const li = document.createElement('li');
    li.dataset.key = person.identity;
    li.classList.toggle('mic-off', !person.mic);
    li.classList.toggle('speaking', person.speaking);

    const circle = document.createElement('div');
    circle.className = 'pcircle';
    circle.style.background = avatarColor(person.identity || person.name);
    circle.textContent = initialOf(person.name);
    li.appendChild(circle);

    const nameWrap = document.createElement('div');
    nameWrap.className = 'pname';
    nameWrap.textContent = person.isLocal ? `${person.name} (You)` : person.name;
    li.appendChild(nameWrap);

    const role = document.createElement('div');
    role.className = 'prole';
    role.textContent = person.role;
    li.appendChild(role);

    const mic = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    mic.classList.add('pmic');
    mic.innerHTML = `<use href="#${person.mic ? 'i-mic' : 'i-mic-off'}"/>`;
    li.appendChild(mic);

    peopleList.appendChild(li);
  }

  const badge = peopleBtn.querySelector('.badge');
  if (badge) badge.textContent = String(people.length);
}

/* -------------------------------------------------------------------------
 * Room events
 * ----------------------------------------------------------------------- */

room.on(RoomEvent.Connected, () => {
  setStatus('Connected');
  window.testState.connected = true;
  window.testState.connectionState = 'connected';
  addTile(room.localParticipant, { isLocal: true });
  room.remoteParticipants.forEach((p) => addTile(p, { isLocal: false }));
});

room.on(RoomEvent.Disconnected, (reason) => {
  setStatus(`Disconnected: ${reason || 'unknown'}`);
  window.testState.connected = false;
  window.testState.connectionState = 'disconnected';
  let title = userLeft ? 'You left the call' : 'Call ended';
  let detail = userLeft ? '' : 'The session has ended.';
  if (callId && reason === DisconnectReason.ROOM_DELETED) {
    // Someone used "End call for everyone"; the server named them in metadata.
    const by = roomRecordingMeta().ended_by;
    title = 'Call ended';
    detail = endedByMe ? 'You ended the call for everyone.' : by ? `${by} ended the call.` : '';
  }
  window.testState.endTitle = title;
  window.testState.endDetail = detail;
  if (endTitle) endTitle.textContent = title;
  if (endDetail) endDetail.textContent = detail;
  if (endCard) endCard.hidden = false;
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

room.on(RoomEvent.ParticipantConnected, (participant) => {
  setStatus(`Participant joined: ${participant.identity}`);
  addTile(participant, { isLocal: false });
});

room.on(RoomEvent.ParticipantDisconnected, (participant) => {
  setStatus(`Participant left: ${participant.identity}`);
  removeTile(participantKey(participant));
});

room.on(RoomEvent.ParticipantNameChanged, (name, participant) => {
  const t = tiles.get(participantKey(participant));
  if (t) t.nameEl.textContent = name || participant.identity;
  renderPeople();
});

room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
  const kind = track.kind;
  setStatus(`Subscribed ${kind} from ${participant.identity}`);
  const key = participantKey(participant);
  const t = tiles.get(key) || addTile(participant, { isLocal: false });
  if (kind === Track.Kind.Video) {
    t.hasVideoTrack = true;
    track.attach(t.videoEl);
    if (!document.getElementById('remoteVideo')) t.videoEl.id = 'remoteVideo';
    setTileVideoVisible(key, !publication.isMuted);
    window.testState.remoteVideoTrack = track;
    window.testState.remoteVideoReady = true;
  } else if (kind === Track.Kind.Audio) {
    track.attach();
    setTileMic(key, !publication.isMuted);
    window.testState.remoteAudioTrack = track;
    window.testState.remoteAudioReady = true;
  }
  renderPeople();
});

room.on(RoomEvent.TrackUnsubscribed, (track, publication, participant) => {
  const kind = track.kind;
  setStatus(`Unsubscribed ${kind} from ${participant.identity}`);
  track.detach();
  const key = participantKey(participant);
  const t = tiles.get(key);
  if (kind === Track.Kind.Video) {
    if (t) {
      t.hasVideoTrack = false;
      if (t.videoEl.id === 'remoteVideo') t.videoEl.id = '';
      setTileVideoVisible(key, false);
    }
    ensureRemoteVideoId();
    window.testState.remoteVideoReady = false;
    window.testState.remoteVideoTrack = null;
  } else if (kind === Track.Kind.Audio) {
    window.testState.remoteAudioReady = false;
    window.testState.remoteAudioTrack = null;
  }
  renderPeople();
});

room.on(RoomEvent.TrackMuted, (publication, participant) => {
  const key = participantKey(participant);
  if (publication.kind === Track.Kind.Video) {
    setTileVideoVisible(key, false);
  } else if (publication.kind === Track.Kind.Audio) {
    setTileMic(key, false);
  }
  syncControls();
  renderPeople();
});

room.on(RoomEvent.TrackUnmuted, (publication, participant) => {
  const key = participantKey(participant);
  const t = tiles.get(key);
  if (publication.kind === Track.Kind.Video) {
    setTileVideoVisible(key, Boolean(t && t.hasVideoTrack));
  } else if (publication.kind === Track.Kind.Audio) {
    setTileMic(key, true);
  }
  syncControls();
  renderPeople();
});

room.on(RoomEvent.LocalTrackPublished, () => {
  refreshLocalPreview();
  syncControls();
  renderPeople();
  // Get a newly published track recorded now rather than on the next
  // server reconcile tick.
  if (recordingOn) callRecordingRequest('sync').catch(() => {});
});

room.on(RoomEvent.RoomMetadataChanged, () => applyRecordingState({ announce: true }));

room.on(RoomEvent.LocalTrackUnpublished, () => {
  syncControls();
  renderPeople();
});

room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
  speakingKeys.clear();
  for (const p of speakers) speakingKeys.add(participantKey(p));
  applySpeaking();
  renderPeople();
});

room.on(RoomEvent.DataReceived, (payload, participant, kind, topic) => {
  if (topic !== CHAT_TOPIC) return;
  let msg;
  try {
    msg = JSON.parse(chatDecoder.decode(payload));
  } catch {
    return;
  }
  if (!msg || typeof msg.text !== 'string') return;
  addChatMessage({
    identity: participant ? participant.identity : 'unknown',
    name: (participant && displayName(participant)) || 'unknown',
    text: msg.text,
    ts: msg.ts || Date.now(),
    mine: false,
  });
});

/* -------------------------------------------------------------------------
 * Control bar
 * ----------------------------------------------------------------------- */

function setButtonIcons(btn, onSymbol, offSymbol) {
  btn.innerHTML =
    `<svg class="ic-on"><use href="#${onSymbol}"/></svg>` +
    `<svg class="ic-off"><use href="#${offSymbol}"/></svg>`;
}

function syncControls() {
  const lp = room.localParticipant;
  if (!lp || !lp.identity) return;
  const anyOn = lp.isMicrophoneEnabled || lp.isCameraEnabled;
  muteAllBtn.classList.toggle('off', !anyOn);
  muteAllBtn.title = anyOn ? 'Mute microphone and camera' : 'Unmute microphone and camera';
  micBtn.classList.toggle('off', !lp.isMicrophoneEnabled);
  micBtn.title = lp.isMicrophoneEnabled ? 'Turn off microphone' : 'Turn on microphone';
  camBtn.classList.toggle('off', !lp.isCameraEnabled);
  camBtn.title = lp.isCameraEnabled ? 'Turn off camera' : 'Turn on camera';
  setTileMic(localTileKey, lp.isMicrophoneEnabled);
  if (localTileKey) setTileVideoVisible(localTileKey, lp.isCameraEnabled);
}

async function toggleMic() {
  const lp = room.localParticipant;
  if (!lp || !lp.identity) return;
  const enable = !lp.isMicrophoneEnabled;
  setStatus(enable ? 'Microphone on' : 'Microphone off');
  micBtn.disabled = true;
  try {
    await lp.setMicrophoneEnabled(enable, enable ? deviceOpts('audioinput') : undefined);
  } finally {
    micBtn.disabled = false;
  }
  syncControls();
  renderPeople();
}

/** One button that mutes (or restores) both the microphone and the camera. */
async function toggleMuteAll() {
  const lp = room.localParticipant;
  if (!lp || !lp.identity) return;
  const enable = !(lp.isMicrophoneEnabled || lp.isCameraEnabled);
  setStatus(enable ? 'Microphone and camera on' : 'Microphone and camera muted');
  muteAllBtn.disabled = true;
  try {
    const results = await Promise.allSettled([
      lp.setMicrophoneEnabled(enable, enable ? deviceOpts('audioinput') : undefined),
      lp.setCameraEnabled(enable, enable ? deviceOpts('videoinput') : undefined),
    ]);
    const failed = results.find((r) => r.status === 'rejected');
    if (failed) setError(`Could not ${enable ? 'unmute' : 'mute'}: ${failed.reason.message}`);
  } finally {
    muteAllBtn.disabled = false;
  }
  refreshLocalPreview();
  syncControls();
  renderPeople();
}

async function toggleCamera() {
  const lp = room.localParticipant;
  if (!lp || !lp.identity) return;
  const enable = !lp.isCameraEnabled;
  setStatus(enable ? 'Camera on' : 'Camera off');
  camBtn.disabled = true;
  try {
    await lp.setCameraEnabled(enable, enable ? deviceOpts('videoinput') : undefined);
  } finally {
    camBtn.disabled = false;
  }
  refreshLocalPreview();
  syncControls();
  renderPeople();
}

async function hangUp() {
  userLeft = true;
  setStatus('Leaving…');
  await room.disconnect();
}

let endedByMe = false;

/** Hang up for everyone: the server stops recording and deletes the room. */
async function endCallForEveryone() {
  endedByMe = true;
  setStatus('Ending call for everyone…');
  try {
    const resp = await fetch(`${BASE}/v1/calls/${encodeURIComponent(callId)}/end`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${callToken}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  } catch (err) {
    endedByMe = false;
    setError(`Could not end the call: ${err.message}`);
  }
}

/** Red button: support sessions just leave; open calls offer leave vs end-for-all. */
function onHangupClick(ev) {
  if (!callId) {
    hangUp();
    return;
  }
  ev.stopPropagation();
  showPopupMenu(hangupBtn, [
    { label: 'Leave call', id: 'leaveCallItem', onClick: hangUp },
    { label: 'End call for everyone', id: 'endCallItem', danger: true, onClick: endCallForEveryone },
  ]);
}

let activeTab = 'people';

function togglePanel(tab) {
  if (!peoplePanel.hidden && activeTab === tab) {
    closePanel();
  } else {
    openPanel(tab);
  }
}

function openPanel(tab) {
  activeTab = tab;
  peoplePanel.hidden = false;
  peopleBtn.classList.toggle('toggled', tab === 'people');
  chatBtn.classList.toggle('toggled', tab === 'chat');
  tabPeople.classList.toggle('active', tab === 'people');
  tabChatBtn.classList.toggle('active', tab === 'chat');
  peopleList.hidden = tab !== 'people';
  chatTab.hidden = tab !== 'chat';
  if (tab === 'chat') {
    clearChatUnread();
    chatInput.focus();
  }
}

function closePanel() {
  peoplePanel.hidden = true;
  peopleBtn.classList.remove('toggled');
  chatBtn.classList.remove('toggled');
}

/* -------------------------------------------------------------------------
 * Chat (LiveKit data channel, topic 'chat')
 * ----------------------------------------------------------------------- */

const CHAT_TOPIC = 'chat';
const chatEncoder = new TextEncoder();
const chatDecoder = new TextDecoder();
let chatUnread = 0;

function fmtChatTime(ts) {
  return new Date(ts).toTimeString().slice(0, 5);
}

function updateChatBadge() {
  const badge = chatBtn.querySelector('.badge');
  if (!badge) return;
  badge.hidden = chatUnread === 0;
  badge.textContent = String(chatUnread);
}

function clearChatUnread() {
  chatUnread = 0;
  updateChatBadge();
}

function addChatMessage(msg) {
  window.testState.chatMessages.push(msg);
  const wrap = document.createElement('div');
  wrap.className = `chat-msg${msg.mine ? ' mine' : ''}`;
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = `${msg.mine ? 'You' : msg.name} · ${fmtChatTime(msg.ts)}`;
  const body = document.createElement('div');
  body.className = 'body';
  body.textContent = msg.text;
  wrap.appendChild(meta);
  wrap.appendChild(body);
  chatMessages.appendChild(wrap);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  if (!msg.mine && (peoplePanel.hidden || activeTab !== 'chat')) {
    chatUnread += 1;
    updateChatBadge();
  }
}

async function sendChat(text) {
  const trimmed = (text || '').trim();
  if (!trimmed || !room.localParticipant.identity) return;
  const payload = chatEncoder.encode(
    JSON.stringify({ text: trimmed, name: displayName(room.localParticipant), ts: Date.now() }),
  );
  await room.localParticipant.publishData(payload, { reliable: true, topic: CHAT_TOPIC });
  addChatMessage({
    identity: room.localParticipant.identity,
    name: displayName(room.localParticipant),
    text: trimmed,
    ts: Date.now(),
    mine: true,
  });
}


/* -------------------------------------------------------------------------
 * Open-call recording
 * ----------------------------------------------------------------------- */

function roomRecordingMeta() {
  try {
    return JSON.parse(room.metadata || '{}') || {};
  } catch {
    return {};
  }
}

/** Sync button/badge with room metadata, which every participant receives. */
function applyRecordingState({ announce }) {
  if (!callId) return;
  const meta = roomRecordingMeta();
  const on = !!meta.recording;
  const changed = on !== recordingOn;
  recordingOn = on;
  window.testState.recording = on;
  recBadge.hidden = !on;
  recBtn.classList.toggle('recording', on);
  recBtn.title = on ? 'Stop recording' : 'Start recording';
  if (announce && changed) {
    const who = on ? meta.started_by : meta.stopped_by;
    setStatus(`Recording ${on ? 'started' : 'stopped'}${who ? ` by ${who}` : ''}`);
  }
  // The server retries skipped tracks, so this clears once slots free up.
  const skipped = on ? meta.skipped || 0 : 0;
  window.testState.recordingSkipped = skipped;
  if (skipped) {
    setError(
      `Recording limit reached: ${skipped} track${skipped > 1 ? 's are' : ' is'} ` +
        'not being recorded (LiveKit concurrent recording cap).',
    );
  } else if (errorEl.textContent.startsWith('Recording limit')) {
    errorEl.textContent = '';
  }
}

async function callRecordingRequest(action) {
  const resp = await fetch(
    `${BASE}/v1/calls/${encodeURIComponent(callId)}/recording/${action}`,
    { method: 'POST', headers: { Authorization: `Bearer ${callToken}` } },
  );
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    throw new Error((body && body.error && body.error.message) || `HTTP ${resp.status}`);
  }
  return body;
}

function fmtBytes(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`;
}

/** Recordings viewer: anyone with the call link can list and play them. */
async function showRecordings() {
  const overlay = document.getElementById('recordings');
  const list = document.getElementById('recList');
  const empty = document.getElementById('recEmpty');
  overlay.hidden = false;
  list.innerHTML = '<li>Loading…</li>';
  empty.hidden = true;
  let body;
  try {
    const resp = await fetch(`${BASE}/v1/calls/${encodeURIComponent(callId)}/recordings`);
    body = await resp.json();
    if (!resp.ok) throw new Error((body && body.error && body.error.message) || `HTTP ${resp.status}`);
  } catch (err) {
    list.innerHTML = '';
    empty.hidden = false;
    empty.textContent = `Could not load recordings: ${err.message}`;
    return;
  }
  window.testState.recordings = body.recordings;
  list.innerHTML = '';
  empty.textContent = 'No recordings for this call yet.';
  empty.hidden = body.recordings.length > 0;
  for (const rec of body.recordings) {
    const li = document.createElement('li');
    const head = document.createElement('div');
    head.className = 'rec-head';
    const who = document.createElement('span');
    who.className = 'rec-who';
    who.textContent = rec.participant;
    const meta = document.createElement('span');
    meta.className = 'rec-meta';
    const kind = rec.content_type.startsWith('audio') ? 'audio' : rec.source === 'camera+microphone' ? 'video + audio' : rec.source;
    meta.textContent = `${new Date(rec.started_at).toLocaleString()} · ${kind} · ${fmtBytes(rec.size_bytes)}`;
    const dl = document.createElement('a');
    dl.href = rec.url;
    dl.textContent = 'Download';
    dl.target = '_blank';
    dl.rel = 'noopener';
    head.append(who, meta, dl);
    const player = document.createElement(rec.content_type.startsWith('audio') ? 'audio' : 'video');
    player.controls = true;
    player.preload = 'metadata';
    player.src = rec.url;
    li.append(head, player);
    list.appendChild(li);
  }
}

function closeRecordings() {
  const overlay = document.getElementById('recordings');
  overlay.querySelectorAll('video, audio').forEach((el) => el.pause());
  overlay.hidden = true;
}

async function toggleRecording() {
  recBtn.disabled = true;
  try {
    await callRecordingRequest(recordingOn ? 'stop' : 'start');
  } catch (err) {
    setError(`Recording: ${err.message}`);
  } finally {
    recBtn.disabled = false;
  }
}

/* -------------------------------------------------------------------------
 * Device selection (mic, camera, speaker) — desktop and mobile browsers
 * ----------------------------------------------------------------------- */

const DEVICE_LABELS = { audioinput: 'Microphone', videoinput: 'Camera', audiooutput: 'Speaker' };
const devicePrefs = {
  audioinput: localStorage.getItem('device.audioinput') || '',
  videoinput: localStorage.getItem('device.videoinput') || '',
  audiooutput: localStorage.getItem('device.audiooutput') || '',
};
let previewStream = null;

function supportsOutputSelection() {
  // setSinkId: Chrome/Edge/Firefox desktop; not iOS Safari or most mobile browsers.
  return 'setSinkId' in HTMLMediaElement.prototype;
}

function deviceOpts(kind) {
  return devicePrefs[kind] ? { deviceId: devicePrefs[kind] } : undefined;
}

function isRearCamera(mediaTrack) {
  const settings = (mediaTrack && mediaTrack.getSettings && mediaTrack.getSettings()) || {};
  return settings.facingMode === 'environment';
}

/** Show the current camera in the local tile; front cameras are mirrored. */
function refreshLocalPreview() {
  const t = tiles.get(localTileKey);
  const pub = room.localParticipant.getTrackPublication(Track.Source.Camera);
  if (!t || !pub || !pub.track) return;
  const mediaTrack = pub.track.mediaStreamTrack;
  if (!t.videoEl.srcObject || t.videoEl.srcObject.getVideoTracks()[0] !== mediaTrack) {
    t.videoEl.srcObject = new MediaStream([mediaTrack]);
  }
  t.root.classList.toggle('mirror', !isRearCamera(mediaTrack));
  t.hasVideoTrack = true;
  setTileVideoVisible(localTileKey, !pub.isMuted);
  window.testState.localVideoTrack = mediaTrack;
}

/** Render Microphone/Camera(/Speaker) selects into ``container``. */
async function renderDeviceSelects(container, onChange) {
  let devices = [];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch {
    // No media API (insecure context, old browser): nothing to pick from.
  }
  const kinds = ['audioinput', 'videoinput'];
  if (supportsOutputSelection()) kinds.push('audiooutput');
  container.innerHTML = '';
  for (const kind of kinds) {
    const options = devices.filter((d) => d.kind === kind && d.deviceId);
    if (!options.length) continue;
    const label = document.createElement('label');
    label.textContent = DEVICE_LABELS[kind];
    const select = document.createElement('select');
    select.dataset.kind = kind;
    options.forEach((d, i) => {
      const opt = document.createElement('option');
      opt.value = d.deviceId;
      opt.textContent = d.label || `${DEVICE_LABELS[kind]} ${i + 1}`;
      select.appendChild(opt);
    });
    const active = callActiveDevice(kind) || devicePrefs[kind];
    if (active && options.some((d) => d.deviceId === active)) select.value = active;
    select.onchange = async () => {
      devicePrefs[kind] = select.value;
      localStorage.setItem(`device.${kind}`, select.value);
      select.disabled = true;
      try {
        await onChange(kind, select.value);
      } catch (err) {
        setError(`Could not switch ${DEVICE_LABELS[kind].toLowerCase()}: ${err.message}`);
      } finally {
        select.disabled = false;
      }
    };
    label.appendChild(select);
    container.appendChild(label);
  }
  window.testState.deviceKinds = [...container.querySelectorAll('select')].map((s) => s.dataset.kind);
}

function callActiveDevice(kind) {
  return room.state === 'connected' ? room.getActiveDevice(kind) : undefined;
}

/** Pre-join camera preview; also unlocks device labels via the permission prompt. */
async function startPreview() {
  stopPreview();
  const constraint = (kind) =>
    devicePrefs[kind] ? { deviceId: { exact: devicePrefs[kind] } } : true;
  try {
    previewStream = await navigator.mediaDevices.getUserMedia({
      audio: constraint('audioinput'),
      video: constraint('videoinput'),
    });
  } catch (err) {
    if (err && err.name === 'OverconstrainedError') {
      // Saved device is gone (unplugged, different phone): fall back to defaults.
      devicePrefs.audioinput = devicePrefs.videoinput = '';
      return startPreview();
    }
    setError(`Camera/microphone unavailable: ${err.message}. You can still join.`);
    return;
  }
  const preview = document.getElementById('prejoinPreview');
  preview.srcObject = previewStream;
  preview.classList.toggle('no-mirror', isRearCamera(previewStream.getVideoTracks()[0]));
}

function stopPreview() {
  if (previewStream) previewStream.getTracks().forEach((t) => t.stop());
  previewStream = null;
}

async function switchInCall(kind, deviceId) {
  await room.switchActiveDevice(kind, deviceId);
  if (kind === 'videoinput') refreshLocalPreview();
  setStatus(`${DEVICE_LABELS[kind]} switched`);
}

/* Quick device menu from the arrow on the mic/camera buttons. */
const deviceMenu = document.getElementById('deviceMenu');
let deviceMenuAnchor = null;

function closeDeviceMenu() {
  deviceMenu.hidden = true;
  if (deviceMenuAnchor) deviceMenuAnchor.classList.remove('open');
  deviceMenuAnchor = null;
}

async function openDeviceMenu(anchor) {
  if (deviceMenuAnchor === anchor) {
    closeDeviceMenu();
    return;
  }
  closeDeviceMenu();
  deviceMenuAnchor = anchor;
  anchor.classList.add('open');
  let devices = [];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch {
    // Fall through to the "no devices" note.
  }
  const kinds = anchor.dataset.kinds
    .split(',')
    .filter((k) => k !== 'audiooutput' || supportsOutputSelection());
  deviceMenu.innerHTML = '';
  for (const kind of kinds) {
    const options = devices.filter((d) => d.kind === kind && d.deviceId);
    const title = document.createElement('h2');
    title.textContent = DEVICE_LABELS[kind];
    deviceMenu.appendChild(title);
    if (!options.length) {
      const none = document.createElement('div');
      none.className = 'empty';
      none.textContent = 'None found';
      deviceMenu.appendChild(none);
      continue;
    }
    const active = callActiveDevice(kind) || devicePrefs[kind] || 'default';
    options.forEach((d, i) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.setAttribute('role', 'menuitemradio');
      item.dataset.kind = kind;
      item.dataset.deviceId = d.deviceId;
      const isActive = d.deviceId === active;
      item.setAttribute('aria-checked', String(isActive));
      item.innerHTML = '<span class="check"></span><span class="lbl"></span>';
      item.querySelector('.check').textContent = isActive ? '\u2713' : '';
      item.querySelector('.lbl').textContent = d.label || `${DEVICE_LABELS[kind]} ${i + 1}`;
      item.onclick = async () => {
        closeDeviceMenu();
        devicePrefs[kind] = d.deviceId;
        localStorage.setItem(`device.${kind}`, d.deviceId);
        try {
          await switchInCall(kind, d.deviceId);
        } catch (err) {
          setError(`Could not switch ${DEVICE_LABELS[kind].toLowerCase()}: ${err.message}`);
        }
      };
      deviceMenu.appendChild(item);
    });
  }
  // Open above the arrow, kept inside the viewport.
  positionPopup(anchor);
}

/** Generic popup above ``anchor`` using the device-menu container. */
function showPopupMenu(anchor, items) {
  if (deviceMenuAnchor === anchor) {
    closeDeviceMenu();
    return;
  }
  closeDeviceMenu();
  deviceMenuAnchor = anchor;
  deviceMenu.innerHTML = '';
  for (const item of items) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = item.id;
    btn.setAttribute('role', 'menuitem');
    btn.textContent = item.label;
    if (item.danger) btn.classList.add('danger');
    btn.onclick = () => {
      closeDeviceMenu();
      item.onClick();
    };
    deviceMenu.appendChild(btn);
  }
  positionPopup(anchor);
}

function positionPopup(anchor) {
  deviceMenu.hidden = false;
  const r = anchor.getBoundingClientRect();
  const w = deviceMenu.offsetWidth;
  deviceMenu.style.left = `${Math.max(8, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 8))}px`;
  deviceMenu.style.top = `${Math.max(8, r.top - deviceMenu.offsetHeight - 8)}px`;
}

document.addEventListener('click', (ev) => {
  if (!deviceMenu.hidden && !deviceMenu.contains(ev.target) && ev.target !== deviceMenuAnchor) {
    closeDeviceMenu();
  }
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') closeDeviceMenu();
});

function openSettings() {
  document.getElementById('settings').hidden = false;
  renderDeviceSelects(document.getElementById('settingsDevices'), switchInCall);
}

if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', () => {
    if (!document.getElementById('settings').hidden) {
      renderDeviceSelects(document.getElementById('settingsDevices'), switchInCall);
    }
  });
}

function initControls() {
  setButtonIcons(micBtn, 'i-mic', 'i-mic-off');
  setButtonIcons(camBtn, 'i-cam', 'i-cam-off');
  hangupBtn.innerHTML = '<svg class="ic-on"><use href="#i-hangup"/></svg>';
  peopleBtn.innerHTML =
    '<svg class="ic-on"><use href="#i-people"/></svg><span class="badge">1</span>';
  chatBtn.innerHTML =
    '<svg class="ic-on"><use href="#i-chat"/></svg><span class="badge" hidden>0</span>';
  recBtn.innerHTML = '<svg class="ic-on"><use href="#i-rec"/></svg>';
  recBtn.onclick = toggleRecording;
  setButtonIcons(muteAllBtn, 'i-pause', 'i-play');
  muteAllBtn.onclick = toggleMuteAll;
  settingsBtn.innerHTML = '<svg class="ic-on"><use href="#i-settings"/></svg>';
  settingsBtn.onclick = openSettings;
  for (const id of ['micMenuBtn', 'camMenuBtn']) {
    document.getElementById(id).onclick = (ev) => {
      ev.stopPropagation();
      openDeviceMenu(ev.currentTarget);
    };
  }
  document.getElementById('settingsCloseBtn').onclick = () => {
    document.getElementById('settings').hidden = true;
  };
  document.getElementById('settingsRecsBtn').onclick = () => {
    document.getElementById('settings').hidden = true;
    showRecordings();
  };
  document.getElementById('viewRecsBtn').onclick = showRecordings;
  document.getElementById('recRefreshBtn').onclick = showRecordings;
  document.getElementById('recCloseBtn').onclick = closeRecordings;
  micBtn.onclick = toggleMic;
  camBtn.onclick = toggleCamera;
  hangupBtn.onclick = onHangupClick;
  peopleBtn.onclick = () => togglePanel('people');
  chatBtn.onclick = () => togglePanel('chat');
  tabPeople.onclick = () => openPanel('people');
  tabChatBtn.onclick = () => openPanel('chat');
  closePanelBtn.onclick = closePanel;
  chatForm.onsubmit = (ev) => {
    ev.preventDefault();
    const text = chatInput.value;
    chatInput.value = '';
    sendChat(text);
  };
}

/* -------------------------------------------------------------------------
 * Synthetic media (regression harness)
 * ----------------------------------------------------------------------- */

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

  const dest = audioCtx.createMediaStreamDestination();
  source.connect(gain);

  gain.connect(dest);
  source.start();

  const track = dest.stream.getAudioTracks()[0];
  return {
    track,
    audioCtx,
    stop: () => { source.stop(); audioCtx.close(); },
  };
}

/* -------------------------------------------------------------------------
 * Connect + publish
 * ----------------------------------------------------------------------- */

/**
 * Connect to the room and publish media.
 *
 * mode 'synthetic' publishes the clock/tone (or speech fixture) tracks used by
 * the regression harness; mode 'real' publishes the user's camera and mic.
 */
async function connectRoom(wsUrl, roomToken, { mode, label }) {
  setStatus('Connecting…');
  await room.connect(wsUrl, roomToken);

  const localTile = tiles.get(localTileKey);

  setStatus('Publishing tracks…');
  if (mode === 'real') {
    // LiveKit-managed capture (not raw getUserMedia tracks) so the device can
    // be switched mid-call with room.switchActiveDevice. A missing or blocked
    // device must not keep the user out of the call.
    const lp = room.localParticipant;
    const failures = [];
    try {
      await lp.setMicrophoneEnabled(true, deviceOpts('audioinput'));
    } catch (err) {
      failures.push(`microphone (${err.message})`);
    }
    try {
      await lp.setCameraEnabled(true, {
        ...deviceOpts('videoinput'),
        resolution: { width: 1280, height: 720 },
      });
    } catch (err) {
      failures.push(`camera (${err.message})`);
    }
    if (failures.length) setError(`Could not start ${failures.join(' and ')}`);
    if (devicePrefs.audiooutput && supportsOutputSelection()) {
      await room.switchActiveDevice('audiooutput', devicePrefs.audiooutput).catch(() => {});
    }
    refreshLocalPreview();
    const mic = lp.getTrackPublication(Track.Source.Microphone);
    window.testState.localAudioTrack = (mic && mic.track && mic.track.mediaStreamTrack) || null;
  } else {
    const video = createClockVideoTrack(label);
    const audio = audioUrl
      ? await createSpeechAudioTrack(audioUrl)
      : createToneAudioTrack(audioToneHz);

    await ensureAudioContextRunning(audio.audioCtx);

    window.testState.localVideoTrack = video;
    window.testState.localAudioTrack = audio;

    if (video.track && localTile) {
      const previewStream = new MediaStream([video.track.clone()]);
      localTile.videoEl.srcObject = previewStream;
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
  }

  if (localTileKey && mode !== 'real') {
    const t = tiles.get(localTileKey);
    if (t) t.hasVideoTrack = true;
    setTileVideoVisible(localTileKey, true);
    setTileMic(localTileKey, true);
  }
  syncControls();
  // Remote tracks often arrive while we are still publishing ours.
  const haveRemote = [...room.remoteParticipants.values()].some((p) =>
    [...p.trackPublications.values()].some((t) => t.isSubscribed),
  );
  setStatus(haveRemote ? 'Connected' : 'Waiting for others to join…');
}

function joinErrorMessage(statusCode, body) {
  const code = body && body.error && body.error.code;
  switch (code) {
    case 'domain_not_allowed':
    case 'no_allowlist_configured':
      return 'Your Google account is not authorized for support sign-in.';
    case 'invalid_token':
      return 'Sign-in verification failed. Please try again.';
    case 'code_not_found':
      return 'Support session not found (or no longer active).';
    case 'code_expired':
      return 'This join link has expired.';
    case 'role_occupied':
      return 'Another support operator has already joined this session.';
    case 'session_not_joinable':
      return 'This session is no longer joinable.';
    case 'rate_limit':
      return 'Too many join attempts. Wait a moment and try again.';
    default:
      return (body && body.error && body.error.message) || `Join failed (HTTP ${statusCode})`;
  }
}

/** Authenticated support flow: Google sign-in, then POST /v1/sessions/join. */
async function runAuthFlow() {
  setStatus('Sign in to join the support session');
  if (authEl) authEl.hidden = false;

  let config;
  try {
    const resp = await fetch(`${BASE}/v1/sessions/public-config`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    config = await resp.json();
  } catch (err) {
    setError(`Could not load sign-in configuration: ${err.message}`);
    window.testState.error = 'config_load_failed';
    return;
  }
  if (!config.firebase_api_key) {
    setError('Google sign-in is not configured on this server.');
    window.testState.error = 'signin_not_configured';
    return;
  }

  const firebaseVersion = '10.14.1';
  const [{ initializeApp }, authMod] = await Promise.all([
    import(`https://www.gstatic.com/firebasejs/${firebaseVersion}/firebase-app.js`),
    import(`https://www.gstatic.com/firebasejs/${firebaseVersion}/firebase-auth.js`),
  ]);
  const app = initializeApp({
    apiKey: config.firebase_api_key,
    authDomain: config.firebase_auth_domain,
    projectId: config.firebase_project_id,
  });
  const auth = authMod.getAuth(app);
  const provider = new authMod.GoogleAuthProvider();

  let busy = false;
  signInButton.onclick = async () => {
    if (busy) return;
    busy = true;
    errorEl.textContent = '';
    setStatus('Opening Google sign-in…');
    try {
      const result = await authMod.signInWithPopup(auth, provider);
      const user = result.user;
      const idToken = await authMod.getIdToken(user);
      window.testState.signedInEmail = user.email || null;

      setStatus('Joining session…');
      const resp = await fetch(`${BASE}/v1/sessions/join`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${idToken}`,
        },
        body: JSON.stringify({
          session_id: sessionId,
          display_name: user.displayName || user.email || undefined,
        }),
      });
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        const msg = joinErrorMessage(resp.status, body);
        setError(msg);
        window.testState.error = msg;
        setStatus('Sign in to join the support session');
        return;
      }

      if (userInfoEl) {
        userInfoEl.textContent = `Signed in as ${user.email}${
          body.caller_display_name ? ` — caller: ${body.caller_display_name}` : ''
        }`;
      }
      if (authEl) authEl.hidden = true;
      if (callUI) callUI.hidden = false;
      await connectRoom(body.livekit.ws_url, body.livekit.token, {
        mode: 'real',
        label: user.displayName || 'support',
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg.includes('popup-closed') ? 'Sign-in popup was closed.' : msg);
      window.testState.error = msg;
      setStatus('Sign in to join the support session');
    } finally {
      busy = false;
    }
  };
}

/** Open multi-party call: pick a display name, then POST /v1/calls/<id>/join. */
async function runOpenCallFlow() {
  const prejoinEl = document.getElementById('prejoin');
  const form = document.getElementById('prejoinForm');
  const nameInput = document.getElementById('nameInput');
  const joinBtn = document.getElementById('joinCallBtn');
  const copyBtn = document.getElementById('copyLinkBtn');
  const shareUrl = `${window.location.origin}${BASE}/call/${encodeURIComponent(callId)}`;

  document.getElementById('callLink').textContent = shareUrl;
  nameInput.value = params.get('name') || localStorage.getItem('callDisplayName') || '';
  copyBtn.onclick = async () => {
    try {
      await navigator.clipboard.writeText(shareUrl);
      copyBtn.textContent = 'Link copied';
    } catch {
      copyBtn.textContent = shareUrl;
    }
  };
  setStatus(`Call ${callId}`);
  prejoinEl.hidden = false;
  const synthetic = params.get('synthetic') === '1';
  const prejoinDevices = document.getElementById('prejoinDevices');
  const onPrejoinDevice = (kind) => (kind === 'audiooutput' ? null : startPreview());
  if (synthetic) {
    document.getElementById('prejoinPreview').hidden = true;
  } else {
    // Not awaited, for the same reason as the info fetch below.
    startPreview().then(() => renderDeviceSelects(prejoinDevices, onPrejoinDevice));
  }
  // Not awaited: the join handler below must be attached before any click.
  fetch(`${BASE}/v1/calls/${encodeURIComponent(callId)}`)
    .then((r) => r.json())
    .then((info) => {
      const notice = document.getElementById('recNotice');
      notice.hidden = !info.recording_enabled;
      document.getElementById('viewRecsBtn').hidden = !info.recording_enabled;
      notice.classList.toggle('active', info.recording_active);
      notice.textContent = info.recording_active
        ? '\u25CF This call is being recorded.'
        : 'Anyone in this call can start recording.';
    })
    .catch(() => {});

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    if (joinBtn.disabled) return;
    joinBtn.disabled = true;
    errorEl.textContent = '';
    const name = nameInput.value.trim();
    if (name) localStorage.setItem('callDisplayName', name);
    try {
      setStatus('Joining call…');
      const resp = await fetch(`${BASE}/v1/calls/${encodeURIComponent(callId)}/join`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name: name || undefined }),
      });
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        const msg =
          (body && body.error && body.error.message) ||
          `Join failed (HTTP ${resp.status})`;
        setError(msg);
        window.testState.error = msg;
        return;
      }
      stopPreview();
      prejoinEl.hidden = true;
      callToken = body.livekit.token;
      recBtn.hidden = !body.recording_enabled;
      document.getElementById('settingsRecsBtn').hidden = !body.recording_enabled;
      window.testState.recordingEnabled = body.recording_enabled;
      if (callUI) callUI.hidden = false;
      await connectRoom(body.livekit.ws_url, body.livekit.token, {
        mode: synthetic ? 'synthetic' : 'real',
        label: name || 'guest',
      });
      applyRecordingState({ announce: false });
      if (recordingOn) setStatus('This call is being recorded');
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
      window.testState.error = msg;
      setStatus('Failed');
    } finally {
      joinBtn.disabled = false;
    }
  };
}

async function main() {
  try {
    initControls();
    if (token) {
      if (!wsUrlParam) {
        setError('Missing ?ws_url= query parameter');
        window.testState.error = 'missing_ws_url';
        return;
      }
      if (callUI) callUI.hidden = false;
      await connectRoom(wsUrlParam, token, { mode: 'synthetic', label: identity });
    } else if (callId) {
      await runOpenCallFlow();
    } else if (sessionId) {
      await runAuthFlow();
    } else {
      setError('Missing ?token=, ?session=, or ?call= query parameter');
      window.testState.error = 'missing_token';
    }
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
    await room.connect(wsUrlParam, token);
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
  toggleMic,
  toggleCamera,
  toggleRecording,
  toggleMuteAll,
  openSettings,
  switchDevice: switchInCall,
  showRecordings,
  closeRecordings,
  hangUp,
  endCallForEveryone,
  togglePeople: () => togglePanel('people'),
  toggleChat: () => togglePanel('chat'),
  sendChat,
  getChat: () => [...window.testState.chatMessages],
  getParticipants: () => participantSnapshot(),
};

main();
