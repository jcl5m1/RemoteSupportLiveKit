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

const { Room, RoomEvent, Track, DisconnectReason, DataPacket_Kind } = window.LivekitClient;

const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const identityEl = document.getElementById('identity');
const callUI = document.getElementById('callUI');
const tileGrid = document.getElementById('tileGrid');
const peoplePanel = document.getElementById('peoplePanel');
const peopleList = document.getElementById('peopleList');
const hangupBtn = document.getElementById('hangupBtn');
const peopleBtn = document.getElementById('peopleBtn');
const chatBtn = document.getElementById('chatBtn');
const tabPeople = document.getElementById('tabPeople');
const tabChatBtn = document.getElementById('tabChatBtn');
const tabDataBtn = document.getElementById('tabDataBtn');
const dataTab = document.getElementById('dataTab');
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
// Open-call meeting code; editable on the join screen, generated if absent.
// Meeting links are <base>/?call=<code> (the parameter name identifies the
// kind of link). The server also injects the code as APP_CALL_ID.
let callId = window.APP_CALL_ID || params.get('call');
// Path prefix the app is served under ('' or e.g. '/livekit'), derived from
// this page's own URL so API calls work behind a shared hostname.
const BASE =
  window.APP_BASE !== undefined
    ? window.APP_BASE
    : window.location.pathname.replace(/\/support-web(\/.*)?$/, '');
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

// Remote phone camera control (see "Remote phone camera" below).
const CAMCTL_TOPIC = 'camctl';
const IS_PHONE =
  /Android|iPhone|iPod|iPad/i.test(navigator.userAgent) ||
  (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1); // iPadOS
const camEncoder = new TextEncoder();
const camDecoder = new TextDecoder();
const remoteCams = new Map(); // identity -> announced camera state
const pendingCam = new Map(); // request id -> {op, identity, timer}
let torchOn = false;
let lastCamState = '';
window.testState.isPhone = IS_PHONE;
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

// Stage focus. null (default): the other callers fill the stage (tiled when
// there are several) and your own video floats as a small window. A tile key:
// that video alone is the big view and everyone else floats. Clicking a small
// window or one of several big tiles maximizes it; "Show everyone" goes back.
let focusKey = null;

// Small (floating) remote videos are received at 1/4 frame rate: the SFU then
// forwards only the base temporal layer (less to download, decode and draw).
// Adaptive stream already asks for the small simulcast layer, and dynacast
// pauses a sender's larger layers while nobody shows that video big.
const PIP_FPS = 7; // ~1/4 of 30 fps; the base temporal layer is 7.5 fps
// Big views ask for everything (an unset fps is ignored by the SFU, so a
// lowered rate would stick).
const FULL_FPS = 60;

function applyTileQuality() {
  const fps = {};
  room.remoteParticipants.forEach((p) => {
    const t = tiles.get(participantKey(p));
    const pub = p.getTrackPublication(Track.Source.Camera);
    if (!t || !pub || !pub.isSubscribed || !pub.track) return;
    const small = t.root.classList.contains('self-pip');
    pub.setVideoFPS(small ? PIP_FPS : FULL_FPS);
    fps[p.identity] = small ? PIP_FPS : FULL_FPS;
  });
  window.testState.requestedFps = fps;
}

function layoutGrid() {
  const local = localTileKey ? tiles.get(localTileKey) : null;
  const othersKeys = [...tiles.keys()].filter((k) => k !== localTileKey);
  if (focusKey && !tiles.has(focusKey)) focusKey = null; // they left
  // With one other caller, focusing them is the default layout.
  if (focusKey && othersKeys.length === 1 && focusKey === othersKeys[0]) focusKey = null;
  if (!othersKeys.length) focusKey = null;
  let bigKeys;
  if (focusKey) bigKeys = [focusKey];
  else if (othersKeys.length) bigKeys = othersKeys;
  else bigKeys = local ? [localTileKey] : [];
  const pips = [];
  for (const [k, t] of tiles) {
    const pip = !bigKeys.includes(k);
    t.root.classList.toggle('self-pip', pip);
    if (pip) pips.push(t);
  }
  // Stack floating windows upward from the bottom-right corner, wrapping into
  // further columns to the left when a column is full.
  const pipH = parseFloat(getComputedStyle(tileGrid).getPropertyValue('--pip-h')) || 150;
  const perCol = Math.max(1, Math.floor((tileGrid.clientHeight - 12) / (pipH + 10)));
  // Your own window stays nearest the corner.
  pips.sort((a, b) => Number(b === local) - Number(a === local));
  pips.forEach((t, i) => {
    t.root.style.setProperty('--pip-i', String(i % perCol));
    t.root.style.setProperty('--pip-col', String(Math.floor(i / perCol)));
  });
  const selfPip = Boolean(local && local.root.classList.contains('self-pip'));
  window.testState.selfPip = selfPip;
  refreshParticipantColors();
  window.testState.stageFocus = !othersKeys.length || bigKeys[0] === localTileKey ? 'self' : 'remote';
  window.testState.focusIdentity = focusKey ? tiles.get(focusKey).root.dataset.participant : null;
  window.testState.bigCount = bigKeys.length;
  const showAll = document.getElementById('showAllBtn');
  if (showAll) showAll.hidden = !(focusKey && othersKeys.length >= 2);
  syncCameraCycle();
  syncRemoteCamButtons();
  applyTileQuality();
  // Floating windows are out of the grid flow; lay out the rest.
  const n = Math.max(bigKeys.length, 1);
  // One big view (the usual 1:1 call): on phones it fills the stage edge to
  // edge (CSS .solo); with several big views they keep the tiled layout.
  document.getElementById('stageRow').classList.toggle('solo', n === 1);
  updateSoloFit();
  // Portrait phones stack tiles; landscape/desktop fills a grid.
  const portrait = window.innerHeight > window.innerWidth;
  const cols = portrait ? (n <= 2 ? 1 : 2) : n <= 1 ? 1 : n <= 4 ? 2 : 3;
  tileGrid.style.setProperty('--cols', String(cols));
  tileGrid.style.setProperty('--rows', String(Math.ceil(n / cols)));
}

window.addEventListener('resize', () => layoutGrid());
document.getElementById('showAllBtn').addEventListener('click', () => setStageFocus(null));

/** Big phone view: crop-to-fill only if the video's shape is within ~25% of
 * the stage's (e.g. a portrait phone camera on a portrait phone). */
function updateSoloFit() {
  for (const t of tiles.values()) {
    const v = t.videoEl;
    const r = t.root.getBoundingClientRect();
    let cover = false;
    if (v.videoWidth && v.videoHeight && r.width && r.height) {
      cover = Math.abs(Math.log(v.videoWidth / v.videoHeight / (r.width / r.height))) < 0.25;
    }
    t.root.classList.toggle('fit-cover', cover);
  }
}

window.addEventListener('resize', () => updateSoloFit());

/** Maximize one tile (key) or go back to the default layout (null). */
function setStageFocus(key) {
  focusKey = key && tiles.has(key) ? key : null;
  const t = focusKey ? tiles.get(focusKey) : null;
  logEvent('stage_focus', {
    focus: focusKey === localTileKey ? 'self' : 'remote',
    target: t ? t.root.dataset.participant : null,
  });
  layoutGrid();
}

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
  // Match the tile to the video's real shape (portrait phones, rotation).
  const fitTileToVideo = () => {
    const w = videoEl.videoWidth;
    const h = videoEl.videoHeight;
    if (!w || !h) return;
    root.style.setProperty('--ar', `${w} / ${h}`);
    root.style.setProperty('--ar-num', String(w / h));
    root.classList.toggle('portrait', h > w);
    window.testState.tileAspects = window.testState.tileAspects || {};
    window.testState.tileAspects[p.identity || ''] = w / h;
  };
  videoEl.addEventListener('loadedmetadata', fitTileToVideo);
  videoEl.addEventListener('resize', fitTileToVideo);
  videoEl.addEventListener('resize', () => updateSoloFit());
  videoEl.addEventListener('loadedmetadata', () => updateSoloFit());
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
    '<span class="nm"></span>' +
    '<span class="pcolor" aria-hidden="true"></span>';
  chip.querySelector('.nm').textContent = isLocal ? `${name} (You)` : name;
  root.appendChild(chip);

  tileGrid.appendChild(root);
  const entry = { root, videoEl, nameEl: chip.querySelector('.nm'), circleEl: circle, hasVideoTrack: false };
  tiles.set(key, entry);
  if (isLocal) {
    localTileKey = key;
    // Switch camera (front / rear / USB), shown while your camera is the big view.
    const cycle = document.createElement('button');
    cycle.type = 'button';
    cycle.className = 'cam-cycle';
    cycle.id = 'camCycleBtn';
    cycle.title = 'Switch camera';
    cycle.setAttribute('aria-label', 'Switch camera');
    cycle.innerHTML = '<svg viewBox="0 0 24 24"><use href="#i-cam-switch"/></svg>';
    cycle.hidden = true;
    cycle.addEventListener('click', (ev) => {
      ev.stopPropagation();
      cycleCamera();
    });
    root.appendChild(cycle);
  }
  if (!isLocal) addRemoteCamButtons(root, p.identity);
  // Small floating window, or one of several big tiles: click maximizes it.
  // The only big view: click points at that spot on this person's camera,
  // for everyone.
  root.addEventListener('click', (ev) => {
    const bigCount = tileGrid.querySelectorAll('.tile:not(.self-pip)').length;
    if (root.classList.contains('self-pip') || bigCount > 1) {
      setStageFocus(key);
      return;
    }
    sendPointer(p.identity, entry, ev);
  });
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
  const title = userLeft ? 'You left the call' : 'Call ended';
  let detail = userLeft ? '' : 'The session has ended.';
  if (userLeft && endedSession) detail = 'You were the last one here, so the meeting has ended.';
  if (!userLeft && callId && reason === DisconnectReason.ROOM_DELETED) {
    // The meeting was ended server-side (POST /v1/calls/<id>/end).
    const by = roomRecordingMeta().ended_by;
    detail = by ? `${by} ended the call.` : 'The meeting has ended.';
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
  // Tell the newcomer what this phone's camera can do (after their data
  // channel is up).
  setTimeout(() => announceCamState(participant.identity), 1000);
});

room.on(RoomEvent.ParticipantDisconnected, (participant) => {
  setStatus(`Participant left: ${participant.identity}`);
  remoteCams.delete(participant.identity);
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
    applyTileQuality();
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
  announceCamState();
  syncCameraCycle();
  syncControls();
  renderPeople();
  // Get a newly published track recorded now rather than on the next
  // server reconcile tick.
  if (recordingOn) callRecordingRequest('sync').catch(() => {});
});

room.on(RoomEvent.RoomMetadataChanged, () => applyRecordingState({ announce: true }));

room.on(RoomEvent.LocalTrackUnpublished, () => {
  announceCamState();
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
  logData({
    dir: 'in',
    // No participant = sent by the server (backend/agent via the API).
    source: participant ? displayName(participant) : 'Server',
    topic,
    payload,
    reliable: kind === undefined ? undefined : kind === DataPacket_Kind.RELIABLE,
  });
  if (topic === POINTER_TOPIC) {
    handlePointerMessage(payload, participant);
    return;
  }
  if (topic === CAMCTL_TOPIC) {
    handleCamCtl(payload, participant);
    return;
  }
  if (topic !== CHAT_TOPIC) return;
  let msg;
  try {
    msg = JSON.parse(chatDecoder.decode(payload));
  } catch {
    return;
  }
  if (!msg || typeof msg.text !== 'string') return;
  const name = (participant && displayName(participant)) || 'unknown';
  const show = sanitizeShow(msg.show);
  addChatMessage({
    identity: participant ? participant.identity : 'unknown',
    name,
    text: msg.text,
    ts: msg.ts || Date.now(),
    mine: false,
    show,
  });
  if (show) openShowOverlay(show, name);
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
  const noneEnabled = !mediaEnabled.mic && !mediaEnabled.cam;
  muteAllBtn.classList.toggle('off', muted || noneEnabled);
  muteAllBtn.title = noneEnabled
    ? 'Microphone and camera are turned off in Settings'
    : muted
      ? 'Unmute'
      : 'Mute';
  window.testState.media = { ...mediaEnabled, muted };
  setTileMic(localTileKey, lp.isMicrophoneEnabled);
  if (localTileKey) setTileVideoVisible(localTileKey, lp.isCameraEnabled);
}

/*
 * Microphone/camera state. Settings is primary: a device switched off there
 * stays off. The mute button is secondary — it pauses whatever Settings
 * leaves on, and unmuting only restores those.
 */
const mediaEnabled = {
  mic: localStorage.getItem('media.mic') !== 'off',
  cam: localStorage.getItem('media.cam') !== 'off',
};
let muted = false;

/**
 * Capture size request. Always the landscape 1280x720: phone browsers (iOS
 * Safari, Android Chrome) interpret it relative to the sensor and rotate frames
 * to the device orientation themselves, so a phone held upright still sends
 * 720x1280. Requesting 720x1280 when upright got swapped into landscape -- the
 * reverse of the phone's orientation.
 */
function cameraResolution() {
  return { width: 1280, height: 720 };
}

/** Make the published mic/camera match Settings and the mute button. */
async function applyMediaState() {
  const lp = room.localParticipant;
  if (!lp || !lp.identity) return;
  const wantMic = mediaEnabled.mic && !muted;
  const wantCam = mediaEnabled.cam && !muted;
  const ops = [];
  if (lp.isMicrophoneEnabled !== wantMic) {
    ops.push(lp.setMicrophoneEnabled(wantMic, wantMic ? deviceOpts('audioinput') : undefined));
  }
  if (lp.isCameraEnabled !== wantCam) {
    ops.push(
      lp.setCameraEnabled(
        wantCam,
        wantCam ? { ...deviceOpts('videoinput'), resolution: cameraResolution() } : undefined,
      ),
    );
  }
  const failed = (await Promise.allSettled(ops)).find((r) => r.status === 'rejected');
  if (failed) setError(`Could not update microphone/camera: ${failed.reason.message}`);
  refreshLocalPreview();
  syncControls();
  renderPeople();
}

/** Mute button: pause/resume whatever Settings has switched on. */
async function toggleMuteAll() {
  if (!mediaEnabled.mic && !mediaEnabled.cam) {
    setStatus('Microphone and camera are turned off in Settings');
    return;
  }
  muted = !muted;
  setStatus(muted ? 'Muted' : 'Unmuted');
  logEvent('mute', { muted });
  muteAllBtn.disabled = true;
  try {
    await applyMediaState();
  } finally {
    muteAllBtn.disabled = false;
  }
}

/** Settings switch for one device (primary over the mute button). */
async function setMediaEnabled(kind, enabled) {
  mediaEnabled[kind] = enabled;
  localStorage.setItem(`media.${kind}`, enabled ? 'on' : 'off');
  logEvent('media_toggle', { kind, enabled });
  setStatus(`${kind === 'mic' ? 'Microphone' : 'Camera'} ${enabled ? 'enabled' : 'disabled'}`);
  await applyMediaState();
}

let endedSession = false;

function otherHumansPresent() {
  return [...room.remoteParticipants.values()].some(
    (p) => !(p.identity || '').startsWith('agent'),
  );
}

/**
 * Red button: leave the call. If this is the last person in an open call, also
 * end the meeting (stop recording so files finalize, close the room) rather
 * than leaving it to LiveKit's departure timeout. Tab closes / network loss
 * can't make this request; the departure timeout covers those.
 */
async function hangUp() {
  userLeft = true;
  const last = Boolean(callId && callToken) && !otherHumansPresent();
  endedSession = last;
  window.testState.endedSession = last;
  logEvent('leave', { last });
  await flushEvents({ keepalive: true });
  setStatus('Leaving…');
  await room.disconnect();
  if (last) {
    // keepalive: the request survives the user navigating away right after.
    fetch(`${BASE}/v1/calls/${encodeURIComponent(callId)}/end`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${callToken}` },
      keepalive: true,
    }).catch(() => {});
  }
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
  tabDataBtn.classList.toggle('active', tab === 'data');
  peopleList.hidden = tab !== 'people';
  chatTab.hidden = tab !== 'chat';
  dataTab.hidden = tab !== 'data';
  if (tab === 'data') dataTab.scrollTop = dataTab.scrollHeight;
  if (tab === 'chat') {
    clearChatUnread();
    chatMessages.scrollTop = chatMessages.scrollHeight;
    // Phones: don't pop the keyboard over the messages just by opening chat.
    if (window.matchMedia('(pointer: fine)').matches) chatInput.focus();
  }
}

/* -------------------------------------------------------------------------
 * Data-channel log: metadata only (time, source, description, size) —
 * payload contents are never displayed or stored.
 * ----------------------------------------------------------------------- */

const DATA_LOG_MAX = 500;
const dataLog = [];
window.testState.dataLog = dataLog;

/** Describe a payload by its shape (kind, key names, length), never its values. */
function describePayload(topic, bytes) {
  let text = null;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    return 'Binary';
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return `Text (${text.length} chars)`;
  }
  if (topic === CHAT_TOPIC && parsed && typeof parsed.text === 'string') {
    if (parsed.show && typeof parsed.show === 'object') {
      return `Shared media (${SHOW_KIND_LABELS[parsed.show.kind] || 'link'})`;
    }
    return `Chat message (${parsed.text.length} chars)`;
  }
  if (Array.isArray(parsed)) return `JSON array (${parsed.length} items)`;
  if (parsed && typeof parsed === 'object') {
    const keys = Object.keys(parsed);
    const shown = keys.slice(0, 6).join(', ');
    return `JSON {${shown}${keys.length > 6 ? ', …' : ''}}`;
  }
  return `JSON ${typeof parsed}`;
}

function fmtLogTime(ts) {
  const d = new Date(ts);
  return `${d.toTimeString().slice(0, 8)}.${String(d.getMilliseconds()).padStart(3, '0')}`;
}

function logData({ dir, source, topic, payload, reliable }) {
  const entry = {
    ts: Date.now(),
    dir,
    source,
    topic: topic || null,
    description: describePayload(topic, payload),
    size: payload.byteLength,
    reliable,
  };
  dataLog.push(entry);
  if (dataLog.length > DATA_LOG_MAX) dataLog.shift();
  renderDataEntry(entry);
}

function renderDataEntry(entry) {
  const tbody = document.querySelector('#dataLog tbody');
  if (!tbody) return;
  document.getElementById('dataEmpty').hidden = true;
  document.getElementById('dataCount').textContent = `(${dataLog.length})`;
  const tr = document.createElement('tr');
  const cells = {
    t: fmtLogTime(entry.ts),
    src: '',
    desc: '',
    sz: `${entry.size} B`,
  };
  for (const [cls, text] of Object.entries(cells)) {
    const td = document.createElement('td');
    td.className = cls;
    td.textContent = text;
    tr.appendChild(td);
  }
  const dir = document.createElement('span');
  dir.className = 'dir';
  dir.textContent = entry.dir === 'out' ? '\u2191' : '\u2193';
  dir.title = entry.dir === 'out' ? 'Sent' : 'Received';
  tr.children[1].append(dir, document.createTextNode(entry.source));
  tr.children[2].textContent = entry.description;
  const meta = [entry.topic ? `topic: ${entry.topic}` : 'no topic'];
  if (entry.reliable !== undefined) meta.push(entry.reliable ? 'reliable' : 'lossy');
  const topic = document.createElement('span');
  topic.className = 'topic';
  topic.textContent = meta.join(' · ');
  tr.children[2].appendChild(topic);
  while (tbody.children.length >= DATA_LOG_MAX) tbody.firstChild.remove();
  tbody.appendChild(tr);
  if (!dataTab.hidden) dataTab.scrollTop = dataTab.scrollHeight;
}

/*
 * Desktop: drag the side panel's left edge to change its width (People / Chat /
 * Data). Double-click resets; arrow keys nudge when the handle is focused.
 * Width is remembered per device.
 */
const PANEL_DEFAULT_W = 300;
const PANEL_MIN_W = 260;

function panelMaxWidth() {
  return Math.max(PANEL_MIN_W, Math.min(720, Math.floor(window.innerWidth * 0.6)));
}

function setPanelWidth(w, { save = true } = {}) {
  const width = Math.round(Math.min(panelMaxWidth(), Math.max(PANEL_MIN_W, w)));
  peoplePanel.style.setProperty('--panel-w', `${width}px`);
  window.testState.panelWidth = width;
  if (save) localStorage.setItem('panelWidth', String(width));
  layoutGrid();
  return width;
}

(function initPanelResizer() {
  const handle = document.getElementById('panelResizer');
  const saved = Number(localStorage.getItem('panelWidth'));
  setPanelWidth(saved || PANEL_DEFAULT_W, { save: false });
  let drag = null;
  handle.addEventListener('pointerdown', (ev) => {
    ev.preventDefault();
    handle.setPointerCapture(ev.pointerId);
    drag = { x: ev.clientX, w: peoplePanel.getBoundingClientRect().width };
    document.body.classList.add('resizing-panel');
  });
  handle.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    // Panel is on the right: dragging left widens it.
    setPanelWidth(drag.w + (drag.x - ev.clientX), { save: false });
  });
  const end = () => {
    if (!drag) return;
    drag = null;
    document.body.classList.remove('resizing-panel');
    localStorage.setItem('panelWidth', String(window.testState.panelWidth));
  };
  handle.addEventListener('pointerup', end);
  handle.addEventListener('pointercancel', end);
  handle.addEventListener('dblclick', () => setPanelWidth(PANEL_DEFAULT_W));
  handle.addEventListener('keydown', (ev) => {
    const step = ev.shiftKey ? 60 : 20;
    const w = peoplePanel.getBoundingClientRect().width;
    if (ev.key === 'ArrowLeft') setPanelWidth(w + step);
    else if (ev.key === 'ArrowRight') setPanelWidth(w - step);
    else return;
    ev.preventDefault();
  });
  // Keep within bounds if the window shrinks.
  window.addEventListener('resize', () => {
    if (window.testState.panelWidth > panelMaxWidth()) setPanelWidth(panelMaxWidth(), { save: false });
  });
})();

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

function updateChatBadge({ pulse = false } = {}) {
  const badge = chatBtn.querySelector('.badge');
  if (!badge) return;
  badge.hidden = chatUnread === 0;
  badge.textContent = chatUnread > 9 ? '9+' : String(chatUnread);
  chatBtn.title = chatUnread ? `Chat — ${chatUnread} new message(s)` : 'Chat with everyone';
  window.testState.chatUnread = chatUnread;
  if (pulse) {
    badge.classList.remove('pulse');
    void badge.offsetWidth; // restart the animation
    badge.classList.add('pulse');
  }
}

/* Chime for incoming chat: two short sine notes via Web Audio (no asset).
 * The context is created/resumed on the Join click so autoplay policies
 * (notably iOS Safari) allow it to sound later. */
let chimeCtx = null;

function unlockChime() {
  try {
    chimeCtx = chimeCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (chimeCtx.state === 'suspended') chimeCtx.resume();
  } catch {
    chimeCtx = null;
  }
}

function playChime() {
  window.testState.chimeCount = (window.testState.chimeCount || 0) + 1;
  if (!chimeCtx) return;
  if (chimeCtx.state === 'suspended') chimeCtx.resume();
  const t0 = chimeCtx.currentTime;
  [880, 1318.5].forEach((freq, i) => {
    const start = t0 + i * 0.13;
    const osc = chimeCtx.createOscillator();
    const gain = chimeCtx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.18, start + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.4);
    osc.connect(gain).connect(chimeCtx.destination);
    osc.start(start);
    osc.stop(start + 0.42);
  });
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
  if (msg.show) {
    // "/show" message: label + link that reopens the full-screen view.
    body.classList.add('shared');
    const label = document.createElement('div');
    label.className = 'shared-kind';
    label.textContent = `Shared ${SHOW_KIND_LABELS[msg.show.kind] || 'link'}`;
    const link = document.createElement('a');
    link.href = msg.show.final_url;
    link.textContent = msg.show.title || msg.show.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    body.append(label, link);
    if (!msg.mine) {
      const again = document.createElement('button');
      again.type = 'button';
      again.className = 'show-again';
      again.textContent = 'Show';
      again.onclick = () => openShowOverlay(msg.show, msg.name);
      body.appendChild(again);
    }
  } else {
    body.textContent = msg.text;
  }
  wrap.appendChild(meta);
  wrap.appendChild(body);
  chatMessages.appendChild(wrap);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  if (msg.mine) return;
  playChime();
  if (peoplePanel.hidden || activeTab !== 'chat') {
    chatUnread += 1;
    updateChatBadge({ pulse: true });
  }
}

/** Returns true if sent; false leaves the text in the input (e.g. bad /show link). */
async function sendChat(text) {
  const trimmed = (text || '').trim();
  if (!trimmed || !room.localParticipant.identity) return false;
  let show = null;
  const showMatch = trimmed.match(SHOW_RE);
  if (showMatch) {
    show = await checkShowUrl(showMatch[1]);
    if (!show) return false;
  }
  const payload = chatEncoder.encode(
    JSON.stringify({
      text: trimmed,
      name: displayName(room.localParticipant),
      ts: Date.now(),
      ...(show ? { show } : {}),
    }),
  );
  await room.localParticipant.publishData(payload, { reliable: true, topic: CHAT_TOPIC });
  logData({ dir: 'out', source: 'You', topic: CHAT_TOPIC, payload, reliable: true });
  logEvent('chat', show ? { text: trimmed, show } : { text: trimmed });
  addChatMessage({
    identity: room.localParticipant.identity,
    name: displayName(room.localParticipant),
    text: trimmed,
    ts: Date.now(),
    mine: true,
    show,
  });
  return true;
}

/* -------------------------------------------------------------------------
 * "/show <url>": share an image, audio, video, YouTube, map, or webpage that
 * opens full screen for everyone else. The backend checks the link resolves
 * (and classifies it) before anything is sent.
 * ----------------------------------------------------------------------- */

const SHOW_RE = /^\/show\s+(\S+)\s*$/i;
const SHOW_KINDS = ['image', 'audio', 'video', 'youtube', 'map', 'webpage', 'link'];
const SHOW_KIND_LABELS = {
  image: 'image', audio: 'audio file', video: 'video', youtube: 'YouTube video',
  map: 'map', webpage: 'webpage', link: 'link',
};

function chatError(message) {
  // Shown only on the sender's screen, inline in the chat and in the chip.
  setStatus('Link not shared');
  setError(message);
  window.testState.chatError = message;
  const wrap = document.createElement('div');
  wrap.className = 'chat-msg mine chat-error';
  wrap.textContent = message;
  chatMessages.appendChild(wrap);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

async function checkShowUrl(raw) {
  let url = raw;
  if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) url = `https://${url}`; // "example.com/x"
  if (!callId || !callToken) {
    chatError('/show is only available in meetings.');
    return null;
  }
  setStatus('Checking link…');
  try {
    const resp = await fetch(
      `${BASE}/v1/calls/${encodeURIComponent(callId)}/media-check?url=${encodeURIComponent(url)}`,
      { headers: { Authorization: `Bearer ${callToken}` } },
    );
    const body = await resp.json().catch(() => null);
    if (!resp.ok) {
      const reason = (body && body.error && body.error.message) || `HTTP ${resp.status}`;
      chatError(`Can't show ${raw}: ${reason}`);
      return null;
    }
    setStatus(`Shared ${SHOW_KIND_LABELS[body.kind] || 'link'}`);
    return sanitizeShow(body);
  } catch (err) {
    chatError(`Can't show ${raw}: ${err.message}`);
    return null;
  }
}

function isHttpUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === 'https:' || u.protocol === 'http:';
  } catch {
    return false;
  }
}

/**
 * Accept only well-formed http(s) show payloads. A peer can publish anything
 * on the data channel, so never trust it to have gone through media-check.
 */
function sanitizeShow(show) {
  if (!show || typeof show !== 'object' || !SHOW_KINDS.includes(show.kind)) return null;
  if (!isHttpUrl(show.url) || !isHttpUrl(show.final_url)) return null;
  if (show.embed_url && !isHttpUrl(show.embed_url)) return null;
  return {
    kind: show.kind,
    url: String(show.url),
    final_url: String(show.final_url),
    embed_url: show.embed_url ? String(show.embed_url) : null,
    geo: sanitizeGeo(show.geo),
    title: typeof show.title === 'string' ? show.title.slice(0, 120) : null,
  };
}

function sanitizeGeo(geo) {
  if (!geo || typeof geo !== 'object') return null;
  const lat = Number(geo.lat);
  const lng = Number(geo.lng);
  const zoom = Number(geo.zoom);
  if (!Number.isFinite(lat) || !Number.isFinite(lng) || Math.abs(lat) > 90 || Math.abs(lng) > 180) {
    return null;
  }
  return {
    lat,
    lng,
    zoom: Number.isFinite(zoom) ? Math.min(19, Math.max(2, zoom)) : 16,
    label: typeof geo.label === 'string' ? geo.label.slice(0, 200) : null,
  };
}

/*
 * Interactive map for "/show <maps link>": Leaflet + OpenStreetMap tiles, so
 * one finger drags and two fingers zoom. (Google's embedded map in an iframe
 * forces "use two fingers to move the map" on phones, and its gesture mode
 * can't be changed without the paid JS API.) Leaflet loads on first use.
 */
const LEAFLET = {
  css: 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  cssSri: 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=',
  js: 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  jsSri: 'sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=',
};
let leafletLoading = null;
let currentMap = null;

function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  if (leafletLoading) return leafletLoading;
  leafletLoading = new Promise((resolve, reject) => {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = LEAFLET.css;
    css.integrity = LEAFLET.cssSri;
    css.crossOrigin = '';
    document.head.appendChild(css);
    const js = document.createElement('script');
    js.src = LEAFLET.js;
    js.integrity = LEAFLET.jsSri;
    js.crossOrigin = '';
    js.onload = () => resolve(window.L);
    js.onerror = () => {
      leafletLoading = null;
      reject(new Error('map library failed to load'));
    };
    document.head.appendChild(js);
  });
  return leafletLoading;
}

/*
 * "Where am I" on shared maps: a blue dot (+ accuracy circle) from the browser's
 * geolocation. Only shown on this screen -- never sent anywhere. Asked for only
 * when the user taps the locate button; if permission was already granted, the
 * dot appears automatically (no prompt).
 */
let locationWatchId = null;

/**
 * Shared "you are here" logic for both map providers. ``view`` adapts the map:
 *   addButton(onClick) -> button element, showDot(lat, lng, accuracyM),
 *   hasDot(), dotPosition() -> {lat, lng}, frame(me) (fit you + the place).
 */
function trackMyLocation(view, isCurrent) {
  if (!('geolocation' in navigator)) return;
  let fitOnNextFix = false;
  const btn = view.addButton(() => {
    if (view.hasDot()) {
      view.frame(view.dotPosition()); // already tracking: just recentre
    } else {
      fitOnNextFix = true;
      start();
    }
  });
  btn.id = 'mapLocateBtn';
  btn.title = 'Show my location';
  btn.setAttribute('role', 'button');
  btn.setAttribute('aria-label', 'Show my location');
  btn.classList.add('locate-btn');
  btn.innerHTML = '<svg viewBox="0 0 24 24"><use href="#i-my-location"/></svg>';

  function onFix(pos) {
    const me = { lat: pos.coords.latitude, lng: pos.coords.longitude };
    const acc = Math.max(pos.coords.accuracy || 0, 5);
    view.showDot(me.lat, me.lng, acc);
    btn.classList.add('active');
    window.testState.myLocation = { ...me, accuracy: acc };
    if (fitOnNextFix) {
      fitOnNextFix = false;
      view.frame(me);
    }
  }

  function onError(err) {
    fitOnNextFix = false;
    const denied = err && err.code === 1;
    btn.classList.add('error');
    btn.title = denied ? 'Location permission denied' : 'Location unavailable';
    setStatus(denied ? 'Location permission denied' : 'Could not get your location');
    window.testState.myLocationError = denied ? 'denied' : 'unavailable';
  }

  function start() {
    if (locationWatchId !== null) navigator.geolocation.clearWatch(locationWatchId);
    locationWatchId = navigator.geolocation.watchPosition(onFix, onError, {
      enableHighAccuracy: true,
      maximumAge: 10_000,
      timeout: 20_000,
    });
  }

  // Already allowed (earlier tap / site setting): show the dot without prompting.
  if (navigator.permissions && navigator.permissions.query) {
    navigator.permissions
      .query({ name: 'geolocation' })
      .then((status) => {
        if (status.state === 'granted' && isCurrent()) start();
      })
      .catch(() => {});
  }
}

/** Haversine distance in metres. */
function distanceM(a, b) {
  const R = 6371000;
  const rad = (d) => (d * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat);
  const dLng = rad(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function reportMapView(provider, center, zoom, extra = {}) {
  window.testState.map = { provider, lat: center.lat, lng: center.lng, zoom, interactive: true, ...extra };
}

/* --- OpenStreetMap (Leaflet) viewer ------------------------------------ */

async function mountLeafletMap(container, show) {
  const { lat, lng, zoom, label } = show.geo;
  const L = await loadLeaflet();
  if (!container.isConnected) return;
  const map = L.map(container, { zoomControl: true, dragging: true, tap: true });
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);
  map.setView([lat, lng], zoom);
  const marker = L.marker([lat, lng]).addTo(map);
  if (label) marker.bindTooltip(label, { permanent: true, direction: 'top', offset: [-15, -12] });
  const report = () => {
    const b = map.getBounds();
    reportMapView('osm', map.getCenter(), map.getZoom(), {
      bounds: { n: b.getNorth(), s: b.getSouth(), e: b.getEast(), w: b.getWest() },
    });
  };
  map.on('moveend zoomend', report);
  report();

  let dot = null;
  let ring = null;
  const place = L.latLng(lat, lng);
  trackMyLocation(
    {
      addButton(onClick) {
        const Locate = L.Control.extend({
          options: { position: 'topleft' },
          onAdd() {
            const bar = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
            const a = L.DomUtil.create('a', '', bar);
            a.href = '#';
            L.DomEvent.disableClickPropagation(bar);
            L.DomEvent.on(a, 'click', (ev) => {
              L.DomEvent.preventDefault(ev);
              onClick();
            });
            this._btn = a;
            return bar;
          },
        });
        const control = new Locate();
        control.addTo(map);
        return control._btn;
      },
      showDot(dlat, dlng, acc) {
        const me = L.latLng(dlat, dlng);
        if (!dot) {
          ring = L.circle(me, { radius: acc, className: 'my-accuracy', interactive: false }).addTo(map);
          dot = L.circleMarker(me, { radius: 8, className: 'my-location', interactive: false }).addTo(map);
        } else {
          dot.setLatLng(me);
          ring.setLatLng(me).setRadius(acc);
        }
      },
      hasDot: () => Boolean(dot),
      dotPosition: () => dot.getLatLng(),
      frame(me) {
        const ll = L.latLng(me.lat, me.lng);
        if (ll.distanceTo(place) < 50_000) {
          map.fitBounds(L.latLngBounds([ll, place]), { padding: [60, 60], maxZoom: 17 });
        } else {
          map.setView(ll, 15);
        }
      },
    },
    () => currentMap === map,
  );
  currentMap = map;
  requestAnimationFrame(() => map.invalidateSize());
}

/* --- Google Maps viewer (when GOOGLE_MAPS_API_KEY is configured) -------- */

let gmapsLoading = null;
let gmapsBroken = false; // invalid key / referrer -> stay on OpenStreetMap
let activeGoogleMap = null; // { container, show } for the auth-failure fallback

function googleMapsConfig() {
  const cfg = window.APP_CONFIG || {};
  return cfg.googleMapsKey && !gmapsBroken ? cfg : null;
}

function loadGoogleMaps(key) {
  if (window.google && window.google.maps && window.google.maps.Map) {
    return Promise.resolve(window.google.maps);
  }
  if (gmapsLoading) return gmapsLoading;
  gmapsLoading = new Promise((resolve, reject) => {
    window.__gmapsReady = () => resolve(window.google.maps);
    const s = document.createElement('script');
    s.src =
      'https://maps.googleapis.com/maps/api/js' +
      `?key=${encodeURIComponent(key)}&v=weekly&loading=async&callback=__gmapsReady`;
    s.async = true;
    s.onerror = () => {
      gmapsLoading = null;
      reject(new Error('Google Maps failed to load'));
    };
    document.head.appendChild(s);
  });
  return gmapsLoading;
}

// Google calls this when the key is rejected (bad key, referrer not allowed,
// billing off). Swap the open map for the OpenStreetMap viewer.
window.gm_authFailure = () => {
  gmapsBroken = true;
  window.testState.googleMapsAuthFailed = true;
  const active = activeGoogleMap;
  activeGoogleMap = null;
  if (active && active.container.isConnected) {
    const fresh = active.container.cloneNode(false);
    active.container.replaceWith(fresh);
    mountLeafletMap(fresh, active.show).catch(() => {});
  }
};

async function mountGoogleMap(container, show, cfg) {
  const { lat, lng, zoom, label } = show.geo;
  const maps = await loadGoogleMaps(cfg.googleMapsKey);
  if (!container.isConnected) return;
  const map = new maps.Map(container, {
    center: { lat, lng },
    zoom: Math.round(zoom),
    gestureHandling: 'greedy', // one finger drags, like the Google Maps app
    mapTypeControl: true,
    mapTypeControlOptions: {
      style: maps.MapTypeControlStyle.HORIZONTAL_BAR,
      position: maps.ControlPosition.TOP_LEFT,
      mapTypeIds: ['roadmap', 'satellite'], // Map / Satellite (+ Labels checkbox)
    },
    streetViewControl: false,
    fullscreenControl: false,
    clickableIcons: false,
    ...(cfg.googleMapsMapId ? { mapId: cfg.googleMapsMapId } : {}),
  });
  activeGoogleMap = { container, show };
  const marker = new maps.Marker({ position: { lat, lng }, map, title: label || '' });
  if (label) {
    const info = new maps.InfoWindow({ content: label, headerDisabled: true, disableAutoPan: true });
    info.open({ map, anchor: marker });
  }
  const report = () => {
    const c = map.getCenter();
    const b = map.getBounds();
    if (!c) return;
    reportMapView('google', { lat: c.lat(), lng: c.lng() }, map.getZoom(), {
      mapType: map.getMapTypeId(),
      bounds: b && {
        n: b.getNorthEast().lat(), s: b.getSouthWest().lat(),
        e: b.getNorthEast().lng(), w: b.getSouthWest().lng(),
      },
    });
  };
  map.addListener('idle', report);
  map.addListener('maptypeid_changed', report);
  report();

  let dot = null;
  let ring = null;
  trackMyLocation(
    {
      addButton(onClick) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'gmap-locate';
        b.addEventListener('click', onClick);
        map.controls[maps.ControlPosition.RIGHT_BOTTOM].push(b);
        return b;
      },
      showDot(dlat, dlng, acc) {
        const me = { lat: dlat, lng: dlng };
        if (!dot) {
          ring = new maps.Circle({
            map, center: me, radius: acc, clickable: false,
            fillColor: '#1a73e8', fillOpacity: 0.12, strokeColor: '#1a73e8', strokeOpacity: 0.35, strokeWeight: 1,
          });
          dot = new maps.Marker({
            map, position: me, clickable: false, zIndex: 999,
            icon: {
              path: maps.SymbolPath.CIRCLE, scale: 8, fillColor: '#1a73e8', fillOpacity: 1,
              strokeColor: '#ffffff', strokeWeight: 3,
            },
          });
        } else {
          dot.setPosition(me);
          ring.setCenter(me);
          ring.setRadius(acc);
        }
      },
      hasDot: () => Boolean(dot),
      dotPosition: () => ({ lat: dot.getPosition().lat(), lng: dot.getPosition().lng() }),
      frame(me) {
        if (distanceM(me, { lat, lng }) < 50_000) {
          const bounds = new maps.LatLngBounds();
          bounds.extend(me);
          bounds.extend({ lat, lng });
          map.fitBounds(bounds, 60);
          maps.event.addListenerOnce(map, 'idle', () => {
            if (map.getZoom() > 17) map.setZoom(17);
          });
        } else {
          map.setCenter(me);
          map.setZoom(15);
        }
      },
    },
    () => currentMap && currentMap.gmap === map,
  );
  currentMap = { gmap: map, remove() { activeGoogleMap = null; } };
}

/** Google Maps when configured, else OpenStreetMap; last resort the Google embed. */
async function mountInteractiveMap(container, show) {
  const cfg = googleMapsConfig();
  try {
    if (cfg) {
      try {
        await mountGoogleMap(container, show, cfg);
        return;
      } catch {
        // Script blocked/offline: fall through to OpenStreetMap.
      }
    }
    await mountLeafletMap(container, show);
  } catch {
    if (!container.isConnected || !show.embed_url) return;
    const frame = document.createElement('iframe');
    frame.src = show.embed_url;
    frame.className = 'show-media';
    frame.allow = 'fullscreen';
    container.replaceWith(frame);
    window.testState.map = { interactive: false };
  }
}

/*
 * Pinch-zoom / pan for the shared image (touch), plus double-tap/double-click
 * to toggle zoom and wheel zoom on desktop. The image is transformed rather
 * than the page zoomed, so the overlay chrome stays put. Pointer events cover
 * mouse, pen, and touch; touch-action:none stops the browser's own pinch.
 */
const ZOOM_MIN = 1;
const ZOOM_MAX = 6;

function enableImageZoom(stage, img) {
  let scale = 1;
  let tx = 0;
  let ty = 0;
  const pointers = new Map();
  let gesture = null; // snapshot at the start of a pan/pinch

  const center = () => {
    const r = stage.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height };
  };

  function apply() {
    scale = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, scale));
    if (scale === ZOOM_MIN) {
      tx = 0;
      ty = 0;
    } else {
      // Keep the zoomed image covering the stage (no panning past its edges).
      const { w, h } = center();
      const maxX = (w * (scale - 1)) / 2;
      const maxY = (h * (scale - 1)) / 2;
      tx = Math.min(maxX, Math.max(-maxX, tx));
      ty = Math.min(maxY, Math.max(-maxY, ty));
    }
    img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    img.classList.toggle('zoomed', scale > 1);
    window.testState.imageZoom = { scale, tx, ty };
  }

  /** Zoom to ``next`` keeping the screen point (px, py) fixed under the finger/cursor. */
  function zoomAt(next, px, py) {
    const c = center();
    const clamped = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next));
    const ox = px - c.x;
    const oy = py - c.y;
    tx = ox - ((ox - tx) * clamped) / scale;
    ty = oy - ((oy - ty) * clamped) / scale;
    scale = clamped;
    apply();
  }

  function snapshot() {
    const pts = [...pointers.values()];
    if (pts.length >= 2) {
      const [a, b] = pts;
      gesture = {
        dist: Math.hypot(b.x - a.x, b.y - a.y) || 1,
        mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
        scale,
        tx,
        ty,
      };
    } else if (pts.length === 1) {
      gesture = { start: { ...pts[0] }, tx, ty };
    } else {
      gesture = null;
    }
  }

  let tapStart = null;
  let lastTap = null;
  let lastTouchToggleAt = 0;
  const toggleZoomAt = (x, y) => zoomAt(scale > 1 ? 1 : 2.5, x, y);

  img.addEventListener('pointerdown', (ev) => {
    img.setPointerCapture(ev.pointerId);
    pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    tapStart = pointers.size === 1 ? { x: ev.clientX, y: ev.clientY, t: Date.now() } : null;
    snapshot();
  });
  img.addEventListener('pointermove', (ev) => {
    if (!pointers.has(ev.pointerId) || !gesture) return;
    pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    const pts = [...pointers.values()];
    if (pts.length >= 2 && gesture.dist) {
      const [a, b] = pts;
      const dist = Math.hypot(b.x - a.x, b.y - a.y);
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, (gesture.scale * dist) / gesture.dist));
      // Point under the starting midpoint follows the moving midpoint (pinch + pan).
      const c = center();
      const sox = gesture.mid.x - c.x;
      const soy = gesture.mid.y - c.y;
      tx = mid.x - c.x - ((sox - gesture.tx) * next) / gesture.scale;
      ty = mid.y - c.y - ((soy - gesture.ty) * next) / gesture.scale;
      scale = next;
      apply();
    } else if (pts.length === 1 && gesture.start && scale > 1) {
      tx = gesture.tx + (pts[0].x - gesture.start.x);
      ty = gesture.ty + (pts[0].y - gesture.start.y);
      apply();
    }
  });
  const release = (ev) => {
    pointers.delete(ev.pointerId);
    // Touch double-tap (dblclick is unreliable on mobile browsers).
    if (ev.type === 'pointerup' && ev.pointerType === 'touch' && tapStart && pointers.size === 0) {
      const now = Date.now();
      const still = Math.hypot(ev.clientX - tapStart.x, ev.clientY - tapStart.y) < 12;
      if (still && now - tapStart.t < 300) {
        if (lastTap && now - lastTap.t < 350 && Math.hypot(ev.clientX - lastTap.x, ev.clientY - lastTap.y) < 40) {
          toggleZoomAt(ev.clientX, ev.clientY);
          lastTouchToggleAt = now;
          lastTap = null;
        } else {
          lastTap = { x: ev.clientX, y: ev.clientY, t: now };
        }
      }
    }
    tapStart = null;
    snapshot(); // e.g. 2 -> 1 finger continues as a pan from here
  };
  img.addEventListener('pointerup', release);
  img.addEventListener('pointercancel', release);

  // Double-tap (touch) / double-click (mouse): toggle 1x <-> 2.5x at that point.
  img.addEventListener('dblclick', (ev) => {
    ev.preventDefault();
    // A touch double-tap may also fire dblclick; don't toggle twice.
    if (Date.now() - lastTouchToggleAt < 500) return;
    toggleZoomAt(ev.clientX, ev.clientY);
  });
  // Desktop: wheel / trackpad pinch (ctrl+wheel) zooms at the cursor.
  img.addEventListener(
    'wheel',
    (ev) => {
      ev.preventDefault();
      zoomAt(scale * Math.exp(-ev.deltaY * 0.0015), ev.clientX, ev.clientY);
    },
    { passive: false },
  );
  apply();
}

function fmtClock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '--:--';
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/**
 * Audio "/show" player: play/pause, repeat (off by default), and a seekable
 * timeline with current / total time. Autoplay is attempted; if the browser
 * blocks it, the button simply shows "play".
 */
function buildAudioPlayer(src, label) {
  const wrap = document.createElement('div');
  wrap.className = 'audio-player';
  wrap.id = 'audioPlayer';
  const audio = document.createElement('audio');
  audio.src = src;
  audio.preload = 'auto';
  audio.loop = false; // repeat defaults off

  const title = document.createElement('div');
  title.className = 'ap-title';
  try {
    title.textContent = decodeURIComponent(new URL(label).pathname.split('/').pop()) || label;
  } catch {
    title.textContent = label;
  }

  const playBtn = document.createElement('button');
  playBtn.type = 'button';
  playBtn.id = 'apPlay';
  playBtn.className = 'ap-play';

  const repeatBtn = document.createElement('button');
  repeatBtn.type = 'button';
  repeatBtn.id = 'apRepeat';
  repeatBtn.className = 'ap-repeat';
  repeatBtn.title = 'Repeat';
  repeatBtn.innerHTML = '<svg viewBox="0 0 24 24"><use href="#i-repeat"/></svg>';

  const seek = document.createElement('input');
  seek.type = 'range';
  seek.id = 'apSeek';
  seek.min = '0';
  seek.max = '0';
  seek.step = '0.01';
  seek.value = '0';
  seek.setAttribute('aria-label', 'Seek');

  const time = document.createElement('div');
  time.id = 'apTime';
  time.className = 'ap-time';

  const controls = document.createElement('div');
  controls.className = 'ap-controls';
  controls.append(playBtn, repeatBtn);
  wrap.append(title, controls, seek, time, audio);

  let seeking = false;
  const sync = () => {
    const playing = !audio.paused && !audio.ended;
    playBtn.innerHTML = `<svg viewBox="0 0 24 24"><use href="#${playing ? 'i-ap-pause' : 'i-ap-play'}"/></svg>`;
    playBtn.title = playing ? 'Pause' : 'Play';
    playBtn.setAttribute('aria-label', playBtn.title);
    repeatBtn.setAttribute('aria-pressed', String(audio.loop));
    repeatBtn.classList.toggle('on', audio.loop);
    if (Number.isFinite(audio.duration)) seek.max = String(audio.duration);
    if (!seeking) seek.value = String(audio.currentTime);
    const pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
    seek.style.setProperty('--pct', `${pct}%`);
    time.textContent = `${fmtClock(audio.currentTime)} / ${fmtClock(audio.duration)}`;
    window.testState.audioPlayer = {
      playing,
      loop: audio.loop,
      current: audio.currentTime,
      duration: audio.duration,
      text: time.textContent,
    };
  };
  for (const ev of ['play', 'pause', 'ended', 'timeupdate', 'durationchange', 'loadedmetadata', 'seeked']) {
    audio.addEventListener(ev, sync);
  }
  playBtn.onclick = () => {
    if (audio.paused || audio.ended) audio.play().catch(() => {});
    else audio.pause();
  };
  repeatBtn.onclick = () => {
    audio.loop = !audio.loop;
    sync();
  };
  seek.addEventListener('input', () => {
    seeking = true;
    time.textContent = `${fmtClock(Number(seek.value))} / ${fmtClock(audio.duration)}`;
  });
  seek.addEventListener('change', () => {
    audio.currentTime = Number(seek.value);
    seeking = false;
  });
  sync();
  audio.play().catch(sync); // blocked autoplay: stays on "play"
  return wrap;
}

function stopShowMedia(stage) {
  if (locationWatchId !== null) {
    navigator.geolocation.clearWatch(locationWatchId); // stop GPS when the map closes
    locationWatchId = null;
  }
  if (currentMap) {
    currentMap.remove();
    currentMap = null;
  }
  stage.querySelectorAll('audio, video').forEach((m) => {
    m.pause();
    m.removeAttribute('src');
    m.load();
  });
  stage.replaceChildren();
}

function closeShowOverlay() {
  const overlay = document.getElementById('showOverlay');
  if (!overlay.hidden) logEvent('show_close', {});
  stopShowMedia(document.getElementById('showStage'));
  overlay.hidden = true;
  window.testState.showOpen = false;
}

/** Full-screen presentation of a shared link on this (receiving) screen. */
function openShowOverlay(show, fromName) {
  const overlay = document.getElementById('showOverlay');
  const stage = document.getElementById('showStage');
  document.getElementById('showTitle').textContent = show.title ||
    `${fromName} shared ${/^[aeiou]/i.test(SHOW_KIND_LABELS[show.kind]) ? 'an' : 'a'} ${SHOW_KIND_LABELS[show.kind]}`;
  const open = document.getElementById('showOpenLink');
  open.href = show.final_url;
  stopShowMedia(stage); // a new /show replaces whatever was playing

  let media = null;
  if (show.kind === 'image') {
    media = document.createElement('img');
    media.src = show.embed_url || show.final_url;
    media.alt = 'Shared image';
    media.draggable = false;
    enableImageZoom(stage, media);
  } else if (show.kind === 'audio') {
    media = buildAudioPlayer(show.embed_url || show.final_url, show.url);
  } else if (show.kind === 'video') {
    media = document.createElement(show.kind);
    media.src = show.embed_url || show.final_url;
    media.controls = true;
    media.autoplay = true;
    media.playsInline = true;
    media.play().catch(() => {
      // Autoplay with sound was blocked: one tap starts it.
      const tap = document.createElement('button');
      tap.type = 'button';
      tap.className = 'show-tap';
      tap.textContent = '\u25B6 Tap to play';
      tap.onclick = () => {
        tap.remove();
        media.play().catch(() => {});
      };
      stage.appendChild(tap);
    });
  } else if (show.kind === 'map' && show.geo) {
    media = document.createElement('div');
    media.className = 'show-map';
    mountInteractiveMap(media, show);
  } else if (show.embed_url) {
    media = document.createElement('iframe');
    media.src = show.embed_url;
    media.allow = 'autoplay; fullscreen; encrypted-media; picture-in-picture';
    media.allowFullscreen = true;
    media.referrerPolicy = 'strict-origin-when-cross-origin';
    if (show.kind === 'webpage') {
      // Arbitrary site: no top-level navigation of our page.
      media.setAttribute(
        'sandbox',
        'allow-scripts allow-same-origin allow-popups allow-forms allow-presentation',
      );
    }
  } else {
    // Resolved but can't be embedded (e.g. the site forbids framing).
    media = document.createElement('div');
    media.className = 'show-link';
    const p = document.createElement('p');
    p.textContent = "This page can't be shown inside the call.";
    const a = document.createElement('a');
    a.href = show.final_url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = `Open ${show.url}`;
    media.append(p, a);
  }
  media.classList.add('show-media');
  stage.prepend(media);
  overlay.hidden = false;
  window.testState.showOpen = true;
  window.testState.lastShow = { ...show, from: fromName };
}


/* -------------------------------------------------------------------------
 * Interaction log: what this client sends or does (chat, pointer, controls,
 * leave) is reported to the backend, which stamps it with server time and
 * stores it as JSON lines beside the call's recordings. Only the *sender*
 * reports an interaction, so nothing is logged twice. Batched every ~2 s.
 * ----------------------------------------------------------------------- */

const eventQueue = [];
let eventFlushTimer = null;

function logEvent(type, data = {}) {
  if (!callId || !callToken) return; // open calls only (needs the call token)
  const id = (crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random()}`;
  eventQueue.push({ id, type, ts: Date.now(), data });
  window.testState.eventsLogged = (window.testState.eventsLogged || 0) + 1;
  if (eventQueue.length >= 20) flushEvents();
  else if (!eventFlushTimer) eventFlushTimer = setTimeout(() => flushEvents(), 2000);
}

async function flushEvents({ keepalive = false } = {}) {
  clearTimeout(eventFlushTimer);
  eventFlushTimer = null;
  if (!eventQueue.length || !callId || !callToken) return;
  const batch = eventQueue.splice(0, 100);
  try {
    const resp = await fetch(`${BASE}/v1/calls/${encodeURIComponent(callId)}/events`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${callToken}` },
      // sent_at lets the server map each event's ts onto its own clock.
      body: JSON.stringify({ events: batch, sent_at: Date.now() }),
      keepalive, // survives the page closing right after (hang-up)
    });
    if (!resp.ok && resp.status >= 500) throw new Error(`HTTP ${resp.status}`);
    window.testState.eventsFlushed = (window.testState.eventsFlushed || 0) + batch.length;
  } catch {
    eventQueue.unshift(...batch); // retry later; ids make retries idempotent
    if (!eventFlushTimer) eventFlushTimer = setTimeout(() => flushEvents(), 5000);
  }
  if (eventQueue.length && !eventFlushTimer) eventFlushTimer = setTimeout(() => flushEvents(), 500);
}

// Closing the tab without hanging up: send what's queued.
window.addEventListener('pagehide', () => {
  flushEvents({ keepalive: true });
});

/* -------------------------------------------------------------------------
 * Pointer ("ping"): click/tap a big video to blink a ring at that spot on that
 * person's camera, on every screen. The point travels as a fraction of the
 * *video frame* (0..1, unmirrored), so each screen maps it back through its
 * own fit (cover crop / contain bars), mirroring, and size.
 * ----------------------------------------------------------------------- */

const POINTER_TOPIC = 'pointer';
const pointerEncoder = new TextEncoder();

/** Where the video frame is drawn inside its element (object-fit aware). */
function videoContentRect(videoEl) {
  const vw = videoEl.videoWidth;
  const vh = videoEl.videoHeight;
  const ew = videoEl.clientWidth;
  const eh = videoEl.clientHeight;
  if (!vw || !vh || !ew || !eh) return null;
  const fit = getComputedStyle(videoEl).objectFit;
  const scale = fit === 'contain' ? Math.min(ew / vw, eh / vh) : Math.max(ew / vw, eh / vh);
  const w = vw * scale;
  const h = vh * scale;
  return { x: (ew - w) / 2, y: (eh - h) / 2, w, h, ew, eh };
}

function isMirrored(entry) {
  return entry.root.classList.contains('mirror');
}

/** Element-relative click -> normalized frame coordinates (null if off-frame). */
function toFrameCoords(entry, clientX, clientY) {
  const r = videoContentRect(entry.videoEl);
  if (!r) return null;
  const box = entry.videoEl.getBoundingClientRect();
  let u = (clientX - box.left - r.x) / r.w;
  const v = (clientY - box.top - r.y) / r.h;
  if (isMirrored(entry)) u = 1 - u;
  if (u < 0 || u > 1 || v < 0 || v > 1) return null;
  return { x: u, y: v };
}

/** Draw the blinking ring for frame point (x, y) on a tile, scaled to its view. */
/*
 * One colour per participant, the same on every screen: participants ranked by
 * the server's join time (identity as tie-break) pick from a palette ordered
 * colour-blind-safe first -- Okabe-Ito, then Paul Tol "bright".
 */
const PARTICIPANT_COLORS = [
  '#E69F00', // orange
  '#56B4E9', // sky blue
  '#009E73', // bluish green
  '#F0E442', // yellow
  '#0072B2', // blue
  '#D55E00', // vermilion
  '#CC79A7', // reddish purple
  '#EE6677', // Tol red
  '#228833', // Tol green
  '#AA3377', // Tol purple
  '#BBBBBB', // Tol grey
];

function participantColor(identity) {
  const everyone = [room.localParticipant, ...room.remoteParticipants.values()].filter(
    (p) => p && p.identity && !p.identity.startsWith('agent'),
  );
  everyone.sort((a, b) => {
    const ta = a.joinedAt ? a.joinedAt.getTime() : 0;
    const tb = b.joinedAt ? b.joinedAt.getTime() : 0;
    return ta - tb || a.identity.localeCompare(b.identity);
  });
  const idx = Math.max(0, everyone.findIndex((p) => p.identity === identity));
  return PARTICIPANT_COLORS[idx % PARTICIPANT_COLORS.length];
}

/** Colour dot on every name chip (re-run when people join/leave). */
function refreshParticipantColors() {
  const colors = {};
  for (const [key, t] of tiles) {
    const identity = key === localTileKey ? room.localParticipant.identity : t.root.dataset.participant;
    if (!identity) continue;
    const c = participantColor(identity);
    colors[identity] = c;
    t.root.style.setProperty('--pcolor', c);
  }
  window.testState.participantColors = colors;
}

function showPointer(entry, x, y, fromName, color) {
  const r = videoContentRect(entry.videoEl);
  if (!r) return false;
  const u = isMirrored(entry) ? 1 - x : x;
  const px = r.x + u * r.w;
  const py = r.y + y * r.h;
  // Clamp to what's actually visible (cover may crop the frame's edges).
  const cx = Math.min(r.ew, Math.max(0, px));
  const cy = Math.min(r.eh, Math.max(0, py));
  const size = Math.round(Math.min(96, Math.max(18, 0.12 * Math.min(r.ew, r.eh))));
  const ring = document.createElement('div');
  ring.className = 'pointer-ring';
  ring.style.left = `${cx}px`;
  ring.style.top = `${cy}px`;
  ring.style.setProperty('--ring', `${size}px`);
  ring.style.setProperty('--ring-color', color);
  if (fromName) ring.title = `${fromName} pointed here`;
  entry.root.appendChild(ring);
  ring.addEventListener('animationend', () => ring.remove(), { once: true });
  window.testState.lastPointer = {
    x, y, px: cx, py: cy, size, color, tile: entry.root.dataset.participant,
  };
  return true;
}

/** Flash a tile's border (two pulses) in ``color`` -- marks who pointed. */
function flashSource(entry, color) {
  if (!entry) return;
  entry.root.style.setProperty('--flash-color', color);
  entry.root.classList.remove('pointer-flash');
  void entry.root.offsetWidth; // restart the animation
  entry.root.classList.add('pointer-flash');
  window.testState.lastFlash = { tile: entry.root.dataset.participant, color };
}

async function sendPointer(targetIdentity, entry, ev) {
  if (!entry.hasVideoTrack || !room.localParticipant.identity) return;
  const pt = toFrameCoords(entry, ev.clientX, ev.clientY);
  if (!pt) return;
  const color = participantColor(room.localParticipant.identity);
  showPointer(entry, pt.x, pt.y, 'You', color);
  flashSource(tiles.get(localTileKey), color); // I'm the source on my screen
  const payload = pointerEncoder.encode(
    JSON.stringify({
      target: targetIdentity,
      x: +pt.x.toFixed(4),
      y: +pt.y.toFixed(4),
      color, // sender's colour, so every screen shows the same one
      ts: Date.now(),
    }),
  );
  try {
    await room.localParticipant.publishData(payload, { reliable: true, topic: POINTER_TOPIC });
    logData({ dir: 'out', source: 'You', topic: POINTER_TOPIC, payload, reliable: true });
    logEvent('pointer', { target: targetIdentity, x: +pt.x.toFixed(4), y: +pt.y.toFixed(4), color });
    window.testState.pointersSent = (window.testState.pointersSent || 0) + 1;
  } catch {
    // Best effort: the local ring already showed.
  }
}

function handlePointerMessage(payload, participant) {
  let msg;
  try {
    msg = JSON.parse(chatDecoder.decode(payload));
  } catch {
    return;
  }
  const x = Number(msg && msg.x);
  const y = Number(msg && msg.y);
  if (!msg || typeof msg.target !== 'string' || !(x >= 0 && x <= 1 && y >= 0 && y <= 1)) return;
  const isMe = msg.target === room.localParticipant.identity;
  let entry = null;
  if (isMe) {
    entry = tiles.get(localTileKey);
  } else {
    const p = [...room.remoteParticipants.values()].find((rp) => rp.identity === msg.target);
    entry = p ? tiles.get(participantKey(p)) : null;
  }
  if (!entry) return;
  const from = participant ? displayName(participant) : 'Someone';
  // Only palette colours are accepted from the wire; else derive locally.
  const color = PARTICIPANT_COLORS.includes(msg.color)
    ? msg.color
    : participantColor(participant ? participant.identity : '');
  // Ring on the video that was pointed at; border flash on the pointer's own video.
  flashSource(participant ? tiles.get(participantKey(participant)) : null, color);
  if (showPointer(entry, x, y, from, color)) {
    window.testState.pointersReceived = (window.testState.pointersReceived || 0) + 1;
    if (isMe) setStatus(`${from} pointed at your camera`);
  }
}

/* -------------------------------------------------------------------------
 * Switch camera (front / rear / USB UVC), on your big self view.
 * ----------------------------------------------------------------------- */

async function videoInputs() {
  try {
    return (await navigator.mediaDevices.enumerateDevices()).filter(
      (d) => d.kind === 'videoinput' && d.deviceId,
    );
  } catch {
    return [];
  }
}

async function syncCameraCycle() {
  const btn = document.getElementById('camCycleBtn');
  if (!btn) return;
  const local = tiles.get(localTileKey);
  const big = Boolean(local && !local.root.classList.contains('self-pip'));
  const camOn = Boolean(room.localParticipant && room.localParticipant.isCameraEnabled);
  const cams = big && camOn ? await videoInputs() : [];
  btn.hidden = !(big && camOn && cams.length > 1);
  window.testState.camCycleVisible = !btn.hidden;
}

async function cycleCamera() {
  const cams = await videoInputs();
  if (cams.length < 2) return;
  const current = room.getActiveDevice('videoinput') || devicePrefs.videoinput;
  const idx = cams.findIndex((d) => d.deviceId === current);
  const next = cams[(idx + 1) % cams.length];
  const btn = document.getElementById('camCycleBtn');
  if (btn) btn.disabled = true;
  try {
    devicePrefs.videoinput = next.deviceId;
    localStorage.setItem('device.videoinput', next.deviceId);
    await room.switchActiveDevice('videoinput', next.deviceId);
    refreshLocalPreview();
    const label = next.label || `Camera ${cams.indexOf(next) + 1}`;
    setStatus(`Camera: ${label}`);
    window.testState.cameraLabel = label;
    logEvent('camera_switch', { label });
    window.testState.cameraDeviceId = next.deviceId;
    torchOn = false; // a different camera starts with its light off
    announceCamState();
  } catch (err) {
    setError(`Could not switch camera: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* -------------------------------------------------------------------------
 * Remote phone camera (data topic 'camctl'). A phone announces what its camera
 * can do ({type:'state'}); anyone else in the call gets buttons on its big
 * view to switch its camera, toggle its flashlight, or take a full-resolution
 * photo, which the phone uploads and shows to whoever asked (same overlay as
 * "/show"). Commands go only to the phone; it replies {type:'result'}.
 * ----------------------------------------------------------------------- */


function localCameraTrack() {
  const lp = room.localParticipant;
  const pub = lp && lp.getTrackPublication(Track.Source.Camera);
  return pub && pub.track ? pub.track.mediaStreamTrack : null;
}

function torchSupported(track) {
  try {
    return Boolean(track && track.getCapabilities && track.getCapabilities().torch);
  } catch {
    return false;
  }
}

async function sendCamCtl(msg, to) {
  const payload = camEncoder.encode(JSON.stringify(msg));
  const opts = { reliable: true, topic: CAMCTL_TOPIC };
  if (to) opts.destinationIdentities = [to];
  await room.localParticipant.publishData(payload, opts);
  logData({ dir: 'out', source: 'You', topic: CAMCTL_TOPIC, payload, reliable: true });
}

/** Phone only: broadcast camera state (to one newcomer, or everyone on change). */
async function announceCamState(to) {
  if (!IS_PHONE || room.state !== 'connected' || !room.localParticipant.identity) return;
  const track = localCameraTrack();
  const live = Boolean(track && track.readyState === 'live' && room.localParticipant.isCameraEnabled);
  const state = {
    type: 'state',
    phone: true,
    cam: live,
    cams: live ? (await videoInputs()).length : 0,
    torch: live && torchSupported(track),
    torchOn: live && torchOn,
  };
  window.testState.myCamState = state;
  const key = JSON.stringify(state);
  if (!to && key === lastCamState) return;
  if (!to) lastCamState = key;
  await sendCamCtl(state, to).catch(() => {});
}

async function setTorch(on) {
  const track = localCameraTrack();
  if (!torchSupported(track)) throw new Error('this camera has no flashlight');
  await track.applyConstraints({ advanced: [{ torch: on }] });
  torchOn = on;
  window.testState.torchOn = on;
  logEvent('torch', { on });
}

/** Resolution constraints for the published camera (plus the light, if on). */
function cameraConstraints(size) {
  return {
    width: { ideal: size.width },
    height: { ideal: size.height },
    ...(torchOn ? { advanced: [{ torch: true }] } : {}),
  };
}

function nextFrame(video) {
  return new Promise((resolve) => {
    if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(() => resolve());
    setTimeout(resolve, 400);
  });
}

/** Full-resolution still: ImageCapture.takePhoto (Android Chrome) or, where
 * that's missing (iOS Safari), briefly raise the camera to its largest size and
 * grab a frame; the published video drops back to normal right after. */
async function capturePhoto() {
  const track = localCameraTrack();
  if (!track || track.readyState !== 'live') throw new Error('camera is off');
  if (typeof ImageCapture === 'function') {
    try {
      const capture = new ImageCapture(track);
      let opts = {};
      try {
        const caps = await capture.getPhotoCapabilities();
        if (caps.imageWidth && caps.imageWidth.max) {
          opts = { imageWidth: caps.imageWidth.max, imageHeight: caps.imageHeight.max };
        }
      } catch {
        // No photo capabilities: take it at the default size.
      }
      const blob = await capture.takePhoto(opts);
      if (torchOn) track.applyConstraints({ advanced: [{ torch: true }] }).catch(() => {});
      if (blob && blob.size) return blob;
    } catch (err) {
      console.warn('takePhoto failed; grabbing a video frame instead', err);
    }
  }
  const caps = (track.getCapabilities && track.getCapabilities()) || {};
  const before = track.getSettings();
  const beforeArea = (before.width || 0) * (before.height || 0);
  const maxW = caps.width && caps.width.max;
  const maxH = caps.height && caps.height.max;
  let raised = false;
  if (maxW && maxH && maxW * maxH > beforeArea) {
    try {
      await track.applyConstraints(cameraConstraints({ width: maxW, height: maxH }));
      raised = true;
    } catch {
      // Can't go bigger: use the current size.
    }
  }
  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  try {
    video.srcObject = new MediaStream([track]);
    await video.play().catch(() => {});
    const deadline = Date.now() + 3000;
    do {
      await nextFrame(video);
    } while (
      Date.now() < deadline &&
      (!video.videoWidth || (raised && video.videoWidth * video.videoHeight <= beforeArea))
    );
    if (raised && video.videoWidth * video.videoHeight <= beforeArea) {
      // The camera didn't go bigger (some fall back to a small mode): restore
      // the normal size and take the photo at that instead.
      await track.applyConstraints(cameraConstraints(cameraResolution())).catch(() => {});
      raised = false;
      const until = Date.now() + 2000;
      do {
        await nextFrame(video);
      } while (Date.now() < until && video.videoWidth * video.videoHeight < beforeArea);
    }
    await nextFrame(video); // let exposure settle after the size change
    if (!video.videoWidth) throw new Error('no camera frame');
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    return await new Promise((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('could not encode photo'))), 'image/jpeg', 0.92),
    );
  } finally {
    video.srcObject = null;
    if (raised) await track.applyConstraints(cameraConstraints(cameraResolution())).catch(() => {});
  }
}

async function imageSize(blob) {
  try {
    const bmp = await createImageBitmap(blob);
    const size = { width: bmp.width, height: bmp.height };
    bmp.close();
    return size;
  } catch {
    return null;
  }
}

/** Phone: take a photo, upload it, and show it to the requester. */
async function takeAndSendPhoto(requester) {
  let blob = await capturePhoto();
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(blob.type)) {
    blob = new Blob([blob], { type: 'image/jpeg' });
  }
  const size = await imageSize(blob);
  const q = new URLSearchParams({ requested_by: requester.identity });
  if (size) {
    q.set('width', String(size.width));
    q.set('height', String(size.height));
  }
  const resp = await fetch(`${BASE}/v1/calls/${encodeURIComponent(callId)}/photos?${q}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${callToken}`, 'Content-Type': blob.type },
    body: blob,
  });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) throw new Error((body && body.error && body.error.message) || `upload failed (HTTP ${resp.status})`);
  const me = displayName(room.localParticipant);
  const url = `${window.location.origin}${BASE}${body.path}`;
  const show = { kind: 'image', url, final_url: url, embed_url: null, title: `Photo from ${me}` };
  const payload = chatEncoder.encode(
    JSON.stringify({ text: `Photo from ${me}`, name: me, ts: Date.now(), show, photo: size }),
  );
  await room.localParticipant.publishData(payload, {
    reliable: true,
    topic: CHAT_TOPIC,
    destinationIdentities: [requester.identity],
  });
  logData({ dir: 'out', source: 'You', topic: CHAT_TOPIC, payload, reliable: true });
  window.testState.photosSent = (window.testState.photosSent || 0) + 1;
  window.testState.lastPhoto = { ...body, ...(size || {}), type: blob.type };
  return body;
}

function flashLocalTile(cls) {
  const t = tiles.get(localTileKey);
  if (!t) return;
  t.root.classList.remove(cls);
  void t.root.offsetWidth; // restart the animation
  t.root.classList.add(cls);
  setTimeout(() => t.root.classList.remove(cls), 800);
}

async function handleCamCtl(payload, participant) {
  if (!participant) return;
  let msg;
  try {
    msg = JSON.parse(camDecoder.decode(payload));
  } catch {
    return;
  }
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'state') {
    remoteCams.set(participant.identity, {
      phone: msg.phone === true,
      cam: msg.cam === true,
      cams: Math.max(0, Math.min(16, Number(msg.cams) || 0)),
      torch: msg.torch === true,
      torchOn: msg.torchOn === true,
    });
    window.testState.remoteCams = Object.fromEntries(remoteCams);
    syncRemoteCamButtons();
    return;
  }
  if (msg.type === 'result') {
    handleCamResult(msg, participant);
    return;
  }
  if (msg.type !== 'cmd' || !IS_PHONE || !['cycle', 'torch', 'photo'].includes(msg.op)) return;
  const from = displayName(participant);
  const result = { type: 'result', id: String(msg.id || '').slice(0, 64), op: msg.op, ok: true };
  try {
    if (msg.op === 'cycle') {
      setStatus(`${from} switched your camera`);
      await cycleCamera();
    } else if (msg.op === 'torch') {
      await setTorch(Boolean(msg.on));
      setStatus(`${from} turned your flashlight ${torchOn ? 'on' : 'off'}`);
    } else {
      setStatus(`${from} is taking a photo with your camera`);
      flashLocalTile('photo-flash');
      const photo = await takeAndSendPhoto(participant);
      result.photo = photo.name;
      setStatus(`Photo sent to ${from}`);
    }
  } catch (err) {
    result.ok = false;
    result.error = String((err && err.message) || err).slice(0, 200);
    setError(`Could not ${msg.op === 'photo' ? 'take photo' : msg.op === 'torch' ? 'switch flashlight' : 'switch camera'}: ${result.error}`);
  }
  logEvent('camera_control', { op: msg.op, by: participant.identity, ok: result.ok });
  await announceCamState();
  await sendCamCtl(result, participant.identity).catch(() => {});
}

function handleCamResult(msg, participant) {
  const pending = pendingCam.get(msg.id);
  if (!pending || pending.identity !== participant.identity) return;
  clearTimeout(pending.timer);
  pendingCam.delete(msg.id);
  const name = displayName(participant);
  (window.testState.camResults = window.testState.camResults || []).push({ op: msg.op, ok: msg.ok === true });
  if (msg.ok !== true) setError(`${name}'s phone: ${String(msg.error || 'failed').slice(0, 200)}`);
  else if (pending.op === 'photo') setStatus(`Photo received from ${name}`);
  syncRemoteCamButtons();
}

/** Viewer: send a camera command to a phone. */
async function requestCam(identity, op) {
  const p = room.remoteParticipants.get(identity);
  const state = remoteCams.get(identity);
  if (!p || !state) return;
  const name = displayName(p);
  const id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  const msg = { type: 'cmd', op, id };
  if (op === 'torch') msg.on = !state.torchOn;
  const timer = setTimeout(() => {
    pendingCam.delete(id);
    setError(`${name}'s phone did not respond`);
    syncRemoteCamButtons();
  }, op === 'photo' ? 60000 : 15000);
  pendingCam.set(id, { op, identity, timer });
  if (op === 'photo') setStatus(`Taking a photo with ${name}'s camera…`);
  syncRemoteCamButtons();
  try {
    await sendCamCtl(msg, identity);
    logEvent('camera_control', { op, target: identity, ...(op === 'torch' ? { on: msg.on } : {}) });
  } catch (err) {
    clearTimeout(timer);
    pendingCam.delete(id);
    setError(`Could not reach ${name}'s phone: ${err.message}`);
    syncRemoteCamButtons();
  }
}

const REMOTE_CAM_BUTTONS = [
  { op: 'cycle', icon: 'i-cam-switch', label: 'Switch their camera' },
  { op: 'photo', icon: 'i-photo', label: 'Take a photo with their camera' },
  { op: 'torch', icon: 'i-flashlight', label: 'Flashlight' },
];

function addRemoteCamButtons(root, identity) {
  const bar = document.createElement('div');
  bar.className = 'remote-cam';
  bar.hidden = true;
  for (const b of REMOTE_CAM_BUTTONS) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `cam-cycle rc-${b.op}`;
    btn.title = b.label;
    btn.setAttribute('aria-label', b.label);
    btn.innerHTML = `<svg viewBox="0 0 24 24"><use href="#${b.icon}"/></svg>`;
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation(); // not a pointer click
      requestCam(identity, b.op);
    });
    bar.appendChild(btn);
  }
  root.appendChild(bar);
}

/** Show phone controls on big views of phones whose camera is on. */
function syncRemoteCamButtons() {
  for (const [identity, state] of remoteCams) {
    const t = [...tiles.values()].find((x) => x.root.dataset.participant === identity);
    const bar = t && t.root.querySelector('.remote-cam');
    if (!bar) continue;
    const busy = (op) => [...pendingCam.values()].some((x) => x.identity === identity && x.op === op);
    bar.hidden = !(state.phone && state.cam) || t.root.classList.contains('self-pip');
    const cycle = bar.querySelector('.rc-cycle');
    const photo = bar.querySelector('.rc-photo');
    const torch = bar.querySelector('.rc-torch');
    cycle.hidden = state.cams < 2;
    torch.hidden = !state.torch;
    torch.setAttribute('aria-pressed', String(state.torchOn));
    torch.title = state.torchOn ? 'Turn their flashlight off' : 'Turn their flashlight on';
    cycle.disabled = busy('cycle');
    photo.disabled = busy('photo');
    torch.disabled = busy('torch');
  }
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
  const logs = body.event_logs || [];
  window.testState.eventLogs = logs;
  for (const log of logs) {
    const li = document.createElement('li');
    li.className = 'rec-log';
    const head = document.createElement('div');
    head.className = 'rec-head';
    const what = document.createElement('span');
    what.className = 'rec-who';
    what.textContent = 'Interaction log';
    const meta = document.createElement('span');
    meta.className = 'rec-meta';
    meta.textContent = `${new Date(log.started_at).toLocaleString()} · chat, pointer, controls · ${fmtBytes(log.size_bytes)}`;
    const dl = document.createElement('a');
    dl.href = log.url;
    dl.textContent = 'Download';
    dl.target = '_blank';
    dl.rel = 'noopener';
    head.append(what, meta, dl);
    li.appendChild(head);
    list.appendChild(li);
  }
  if (logs.length) empty.hidden = true;
  const photos = body.photos || [];
  window.testState.callPhotos = photos;
  for (const photo of photos) {
    const li = document.createElement('li');
    li.className = 'rec-photo';
    const head = document.createElement('div');
    head.className = 'rec-head';
    const who = document.createElement('span');
    who.className = 'rec-who';
    who.textContent = `Photo · ${photo.participant}`;
    const meta = document.createElement('span');
    meta.className = 'rec-meta';
    meta.textContent = `${new Date(photo.taken_at).toLocaleString()} · ${fmtBytes(photo.size_bytes)}`;
    const dl = document.createElement('a');
    dl.href = photo.url;
    dl.textContent = 'Download';
    dl.target = '_blank';
    dl.rel = 'noopener';
    head.append(who, meta, dl);
    const img = document.createElement('img');
    img.src = photo.url;
    img.alt = `Photo from ${photo.participant}`;
    img.loading = 'lazy';
    li.append(head, img);
    list.appendChild(li);
  }
  if (photos.length) empty.hidden = true;
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
  // Camera on its own row, then microphone and speaker side by side.
  const kinds = ['videoinput', 'audioinput'];
  if (supportsOutputSelection()) kinds.push('audiooutput');
  container.innerHTML = '';
  const audioRow = document.createElement('div');
  audioRow.className = 'dev-row';
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
    if (kind === 'videoinput') container.appendChild(label);
    else audioRow.appendChild(label);
  }
  if (audioRow.children.length) container.appendChild(audioRow);
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
  logEvent('device_switch', { kind });
  if (kind === 'videoinput') refreshLocalPreview();
  setStatus(`${DEVICE_LABELS[kind]} switched`);
}

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
  hangupBtn.onclick = hangUp;
  peopleBtn.onclick = () => togglePanel('people');
  chatBtn.onclick = () => togglePanel('chat');
  tabPeople.onclick = () => openPanel('people');
  tabChatBtn.onclick = () => openPanel('chat');
  tabDataBtn.onclick = () => openPanel('data');
  closePanelBtn.onclick = closePanel;
  chatForm.onsubmit = async (ev) => {
    ev.preventDefault();
    const text = chatInput.value;
    chatInput.value = '';
    const sent = await sendChat(text);
    if (!sent && SHOW_RE.test(text.trim())) chatInput.value = text; // let them fix it
  };
  document.getElementById('showCloseBtn').onclick = closeShowOverlay;
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !document.getElementById('showOverlay').hidden) closeShowOverlay();
  });
  const micToggle = document.getElementById('micEnabledToggle');
  const camToggle = document.getElementById('camEnabledToggle');
  micToggle.checked = mediaEnabled.mic;
  camToggle.checked = mediaEnabled.cam;
  micToggle.onchange = () => setMediaEnabled('mic', micToggle.checked);
  camToggle.onchange = () => setMediaEnabled('cam', camToggle.checked);
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
    // Devices switched off in Settings stay off when joining.
    try {
      if (mediaEnabled.mic) await lp.setMicrophoneEnabled(true, deviceOpts('audioinput'));
    } catch (err) {
      failures.push(`microphone (${err.message})`);
    }
    try {
      if (mediaEnabled.cam) {
        await lp.setCameraEnabled(true, {
          ...deviceOpts('videoinput'),
          resolution: cameraResolution(),
        });
      }
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
    unlockChime();
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

/* -------------------------------------------------------------------------
 * On-screen keyboard (phones): size the call screen to the visual viewport,
 * the part not covered by the keyboard. Android Chrome also honours
 * interactive-widget=resizes-content in the viewport meta; iOS Safari needs
 * this. Without it the chat messages and input sat under the keyboard.
 * ----------------------------------------------------------------------- */

function fitToVisualViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  const root = document.documentElement.style;
  root.setProperty('--vv-height', `${vv.height}px`);
  root.setProperty('--vv-top', `${vv.offsetTop}px`);
  if (document.activeElement === chatInput) {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }
}

if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', fitToVisualViewport);
  window.visualViewport.addEventListener('scroll', fitToVisualViewport);
  fitToVisualViewport();
}

chatInput.addEventListener('focus', () => {
  if (window.matchMedia('(max-width: 600px)').matches) document.body.classList.add('typing');
  setTimeout(fitToVisualViewport, 50);
});
chatInput.addEventListener('blur', () => {
  document.body.classList.remove('typing');
  setTimeout(fitToVisualViewport, 50);
});

/** Open multi-party call: pick a display name, then POST /v1/calls/<id>/join. */
// Mirrors the server's call-id rule (routers/calls.py _CALL_ID_RE).
const CALL_CODE_RE = /^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$/;

/** Meet-style ``xxx-xxxx-xxx`` code, ~50 bits of entropy (no 'l', like the server). */
function newCallCode() {
  const alphabet = 'abcdefghijkmnopqrstuvwxyz';
  const rand = crypto.getRandomValues(new Uint32Array(10));
  const chars = [...rand].map((r) => alphabet[r % alphabet.length]);
  return `${chars.slice(0, 3).join('')}-${chars.slice(3, 7).join('')}-${chars.slice(7).join('')}`;
}

/** Copy to the clipboard; falls back to execCommand for older mobile browsers. */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    ta.remove();
    return ok;
  }
}

function normalizeCallCode(raw) {
  return raw.trim().toLowerCase().replace(/[\s_]+/g, '-');
}

/**
 * Join screen for open calls. ``fresh`` = no code in the URL: a new code is
 * generated and the button reads "Start meeting" until the code is edited.
 */
async function runOpenCallFlow({ fresh = false } = {}) {
  const prejoinEl = document.getElementById('prejoin');
  const form = document.getElementById('prejoinForm');
  const nameInput = document.getElementById('nameInput');
  const joinBtn = document.getElementById('joinCallBtn');
  const copyBtn = document.getElementById('copyCodeBtn');
  const codeInput = document.getElementById('callCodeInput');
  const codeError = document.getElementById('codeError');
  const shareUrl = () => `${window.location.origin}${BASE}/?call=${encodeURIComponent(callId)}`;
  let codeEdited = false;
  let infoTimer = null;

  function syncCodeUi() {
    const valid = CALL_CODE_RE.test(callId);
    codeInput.classList.toggle('invalid', !valid);
    codeError.hidden = valid;
    joinBtn.disabled = !valid;
    const starting = fresh && !codeEdited;
    joinBtn.textContent = starting ? 'Start meeting' : 'Join now';
    document.getElementById('prejoinTitle').textContent = starting
      ? 'Start a new meeting'
      : 'Ready to join?';
    copyBtn.disabled = !valid;
    window.testState.callCode = callId;
    if (valid) {
      // Keep the address bar on the canonical, shareable meeting URL for the
      // current code: <base>/?call=<code> (also tidies legacy forms).
      params.delete('call');
      const rest = params.toString();
      history.replaceState(
        null,
        '',
        `${BASE}/?call=${encodeURIComponent(callId)}${rest ? `&${rest}` : ''}`,
      );
      setStatus(`Call ${callId}`);
    }
  }

  callId = normalizeCallCode(callId);
  codeInput.value = callId;
  codeInput.oninput = () => {
    const normalized = normalizeCallCode(codeInput.value);
    if (normalized !== codeInput.value) codeInput.value = normalized;
    codeEdited = true;
    callId = normalized;
    copyBtn.classList.remove('copied');
    syncCodeUi();
    clearTimeout(infoTimer);
    if (CALL_CODE_RE.test(callId)) infoTimer = setTimeout(refreshCallInfo, 300);
  };
  syncCodeUi();
  nameInput.value = params.get('name') || localStorage.getItem('callDisplayName') || '';
  let copiedTimer = null;
  copyBtn.onclick = async () => {
    const url = shareUrl();
    if (!(await copyText(url))) {
      // No clipboard access (e.g. insecure context): let the user copy it.
      window.prompt('Copy this meeting link', url);
      return;
    }
    window.testState.copiedUrl = url;
    copyBtn.classList.add('copied');
    copyBtn.title = 'Link copied';
    clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => {
      copyBtn.classList.remove('copied');
      copyBtn.title = 'Copy meeting link';
    }, 1500);
  };
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
  // Recording notice for the current code; re-run (debounced) when it changes.
  // Not awaited: the join handler below must be attached before any click.
  function refreshCallInfo() {
    const code = callId;
    fetch(`${BASE}/v1/calls/${encodeURIComponent(code)}`)
      .then((r) => r.json())
      .then((info) => {
        if (code !== callId) return; // a newer code was typed meanwhile
        const notice = document.getElementById('recNotice');
        // Only warn when a recording is already running.
        notice.hidden = !(info.recording_enabled && info.recording_active);
        document.getElementById('viewRecsBtn').hidden = !info.recording_enabled;
        notice.classList.toggle('active', info.recording_active);
        notice.textContent = '\u25CF This call is being recorded.';
      })
      .catch(() => {});
  }
  refreshCallInfo();

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    if (joinBtn.disabled || !CALL_CODE_RE.test(callId)) return;
    unlockChime(); // inside the click gesture, so the chime may play later
    joinBtn.disabled = true;
    codeInput.disabled = true;
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
      codeInput.disabled = false;
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
      // No code: show the join screen with a new meeting code to start.
      callId = newCallCode();
      await runOpenCallFlow({ fresh: true });
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
  toggleRecording,
  setStageFocus,
  cycleCamera,
  setMediaEnabled,
  closeShowOverlay,
  toggleMuteAll,
  openSettings,
  switchDevice: switchInCall,
  showRecordings,
  closeRecordings,
  hangUp,
  togglePeople: () => togglePanel('people'),
  toggleChat: () => togglePanel('chat'),
  sendChat,
  getChat: () => [...window.testState.chatMessages],
  getDataLog: () => dataLog.map((e) => ({ ...e })),
  openDataLog: () => openPanel('data'),
  getParticipants: () => participantSnapshot(),
};

main();
