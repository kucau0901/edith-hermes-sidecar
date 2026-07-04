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

## Supported systems + restarts

| OS | Service | Survives a restart? |
|---|---|---|
| **macOS** | launchd LaunchAgent (`RunAtLoad` + `KeepAlive`) | Yes — restarts on crash, and comes back **at login** (same as Hermes itself; enable auto-login for a headless always-on Mac). |
| **Linux** | `systemd --user` (`Restart=always`) + `loginctl enable-linger` | Yes — restarts on crash **and survives reboot headless** (no login needed). |
| **Windows** | — | Not natively. Run Hermes + this installer **inside WSL** (it installs as a Linux service). |

Runs it by hand instead: `HERMES_AGENT_DIR=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python server.py`.

## Then, in EDITH

**Assistant → Hermes**: enter the sidecar's **URL** + your **API key**, pick **Hermes** as the chat
brain, and tap **Test**.

- Over **Tailscale** (recommended): `http://<this-box>:8643` — the tailnet authenticates the network
  layer, so your key isn't the *only* thing standing between the internet and your Hermes.
- Over the **internet**: put it behind **Cloudflare Access / mTLS** (identity + per-request logging +
  revocation), not a raw bearer-only tunnel. See **Security** below.

## What it exposes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | liveness — the only unauthenticated route |
| `POST` | `/v1/audio/transcriptions` | speech-to-text (reuses Hermes's engine) |
| `POST`/`GET` | `/v1/chat/completions`, `/v1/models` | forwarded to your Hermes (`:8642`) |

Bearer-auth (constant-time) on every route except `/health`. The proxy is a **strict allowlist** — it
forwards ONLY the two chat paths above, so the rest of the Hermes api_server (agent/jobs/cron/file
routes) is **not** reachable through the sidecar. No outbound calls to user-supplied URLs (no SSRF).
CORS preflights are answered locally. Set `HERMES_API_PORT` / `HERMES_AUDIO_PORT` / `HERMES_AGENT_DIR`
to override defaults.

## Security

Audited (see `SECURITY.md`): no remotely-exploitable bug — every route is token-gated. Do:
- **Front it with a network gate** (Tailscale, or Cloudflare Access / mTLS) rather than exposing a
  bearer-only service on a raw public tunnel. The bearer alone has no rate-limit or lockout.
- **Use a strong, ideally distinct key** — the sidecar shares your Hermes gateway key by default, so a
  leak is a whole-gateway leak; a separate rotatable token limits the blast radius.
- Keep the proxy **off** if you don't need single-URL chat (audio-only still works).

## Uninstall

```sh
# macOS
launchctl unload ~/Library/LaunchAgents/com.edith.hermes-audio.plist && rm ~/Library/LaunchAgents/com.edith.hermes-audio.plist
# Linux
systemctl --user disable --now hermes-audio-sidecar.service && rm ~/.config/systemd/user/hermes-audio-sidecar.service
```

MIT.
