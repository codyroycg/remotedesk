// RemoteDesk technician console
const $ = (id) => document.getElementById(id);
const views = ['login', 'dashboard', 'session'];
function show(view) { views.forEach(v => $(v).classList.toggle('hidden', v !== view)); }

let token = sessionStorage.getItem('rd_token');
let user = sessionStorage.getItem('rd_user');
let ws = null;
let inSession = false;

// ---------- login ----------
$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('loginError').textContent = '';
  const res = await fetch('/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: $('techName').value, password: $('password').value }),
  });
  if (!res.ok) { $('loginError').textContent = res.status === 429 ? 'Too many attempts' : 'Invalid password'; return; }
  ({ token, user } = await res.json());
  sessionStorage.setItem('rd_token', token);
  sessionStorage.setItem('rd_user', user);
  connect();
});
$('logout').onclick = () => { sessionStorage.clear(); token = null; ws && ws.close(); show('login'); };

// ---------- websocket ----------
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/tech?token=${encodeURIComponent(token)}`);
  ws.binaryType = 'blob';
  ws.onopen = () => { $('whoami').textContent = user; show('dashboard'); };
  ws.onclose = () => {
    if (!token) return;
    if (inSession) endSessionUI('Connection to server lost');
    // token probably expired if we never opened
    setTimeout(() => token && connect(), 2000);
  };
  ws.onmessage = (ev) => {
    if (ev.data instanceof Blob) return drawFrame(ev.data);
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case 'agents': renderAgents(msg.agents); break;
      case 'waiting': $('joinStatus').textContent = msg.message; break;
      case 'join_error': $('joinStatus').textContent = msg.error; break;
      case 'joined': startSessionUI(msg.agent); break;
      case 'ended': endSessionUI(msg.reason); break;
      case 'screen': $('res').textContent = `${msg.width}×${msg.height}`; break;
      case 'chat': addChat(msg.text, false); break;
    }
  };
}
function sendMsg(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

if (token) connect(); else show('login');

// ---------- dashboard ----------
$('joinForm').addEventListener('submit', (e) => {
  e.preventDefault();
  $('joinStatus').textContent = 'Connecting…';
  sendMsg({ type: 'join', code: $('code').value.trim() });
});
function renderAgents(list) {
  const ul = $('agentList');
  ul.innerHTML = '';
  if (!list.length) { ul.innerHTML = '<li class="muted">No machines online.</li>'; return; }
  for (const a of list) {
    const li = document.createElement('li');
    const info = document.createElement('div');
    info.className = 'grow';
    info.innerHTML = `<strong></strong><div class="muted"></div>`;
    info.querySelector('strong').textContent = a.name;
    info.querySelector('div').textContent = `${a.os}${a.user ? ' · ' + a.user : ''}`;
    const btn = document.createElement('button');
    btn.textContent = a.busy ? 'In use' : 'Connect';
    btn.disabled = a.busy;
    btn.onclick = () => sendMsg({ type: 'join', agentId: a.id });
    li.append(info, btn);
    ul.append(li);
  }
}

// ---------- session ----------
const canvas = $('screen');
const ctx = canvas.getContext('2d');
let frames = 0, bytes = 0;

function startSessionUI(agent) {
  inSession = true;
  $('joinStatus').textContent = '';
  $('code').value = '';
  $('sessName').textContent = agent.name;
  $('sessInfo').textContent = agent.os + (agent.user ? ' · ' + agent.user : '');
  $('chatLog').innerHTML = '';
  show('session');
  canvas.focus();
  sendMsg({ type: 'settings', quality: +$('quality').value });
}
function endSessionUI(reason) {
  if (!inSession) return;
  inSession = false;
  show('dashboard');
  $('joinStatus').textContent = `Session ended: ${reason}`;
}
$('disconnect').onclick = () => { sendMsg({ type: 'leave' }); };

async function drawFrame(blob) {
  bytes += blob.size;
  try {
    const bmp = await createImageBitmap(blob);
    if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
      canvas.width = bmp.width; canvas.height = bmp.height;
    }
    ctx.drawImage(bmp, 0, 0);
    bmp.close();
    frames++;
  } finally {
    sendMsg({ type: 'ack' }); // flow control: agent sends next frame after ack
  }
}
setInterval(() => {
  $('fps').textContent = `${frames} fps`;
  $('kbps').textContent = `${Math.round(bytes / 1024)} KB/s`;
  frames = 0; bytes = 0;
}, 1000);

$('quality').onchange = () => sendMsg({ type: 'settings', quality: +$('quality').value });
$('fit').onchange = () => $('stage').classList.toggle('fit', $('fit').checked);
$('chatToggle').onclick = () => { $('chat').classList.toggle('hidden'); };

// ---------- input capture ----------
const viewOnly = () => $('viewOnly').checked || !inSession;
function pos(e) {
  const r = canvas.getBoundingClientRect();
  return {
    x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
    y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
  };
}
let lastMove = 0;
canvas.addEventListener('mousemove', (e) => {
  if (viewOnly()) return;
  const now = performance.now();
  if (now - lastMove < 16) return; // ~60Hz cap
  lastMove = now;
  sendMsg({ type: 'mouse', action: 'move', ...pos(e) });
});
canvas.addEventListener('mousedown', (e) => {
  canvas.focus();
  if (viewOnly()) return;
  e.preventDefault();
  sendMsg({ type: 'mouse', action: 'down', button: e.button, ...pos(e) });
});
canvas.addEventListener('mouseup', (e) => {
  if (viewOnly()) return;
  e.preventDefault();
  sendMsg({ type: 'mouse', action: 'up', button: e.button, ...pos(e) });
});
canvas.addEventListener('contextmenu', (e) => e.preventDefault());
canvas.addEventListener('wheel', (e) => {
  if (viewOnly()) return;
  e.preventDefault();
  sendMsg({ type: 'mouse', action: 'wheel', dx: Math.sign(e.deltaX), dy: -Math.sign(e.deltaY), ...pos(e) });
}, { passive: false });

function keyHandler(action) {
  return (e) => {
    if (viewOnly()) return;
    e.preventDefault();
    sendMsg({ type: 'key', action, key: e.key, code: e.code });
  };
}
canvas.addEventListener('keydown', keyHandler('down'));
canvas.addEventListener('keyup', keyHandler('up'));
// Release modifiers if the canvas loses focus mid-press
canvas.addEventListener('blur', () => {
  if (!inSession) return;
  for (const key of ['Control', 'Shift', 'Alt', 'Meta']) sendMsg({ type: 'key', action: 'up', key, code: '' });
});

// ---------- chat ----------
function addChat(text, mine) {
  const div = document.createElement('div');
  div.className = mine ? 'me' : '';
  const span = document.createElement('span');
  span.textContent = text;
  div.append(span);
  $('chatLog').append(div);
  $('chatLog').scrollTop = 1e9;
  if (!mine) $('chat').classList.remove('hidden');
}
$('chatForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const text = $('chatInput').value.trim();
  if (!text) return;
  sendMsg({ type: 'chat', text });
  addChat(text, true);
  $('chatInput').value = '';
});
