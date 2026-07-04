# Security assessment — Hermes audio sidecar

Red-team audit of `server.py` + `install.sh` (parallel adversarial code review, independent
verification of every finding, and safe non-destructive probes against a live `:8643`).

## Bottom line

**No remotely-exploitable vulnerability.** Every route except `/health` is gated by a constant-time
bearer check, verified live (every unauthenticated probe — proxy paths, a path-traversal attempt,
audio, chat — returned `401`). This is safe to run **behind a network gate (Tailscale / Cloudflare
Access) with a strong key**. The findings below are hardening / posture, not live exploits.

### Verified NOT vulnerable
- **Auth**: constant-time `hmac.compare_digest`, fails closed on a missing key, `/health` is the only
  exemption. No bypass, no `OPTIONS`-swallows-`POST`.
- **SSRF / open proxy**: the upstream (`CHAT_UPSTREAM`) is a fixed process env, not attacker-steerable.
- **Command injection via the STT `model` field**: refuted — Hermes `shlex.quote`s it and runs
  list-mode `subprocess.run(shlex.split(...))` (or a shell-quote-aware renderer). No injection.
- **Path traversal**: the upload filename becomes only a `Path(...).suffix` (no separators); the proxy
  concatenates the path onto a fixed host and makes an HTTP call — it never opens a local file.
- **Buffer overflow**: `server.py` is memory-safe Python. (The real memory-safety surface is the
  native audio decoder — see residuals.)
- **Request smuggling / response splitting**: refuted against the actual aiohttp version.

## Fixes applied (this pass) — deployed + live-verified

1. **Proxy is now a strict allowlist** — only `POST /v1/chat/completions` + `GET /v1/models` reach
   Hermes; the old `/{tail:.*}` catch-all fronted the *entire* api_server (agent/jobs/cron/file routes
   = RCE-as-host-user). Verified: `/api/jobs` and `/api/cron/fire` now `404` **even with a valid token**.
2. **`OPTIONS` handled locally + CORS** — an unauthenticated `OPTIONS` used to be forwarded to `:8642`;
   now it's answered in the sidecar (`204`). This also fixes a latent functional bug: EDITH's WebView
   `fetch` is cross-origin and needs CORS (`*` is safe here — the bearer is a header, not a cookie).
3. **Streaming proxy** — was `await up.read()` (full buffer), which broke SSE and re-introduced the
   Cloudflare `524`; now streams the upstream chunk-by-chunk.
4. **Auth-failure logging** — the `401` path now logs method/path/remote for detection.
5. **Installer hygiene** — log moved off world-readable `/tmp` to `~/.hermes/logs` (0700 dir), `aiohttp`
   pinned (`==3.13.4`), `.bearer` parent dir tightened to `0700`.
6. **README posture** — Tailscale / Cloudflare Access are now the recommended paths, not a raw tunnel.

## Residual — recommended, not yet done

| # | Item | Why | Effort |
|---|---|---|---|
| R1 | **Network gate before any public exposure** — Tailscale, or Cloudflare Access / mTLS | A bearer alone has no rate-limit, lockout, or identity; the gate authenticates before aiohttp | deploy config |
| R2 | **Distinct, rotatable sidecar token** (not the shared Hermes gateway key) | Today a sidecar-side leak = whole-gateway leak | Hermes config |
| R3 | **Rate-limit + lockout on auth failures** | Online brute-force is currently unthrottled (mitigated only by a strong key) | code |
| R4 | **Pin the install one-liner to a release tag/commit + SHA-256** | `curl … /main/…` is TOFU off a mutable branch; whoever controls `main` at fetch time gets boot-persistent RCE on new installs | cut a tagged release |
| R5 | **Fuzz the native audio decoder offline** (never on the live box) | Untrusted audio → ffmpeg/whisper is the real memory-corruption surface; keep it behind the same auth gate | testing |

## Live probe results (non-destructive, run against `:8643`)

| Probe (no token unless noted) | Result |
|---|---|
| `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/audio/transcriptions`, `GET /../../etc/passwd` | `401` |
| `GET /health` | `200` (open by design) |
| `OPTIONS /v1/models` | `204` + `Access-Control-Allow-Origin: *` |
| `GET /api/jobs` / `/api/cron/fire` **with valid token** | `404` (allowlist) |
| `GET /v1/models`, `POST /v1/chat/completions` **with valid token** | `200` (chat still works) |
