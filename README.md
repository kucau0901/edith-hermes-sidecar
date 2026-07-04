# EDITH · Hermes audio sidecar

A tiny HTTP service that lets **EDITH** — the Home Assistant + Hermes plugin for Even Realities **G2
glasses** — do **voice** on your **own** [Hermes](https://github.com/) agent.

Stock Hermes has no HTTP audio API, so an outside app can't reach its speech-to-text. This sidecar
reuses Hermes's *own* STT engine and exposes it over HTTP, and forwards everything else to Hermes — so
EDITH reaches **chat + voice through one URL**. It runs on *your* machine; **audio never leaves it**,
and it uses your own provider, so it costs nothing to run.

## Install — one line

On the machine that runs your Hermes:

```sh
curl -fsSL https://raw.githubusercontent.com/kucau0901/edith-hermes-sidecar/main/install.sh | bash
```

It finds your Hermes venv, installs one dependency (`aiohttp`), drops the sidecar in, runs it under
**launchd** (macOS) or **systemd** (Linux), reuses your existing Hermes API key, and turns on
chat-forwarding. Re-running it is safe.

## Requirements

- **Hermes with voice support** — `pip install 'hermes-agent[voice]'` (your STT provider does the work).
- Your Hermes **API key** (the `api_server` bearer). The installer reuses it from `~/.hermes/config.yaml`;
  if it can't find one, it asks.

## Then, in EDITH

**Assistant → Hermes**: enter the sidecar's **URL** + your **API key**, pick **Hermes** as the chat
brain, and tap **Test**.

- Over **Tailscale** (simplest): `http://<this-box>:8643`
- Over the **internet**: expose port `8643` with your tunnel/reverse-proxy and use that HTTPS URL.

## What it exposes

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/audio/transcriptions` | speech-to-text (reuses Hermes's engine) |
| `*` | anything else | forwarded to your Hermes (`:8642`) — chat, models, … |
| `GET` | `/health` | liveness (the only unauthenticated route) |

Bearer-auth on every route except `/health` (same key as your Hermes). It makes **no outbound calls
to user-supplied URLs** (no SSRF surface). Set `HERMES_API_PORT` / `HERMES_AUDIO_PORT` /
`HERMES_AGENT_DIR` to override defaults.

## Uninstall

```sh
# macOS
launchctl unload ~/Library/LaunchAgents/com.edith.hermes-audio.plist && rm ~/Library/LaunchAgents/com.edith.hermes-audio.plist
# Linux
systemctl --user disable --now hermes-audio-sidecar.service && rm ~/.config/systemd/user/hermes-audio-sidecar.service
```

MIT.
