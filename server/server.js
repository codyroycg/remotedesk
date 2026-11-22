// RemoteDesk relay server
// - Serves the customer "Get support" page (/) and technician console (/console)
// - Serves agent downloads with this server's address baked in (/download/...)
// - Agents (remote machines or browser screen-shares) connect on /agent
// - Technicians connect on /tech (after logging in)
// - Relays screen frames (agent -> tech) and input/chat (tech -> agent)

const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { WebSocketServer } = require('ws');

// ---------- configuration ----------
// Settings come from env vars, else data/config.json, which is created with
// random secrets on first run so the server works with zero setup.
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');
const CONFIG_FILE = path.join(DATA_DIR, 'config.json');
function loadConfig() {
  let cfg = {};
  try { cfg = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')); } catch {}
  let changed = false;
  if (!cfg.techPassword) { cfg.techPassword = crypto.randomBytes(9).toString('base64url'); changed = true; }
  if (!cfg.agentKey) { cfg.agentKey = crypto.randomBytes(24).toString('base64url'); changed = true; }
  if (changed) {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(CONFIG_FILE, JSON.stringify(cfg, null, 2) + '\n', { mode: 0o600 });
  }
  return cfg;
}
const cfg = loadConfig();
const PORT = parseInt(process.env.PORT || cfg.port || '8080', 10);
const TECH_PASSWORD = process.env.TECH_PASSWORD || cfg.techPassword;
const AGENT_KEY = process.env.AGENT_KEY || cfg.agentKey; // required for unattended agents
const PUBLIC_URL = (process.env.PUBLIC_URL || cfg.publicUrl || '').replace(/\/$/, ''); // e.g. https://support.example.com
const CODE_TTL_MS = 10 * 60 * 1000; // session codes expire if nobody joins

const PUBLIC_DIR = path.join(__dirname, 'public');
const DOWNLOAD_DIR = path.join(__dirname, 'downloads');
// Platform -> file name produced by agent/build.py
const DOWNLOADS = {
  windows: { file: 'RemoteDesk-windows.exe', label: 'Windows' },
  linux: { file: 'RemoteDesk-linux', label: 'Linux' },
  macos: { file: 'RemoteDesk-macos', label: 'macOS' },
};
const TRAILER_MAGIC = Buffer.from('RDCFG001');

function availableDownloads() {
  return Object.entries(DOWNLOADS)
    .filter(([, d]) => fs.existsSync(path.join(DOWNLOAD_DIR, d.file)))
    .map(([platform, d]) => ({ platform, label: d.label, url: `/download/${platform}` }));
}
// The address agents should connect to, as seen by whoever made this request.
function agentUrlFor(req) {
  if (PUBLIC_URL) return PUBLIC_URL.replace(/^http/, 'ws') + '/agent';
  const https = req.headers['x-forwarded-proto'] === 'https' || req.socket.encrypted;
  return `${https ? 'wss' : 'ws'}://${req.headers.host}/agent`;
}
const techTokens = new Map(); // token -> { user, expires }
const agents = new Map();     // agentId -> agent record
const codes = new Map();      // code -> agentId

// ---------- helpers ----------
function safeEqual(a, b) {
  const ba = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  return ba.length === bb.length && crypto.timingSafeEqual(ba, bb);
}
function send(ws, obj) {
  if (ws && ws.readyState === ws.OPEN) ws.send(JSON.stringify(obj));
}
function newCode() {
  let code;
  do { code = String(crypto.randomInt(100000, 1000000)); } while (codes.has(code));
  return code;
}
function validToken(token) {
  const t = techTokens.get(token);
  if (!t) return null;
  if (t.expires < Date.now()) { techTokens.delete(token); return null; }
  return t;
}
function agentSummary(a) {
  return { id: a.id, name: a.name, os: a.os, user: a.user, unattended: a.unattended, viewOnly: a.viewOnly, busy: !!a.tech };
}
function broadcastAgentList() {
  const list = [...agents.values()].filter(a => a.unattended).map(agentSummary);
  for (const ws of techSockets) send(ws, { type: 'agents', agents: list });
}
function endSession(agent, reason) {
  if (!agent || !agent.tech) return;
  const tech = agent.tech;
  agent.tech = null;
  tech.agent = null;
  send(tech, { type: 'ended', reason });
  send(agent.ws, { type: 'tech_left', reason });
  console.log(`[session] ended agent=${agent.name} reason=${reason}`);
  broadcastAgentList();
}

// ---------- HTTP ----------
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
const loginAttempts = new Map(); // ip -> { count, reset }

const server = http.createServer((req, res) => {
  if (req.method === 'POST' && req.url === '/api/login') {
    const ip = req.socket.remoteAddress;
    const rec = loginAttempts.get(ip) || { count: 0, reset: Date.now() + 60_000 };
    if (rec.reset < Date.now()) { rec.count = 0; rec.reset = Date.now() + 60_000; }
    if (++rec.count > 10) { loginAttempts.set(ip, rec); res.writeHead(429); return res.end(); }
    loginAttempts.set(ip, rec);

    let body = '';
    req.on('data', c => { body += c; if (body.length > 10_000) req.destroy(); });
    req.on('end', () => {
      let data = {};
      try { data = JSON.parse(body); } catch {}
      if (!data.password || !safeEqual(data.password, TECH_PASSWORD)) {
        res.writeHead(401, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: 'Invalid password' }));
      }
      const token = crypto.randomBytes(24).toString('hex');
      const user = String(data.name || 'Technician').slice(0, 40);
      techTokens.set(token, { user, expires: Date.now() + 12 * 3600 * 1000 });
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ token, user }));
    });
    return;
  }

  const urlPath = req.url.split('?')[0];

  if (req.method === 'GET' && urlPath === '/api/downloads') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify(availableDownloads()));
  }

  // Agent download: the prebuilt executable + a trailer holding this server's
  // address, so the customer just double-clicks it with nothing to configure.
  const dl = urlPath.match(/^\/download\/(windows|linux|macos)$/);
  if (req.method === 'GET' && dl) {
    const d = DOWNLOADS[dl[1]];
    const file = path.join(DOWNLOAD_DIR, d.file);
    if (!fs.existsSync(file)) { res.writeHead(404); return res.end('This download has not been built yet.'); }
    const json = Buffer.from(JSON.stringify({ server: agentUrlFor(req) }));
    const len = Buffer.alloc(4); len.writeUInt32LE(json.length);
    const trailer = Buffer.concat([json, len, TRAILER_MAGIC]);
    const size = fs.statSync(file).size + trailer.length;
    res.writeHead(200, {
      'Content-Type': 'application/octet-stream',
      'Content-Length': size,
      'Content-Disposition': `attachment; filename="${d.file.replace('RemoteDesk-', 'RemoteDesk-Support-')}"`,
      'Cache-Control': 'no-store',
    });
    const stream = fs.createReadStream(file);
    stream.pipe(res, { end: false });
    stream.on('end', () => res.end(trailer));
    stream.on('error', () => res.destroy());
    return;
  }

  // static files
  let p = decodeURIComponent(urlPath);
  if (p === '/') p = '/support.html';
  if (p === '/console') p = '/console.html';
  const file = path.normalize(path.join(PUBLIC_DIR, p));
  if (!file.startsWith(PUBLIC_DIR)) { res.writeHead(403); return res.end(); }
  fs.readFile(file, (err, buf) => {
    if (err) { res.writeHead(404); return res.end('Not found'); }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(buf);
  });
});

// ---------- WebSockets ----------
const agentWss = new WebSocketServer({ noServer: true, maxPayload: 16 * 1024 * 1024 });
const techWss = new WebSocketServer({ noServer: true, maxPayload: 1024 * 1024 });
const techSockets = new Set();

server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname === '/agent') {
    agentWss.handleUpgrade(req, socket, head, ws => agentWss.emit('connection', ws, req));
  } else if (url.pathname === '/tech') {
    const t = validToken(url.searchParams.get('token'));
    if (!t) { socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n'); return socket.destroy(); }
    techWss.handleUpgrade(req, socket, head, ws => { ws.user = t.user; techWss.emit('connection', ws, req); });
  } else {
    socket.destroy();
  }
});

// Agent connections
agentWss.on('connection', (ws) => {
  let agent = null;

  ws.on('message', (data, isBinary) => {
    // Binary = JPEG frame, forward straight to the technician
    if (isBinary) {
      if (agent && agent.tech) agent.tech.send(data, { binary: true });
      return;
    }
    let msg;
    try { msg = JSON.parse(data); } catch { return; }

    if (msg.type === 'hello' && !agent) {
      const unattended = !!msg.unattended;
      if (unattended && (!AGENT_KEY || !safeEqual(msg.key || '', AGENT_KEY))) {
        send(ws, { type: 'error', error: 'Unattended mode requires a valid AGENT_KEY' });
        return ws.close();
      }
      const id = unattended && msg.id ? String(msg.id).slice(0, 64) : crypto.randomUUID();
      const old = agents.get(id);
      if (old) { endSession(old, 'agent reconnected'); old.ws.close(); }
      agent = {
        id, ws, unattended,
        viewOnly: !!msg.viewOnly, // browser screen-share: no remote control
        name: String(msg.name || 'Unknown').slice(0, 60),
        os: String(msg.os || '').slice(0, 60),
        user: String(msg.user || '').slice(0, 60),
        tech: null, code: null, codeExpires: 0,
      };
      agents.set(id, agent);
      if (!unattended) {
        agent.code = newCode();
        agent.codeExpires = Date.now() + CODE_TTL_MS;
        codes.set(agent.code, id);
        send(ws, { type: 'code', code: agent.code, expiresInSec: CODE_TTL_MS / 1000 });
      } else {
        send(ws, { type: 'registered', id });
      }
      console.log(`[agent] connected ${agent.name} (${agent.os}) unattended=${unattended}`);
      broadcastAgentList();
      return;
    }
    if (!agent) return;

    switch (msg.type) {
      case 'consent': {
        const tech = agent.pendingTech;
        agent.pendingTech = null;
        if (!tech || tech.readyState !== tech.OPEN) return;
        if (msg.accepted) startSession(agent, tech);
        else send(tech, { type: 'join_error', error: 'The remote user declined the request.' });
        break;
      }
      case 'screen':  // screen size / metadata
      case 'chat':
      case 'clipboard':
        if (agent.tech) send(agent.tech, msg);
        break;
      case 'end':
        endSession(agent, 'remote user ended the session');
        break;
    }
  });

  ws.on('close', () => {
    if (!agent) return;
    endSession(agent, 'remote computer disconnected');
    if (agent.pendingTech) send(agent.pendingTech, { type: 'join_error', error: 'Remote computer disconnected.' });
    if (agent.code) codes.delete(agent.code);
    if (agents.get(agent.id) === agent) agents.delete(agent.id);
    console.log(`[agent] disconnected ${agent.name}`);
    broadcastAgentList();
  });
});

function startSession(agent, tech) {
  if (agent.code) { codes.delete(agent.code); agent.code = null; } // one-time code
  agent.tech = tech;
  tech.agent = agent;
  send(tech, { type: 'joined', agent: agentSummary(agent) });
  send(agent.ws, { type: 'tech_joined', tech: tech.user });
  console.log(`[session] started tech=${tech.user} agent=${agent.name}`);
  broadcastAgentList();
}

// Technician connections
techWss.on('connection', (ws) => {
  techSockets.add(ws);
  broadcastAgentList();

  ws.on('message', (data, isBinary) => {
    if (isBinary) return;
    let msg;
    try { msg = JSON.parse(data); } catch { return; }

    switch (msg.type) {
      case 'join': {
        if (ws.agent) endSession(ws.agent, 'technician switched sessions');
        let agent;
        if (msg.code) {
          const id = codes.get(String(msg.code).trim());
          agent = id && agents.get(id);
          if (!agent || agent.codeExpires < Date.now()) {
            return send(ws, { type: 'join_error', error: 'Invalid or expired code.' });
          }
        } else if (msg.agentId) {
          agent = agents.get(msg.agentId);
          if (!agent || !agent.unattended) return send(ws, { type: 'join_error', error: 'Machine not found.' });
        }
        if (!agent) return send(ws, { type: 'join_error', error: 'Nothing to join.' });
        if (agent.tech || agent.pendingTech) return send(ws, { type: 'join_error', error: 'Machine is already in a session.' });

        if (agent.unattended) {
          startSession(agent, ws);
        } else {
          agent.pendingTech = ws;
          send(ws, { type: 'waiting', message: 'Waiting for the remote user to accept…' });
          send(agent.ws, { type: 'join_request', tech: ws.user });
        }
        break;
      }
      case 'leave':
        if (ws.agent) endSession(ws.agent, 'technician left');
        break;
      // Input + interaction messages, forwarded to the paired agent only
      case 'mouse':
      case 'key':
      case 'chat':
      case 'ack':
      case 'settings':
      case 'clipboard':
        if (ws.agent) send(ws.agent.ws, msg);
        break;
    }
  });

  ws.on('close', () => {
    techSockets.delete(ws);
    if (ws.agent) endSession(ws.agent, 'technician disconnected');
    for (const a of agents.values()) if (a.pendingTech === ws) a.pendingTech = null;
  });
});

// Expire stale codes
setInterval(() => {
  for (const [code, id] of codes) {
    const a = agents.get(id);
    if (!a || a.codeExpires < Date.now()) {
      codes.delete(code);
      if (a) { send(a.ws, { type: 'error', error: 'Session code expired' }); a.ws.close(); }
    }
  }
}, 30_000);

server.listen(PORT, () => {
  const lan = Object.values(os.networkInterfaces()).flat()
    .filter(i => i && i.family === 'IPv4' && !i.internal).map(i => i.address);
  const base = PUBLIC_URL || `http://localhost:${PORT}`;
  const line = '─'.repeat(60);
  console.log(`\n${line}\n  RemoteDesk is running\n${line}`);
  console.log(`  Technician console:  ${base}/console`);
  console.log(`  Password:            ${process.env.TECH_PASSWORD ? '(from TECH_PASSWORD env var)' : TECH_PASSWORD}`);
  console.log(`  Customer page:       ${base}/`);
  if (!PUBLIC_URL) for (const ip of lan) console.log(`                       http://${ip}:${PORT}/  (your network)`);
  const dls = availableDownloads();
  console.log(`  Agent downloads:     ${dls.length ? dls.map(d => d.label).join(', ') : 'none built yet (see README: "Build the agent app")'}`);
  console.log(`  Settings file:       ${CONFIG_FILE}`);
  console.log(`${line}\n`);
});
