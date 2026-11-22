# RemoteDesk

A self-hosted remote support tool in the spirit of ScreenConnect: a technician
opens a web console, the customer runs a small agent and reads out a 6-digit
code, and the technician can see and control their screen after they accept.

```
 Technician browser  <--wss-->  Relay server (Node)  <--wss-->  Host agent (Python)
   console UI                   auth, codes, relay              screen capture + input
```

Both sides connect *outbound* to the relay, so the customer needs no port
forwarding or firewall changes.

## Features

- **Attended sessions**: one-time 6-digit codes that expire after 10 minutes.
- **Consent prompt** on the host before anyone can connect (denied automatically after about 55 seconds).
- **Always-on-top session banner** on the host with chat and an **End session** button.
- **Unattended access** for machines you administer (requires a shared `AGENT_KEY`).
- Remote screen in the browser with mouse, scroll wheel and keyboard control, a quality setting, fit-to-window and view-only mode.
- Chat between technician and end user.
- Change detection plus ack-based flow control: frames are only sent when the screen changes, and never faster than the client can draw them.
- Password login for technicians, with rate limiting and constant-time comparison.

## Run it

### 1. Relay server (Node 18+)

```bash
cd server && npm install
```

```bash
TECH_PASSWORD='choose-a-strong-one' AGENT_KEY='long-random-secret' npm start
```

Open http://localhost:8080 and sign in.

### 2. Agent, on the computer being supported (Python 3.9+)

```bash
cd agent && pip install -r requirements.txt
```

```bash
python agent.py --server ws://YOUR-SERVER:8080/agent
```

It prints a code. Enter it in the console; the user clicks **Allow**.

Unattended (your own machines only):

```bash
python agent.py --server wss://support.example.com/agent --unattended --key 'long-random-secret'
```

Other options: `--name`, `--monitor N` (0 = all screens), `--fps`, `--quality`,
`--max-width`, `--auto-accept` (skips the prompt; for testing).

## Deploying for real

- **Use TLS.** Put the server behind Caddy or nginx with a certificate and use
  `wss://` URLs. Without TLS, screen contents and the password travel in clear text.
- Change `TECH_PASSWORD` and `AGENT_KEY` to long random values.
- Package the agent as a single executable for customers, for example
  `pyinstaller --onefile --add-data "ui.py:." agent.py`, with `--server` baked in as the default.
- The agent's GUI needs Tk (`python3-tk` on Debian/Ubuntu). Without it, consent is asked in the terminal.

## Platform notes

| | Screen capture | Input |
|---|---|---|
| Windows | yes | yes (cannot control UAC prompts or the login screen unless it runs as a service) |
| macOS | needs the Screen Recording permission | needs the Accessibility permission |
| Linux X11 | yes | yes |
| Linux Wayland | no (needs the PipeWire portal) | no |

## Roadmap ideas

- WebRTC/H.264 video for much lower bandwidth (aiortc on the agent side)
- File transfer, clipboard sync, remote terminal
- Multiple technician accounts with roles, MFA and a session audit log/recording
- A Windows service build for unattended access, so it works at the login screen and with UAC
- Signed installers and auto-update
