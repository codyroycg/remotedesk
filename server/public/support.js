// Customer "Get support" page: download links + view-only browser screen sharing.
const $ = (id) => document.getElementById(id);
const show = (id, on = true) => $(id).classList.toggle('hidden', !on);

// ---------- downloads ----------
function detectPlatform() {
  const p = (navigator.userAgentData?.platform || navigator.platform || navigator.userAgent).toLowerCase();
  if (p.includes('win')) return 'windows';
  if (p.includes('mac')) return 'macos';
  if (p.includes('linux') && !/android/i.test(navigator.userAgent)) return 'linux';
  return null;
}
fetch('/api/downloads').then(r => r.json()).then(list => {
  if (!list.length) return;
  const mine = list.find(d => d.platform === detectPlatform());
  const primary = mine || list[0];
  $('downloadBtn').href = primary.url;
  $('downloadBtn').textContent = `Download for ${primary.label}`;
  const others = list.filter(d => d !== primary);
  if (others.length) {
    $('otherDownloads').append(' Other systems: ');
    others.forEach((d, i) => {
      const a = document.createElement('a');
      a.href = d.url; a.textContent = d.label;
      $('otherDownloads').append(a, i < others.length - 1 ? ', ' : '');
    });
  }
  show('winHelp', primary.platform === 'windows');
  show('downloadCard');
}).catch(() => {});

// ---------- browser screen share (view only) ----------
const canShare = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia;
if (!canShare) {
  $('shareBtn').disabled = true;
  $('shareNote').textContent = !window.isSecureContext
    ? 'Browser sharing needs a secure (https://) address. Use the download instead, or ask your technician for the https link.'
    : "This browser can't share its screen (most phones can't). Use a desktop browser or the download.";
}

let ws = null, stream = null, quality = 0.6, running = false, ackResolve = null, sentEnd = false;

function setStage(stage) {
  for (const id of ['codeBox', 'request', 'live', 'ended']) show(id, id === stage || (stage === 'request' && id === 'codeBox'));
}
function sendMsg(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

$('shareBtn').onclick = () => {
  show('choose', false);
  show('sharePanel');
  setStage('codeBox');
  $('code').textContent = '··· ···';
  $('status').textContent = 'Connecting…';
  $('chatLog').innerHTML = '';
  sentEnd = false;

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/agent`);
  ws.onopen = () => sendMsg({
    type: 'hello', viewOnly: true,
    name: ($('custName').value.trim() || 'Browser user'),
    os: `Browser · ${detectPlatform() || 'unknown OS'}`,
    user: $('custName').value.trim(),
  });
  ws.onclose = () => { if (!$('ended').classList.contains('hidden')) return; finish('Disconnected from the support server.'); };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case 'code':
        $('code').textContent = msg.code.slice(0, 3) + ' ' + msg.code.slice(3);
        $('status').textContent = 'Waiting for your technician to connect…';
        break;
      case 'join_request':
        $('reqTech').textContent = msg.tech;
        setStage('request');
        break;
      case 'tech_joined':
        $('liveTech').textContent = msg.tech;
        setStage('live');
        startStreaming();
        break;
      case 'tech_left':
        finish({ 'technician left': 'Your technician ended the session.',
                 'technician disconnected': 'Your technician disconnected.',
                 'remote user ended the session': 'You stopped sharing your screen.' }[msg.reason]
               || 'The session has ended.');
        break;
      case 'error': finish(msg.error); break;
      case 'ack': if (ackResolve) { ackResolve(); ackResolve = null; } break;
      case 'settings': quality = Math.max(0.1, Math.min(0.95, (msg.quality || 60) / 100)); break;
      case 'chat': addChat(msg.text, false); break;
    }
  };
};

$('denyBtn').onclick = () => { sendMsg({ type: 'consent', accepted: false }); setStage('codeBox'); };
$('allowBtn').onclick = async () => {
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 15 }, audio: false });
  } catch {
    sendMsg({ type: 'consent', accepted: false });
    $('status').textContent = 'Screen sharing was cancelled. Your technician can ask again.';
    setStage('codeBox');
    return;
  }
  // The user stopping the share from the browser's own toolbar ends the session
  stream.getVideoTracks()[0].addEventListener('ended', () => endByUser());
  sendMsg({ type: 'consent', accepted: true });
};
$('stopBtn').onclick = () => endByUser();
$('againBtn').onclick = () => { show('sharePanel', false); show('choose'); };

function endByUser() {
  if (!sentEnd) { sentEnd = true; sendMsg({ type: 'end' }); }
  finish('You stopped sharing your screen.');
}
function finish(message) {
  running = false;
  if (ackResolve) { ackResolve(); ackResolve = null; }
  if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  $('endedMsg').textContent = message;
  setStage('ended');
  if (ws) { const w = ws; ws = null; setTimeout(() => w.close(), 200); }
}

async function startStreaming() {
  if (!stream) return;
  const video = $('video');
  video.srcObject = stream;
  await video.play().catch(() => {});
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  const thumb = document.createElement('canvas'); // tiny copy for change detection
  thumb.width = 64; thumb.height = 36;
  const tctx = thumb.getContext('2d', { willReadFrequently: true });
  let lastThumb = null, lastSent = 0, lastSize = '';
  running = true;
  ackResolve = null;

  while (running) {
    const started = performance.now();
    const w0 = video.videoWidth, h0 = video.videoHeight;
    if (w0 && h0) {
      tctx.drawImage(video, 0, 0, thumb.width, thumb.height);
      const t = tctx.getImageData(0, 0, thumb.width, thumb.height).data;
      const changed = !lastThumb || t.some((v, i) => Math.abs(v - lastThumb[i]) > 2);
      if (changed || started - lastSent > 2000) {
        lastThumb = t;
        const scale = Math.min(1, 1920 / w0);
        canvas.width = Math.round(w0 * scale); canvas.height = Math.round(h0 * scale);
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', quality));
        if (!running || !ws) break;
        const size = `${canvas.width}x${canvas.height}`;
        if (size !== lastSize) { sendMsg({ type: 'screen', width: canvas.width, height: canvas.height }); lastSize = size; }
        const acked = new Promise(r => { ackResolve = r; });
        ws.send(blob);
        lastSent = performance.now();
        // flow control: wait for the technician to draw it (max 3s)
        await Promise.race([acked, new Promise(r => setTimeout(r, 3000))]);
      }
    }
    await new Promise(r => setTimeout(r, Math.max(0, 66 - (performance.now() - started))));
  }
}

// ---------- chat ----------
function addChat(text, mine) {
  const div = document.createElement('div');
  div.className = mine ? 'me' : '';
  const span = document.createElement('span');
  span.textContent = (mine ? '' : $('liveTech').textContent + ': ') + text;
  div.append(span);
  $('chatLog').append(div);
  $('chatLog').scrollTop = 1e9;
}
$('chatForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const text = $('chatInput').value.trim();
  if (!text) return;
  sendMsg({ type: 'chat', text });
  addChat(text, true);
  $('chatInput').value = '';
});
