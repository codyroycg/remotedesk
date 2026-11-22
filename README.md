# RemoteDesk

Self-hosted remote support, in the spirit of ScreenConnect. Your customer
opens a web page, gets a 6-digit code, and reads it to you. You type it in,
they click **Allow**, and you can see and control their screen.

## Quick start (2 minutes)

**1. Start the server.** You only need [Node.js](https://nodejs.org) 18+.

- Windows: double-click `start.bat`
- macOS / Linux: `./start.sh`

It prints everything you need:

```
  Technician console:  http://localhost:8080/console
  Password:            C8WUzGO2vFYI
  Customer page:       http://localhost:8080/
                       http://192.168.1.20:8080/  (your network)
```

The password is generated on first run and saved in `server/data/config.json`
(change it there and restart if you like).

**2. Open the technician console** at `/console` and sign in.

**3. Send the customer to the customer page** (the console has a Copy button).
They pick one of two options:

| Option | Customer needs | You can |
|---|---|---|
| **Download the support app** | Double-click the file | See **and control** their screen |
| **Share from the browser** | Nothing (Chrome, Edge, Firefox, Safari on a computer) | See their screen (view only) |

Either way they get a code. Enter it in the console and they click **Allow**.

> Browser sharing only works over **https://** (or on `localhost`), because browsers
> require a secure page for screen capture. For use over the internet, set up HTTPS (below).

## Build the agent app

The "Download the support app" button appears once a built app is in
`server/downloads/`. Each download automatically has your server's address
embedded, so customers have nothing to type.

**Easiest: let GitHub build all three** (Windows, macOS, Linux). Push this
repo to GitHub, open **Actions → Build support app → Run workflow**, then
download the artifacts and drop the files into `server/downloads/`.

**Or build for the OS you're on** (Python 3.9+ with Tk):

```bash
python agent/build.py
```

## Put it on the internet with HTTPS

You need a server with Docker, and a domain name pointing at it (ports 80 and 443 open):

```bash
DOMAIN=support.example.com docker compose up -d
```

```bash
docker compose logs remotedesk
```

Caddy gets a Let's Encrypt certificate automatically. The logs show the technician
password. Built apps in `server/downloads/` are served automatically.

Without Docker: run `./start.sh` behind any HTTPS reverse proxy (Caddy, nginx) and
set `"publicUrl": "https://support.example.com"` in `server/data/config.json`.

## Unattended access (your own machines)

For computers you manage, run the agent in unattended mode so it shows up in
the console's **Unattended machines** list, with no code or click needed. The key is
`agentKey` in `server/data/config.json`:

```bash
pip install -r agent/requirements.txt
```

```bash
python agent/agent.py --server support.example.com --unattended --key YOUR_AGENT_KEY
```

## Settings

`server/data/config.json` (environment variables override it):

| Setting | Env var | Default |
|---|---|---|
| `techPassword` | `TECH_PASSWORD` | random, generated on first run |
| `agentKey` | `AGENT_KEY` | random, generated on first run |
| `port` | `PORT` | 8080 |
| `publicUrl` | `PUBLIC_URL` | auto-detected from the browser's address |

Agent options (all optional): `--server`, `--name`, `--monitor N` (0 = all
screens), `--fps`, `--quality`, `--cli` (terminal instead of window),
`--auto-accept` (skip the Allow prompt; testing only).

## Good to know

- **Windows SmartScreen** warns about unsigned apps ("Windows protected your PC" → More info → Run anyway).
  The customer page explains this. A code-signing certificate removes the warning.
- **macOS** asks the customer to grant Screen Recording and Accessibility permissions the first time,
  and blocks unsigned downloaded apps (right-click → Open). The macOS app also downloads without its
  "executable" permission, so for Mac customers browser sharing is usually easier until the app is signed and notarized.
- **Linux**: X11 only; Wayland sessions can't be captured or controlled by the app (browser sharing still works).
- **Windows**: the app can't control admin (UAC) prompts or the login screen. That needs a Windows service.

## How it works

```
 Technician browser  <--wss-->  Relay server (Node)  <--wss-->  Support app (Python) or customer's browser
   /console                     auth, codes, relay              screen capture + input
```

Both sides connect *out* to the server, so customers need no firewall or router changes.
Screens are sent as JPEG frames only when something changes, paced by the viewer's acknowledgements.

| Path | What |
|---|---|
| `start.sh`, `start.bat` | one-command server start |
| `server/server.js` | relay server, login, codes, downloads |
| `server/public/support.*` | customer page (+ browser sharing) |
| `server/public/console.*` | technician console |
| `agent/agent.py` | support app (window UI, capture, input) |
| `agent/build.py` | packages the support app into a single executable |
| `docker-compose.yml` | production deploy with automatic HTTPS |
