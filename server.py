#!/usr/bin/env python3
"""Hermes access sidecar — a universal HTTP shim for a Hermes install's own engines.

Exposes OpenAI-style HTTP endpoints that reuse THIS Hermes install's own STT/TTS
engines (and, opt-in, forward chat), so ANY external client — Cerap, EDITH, or your
own app — can do speech + chat against the user's own Hermes over one base URL:

  * ``POST /v1/audio/transcriptions``  (STT)   multipart ``file`` -> ``{"text": ...}``
  * ``POST /v1/audio/speech``          (TTS)   JSON ``{input,...}`` -> audio bytes
  * ``POST /v1/chat/completions``      (chat)  opt-in reverse-proxy (HERMES_CHAT_UPSTREAM)
  * ``GET  /v1/models``                (chat)  opt-in reverse-proxy
  * ``GET  /health``                   liveness (no auth)

Why a sidecar (not a core patch): Hermes's tunneled ``api_server`` is text+image
only and the STT/TTS engines are local-file functions with no HTTP exit. This
process imports those exact engines (``tools.transcription_tools.transcribe_audio``
and ``tools.tts_tool.text_to_speech_tool``) and serves them over HTTP, **without
modifying Hermes source** (update-safe). The user's own provider config
(``stt.provider`` / ``tts.provider`` in ``~/.hermes/config.yaml``) does the work,
so the client pays nothing and audio never leaves the box.

Auth: same ``API_SERVER_KEY`` bearer as the Hermes api_server (constant-time
check), so a client uses ONE token for chat (8642) and audio (this sidecar).

Run it with Hermes's own venv python from the hermes-agent dir — see run.sh.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

# Make the hermes-agent package (engines + their deps) importable.
HERMES_AGENT_DIR = os.environ.get("HERMES_AGENT_DIR") or str(Path.home() / ".hermes" / "hermes-agent")
if HERMES_AGENT_DIR not in sys.path:
    sys.path.insert(0, HERMES_AGENT_DIR)

import aiohttp  # noqa: E402  (after sys.path tweak)
from aiohttp import web  # noqa: E402

from tools.transcription_tools import transcribe_audio  # noqa: E402
from tools.tts_tool import text_to_speech_tool  # noqa: E402

log = logging.getLogger("hermes-audio-sidecar")

# Port. New HERMES_* name is preferred; the old CERAP_AUDIO_PORT is kept as a fallback so the ~1000
# already-deployed launchd/systemd units (which bake CERAP_AUDIO_PORT) keep binding the right port
# untouched after this rebrand.
PORT = int(os.environ.get("HERMES_AUDIO_PORT") or os.environ.get("CERAP_AUDIO_PORT") or "8643")
# Bind host. Default 0.0.0.0 = reachable over the network — devices, other boxes, and glasses reach
# this sidecar REMOTELY, so narrowing to loopback would break them (and every Cerap/EDITH user running
# their own). An operator MAY opt in to 127.0.0.1 ONLY when the sidecar is fronted by a same-box tunnel
# (cloudflared / `tailscale serve`), which then authenticates in front. No existing install sets this,
# so every current deployment stays 0.0.0.0 exactly as before.
BIND = os.environ.get("HERMES_AUDIO_BIND", "0.0.0.0").strip() or "0.0.0.0"
# Optional reverse-proxy. When set (e.g. "http://127.0.0.1:8642"), the allowlisted chat paths are
# forwarded to this Hermes api_server — so a single-base-URL client (e.g. EDITH over Tailscale, with no
# Cloudflare path-routing) can reach chat AND audio through this ONE port. OFF by default → an
# audio-only client (e.g. Cerap, which routes its chat straight to :8642) never hits the proxy.
CHAT_UPSTREAM = os.environ.get("HERMES_CHAT_UPSTREAM", "").strip().rstrip("/")
MAX_AUDIO_BYTES = int(os.environ.get("HERMES_AUDIO_MAX_BYTES") or os.environ.get("CERAP_AUDIO_MAX_BYTES") or str(32 * 1024 * 1024))
_AUDIO_CTYPE = {"mp3": "audio/mpeg", "opus": "audio/ogg", "ogg": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac"}


def _load_api_key() -> str:
    """Same bearer as the Hermes api_server. Resolution: env API_SERVER_KEY,
    then config.yaml platforms.api_server.extra.key, then a .bearer file."""
    key = os.environ.get("API_SERVER_KEY", "").strip()
    if key:
        return key
    try:
        import yaml  # hermes venv ships pyyaml

        cfg = yaml.safe_load((Path.home() / ".hermes" / "config.yaml").read_text()) or {}
        extra = (((cfg.get("platforms") or {}).get("api_server") or {}).get("extra") or {})
        if extra.get("key"):
            return str(extra["key"]).strip()
    except Exception:  # pragma: no cover - best effort
        pass
    # New dir first, then the old "cerap-audio-sidecar" dir — installs that wrote a key file before the
    # rebrand keep resolving (else every request 503s "no API_SERVER_KEY").
    for _name in ("hermes-audio-sidecar", "cerap-audio-sidecar"):
        bearer_file = Path(HERMES_AGENT_DIR).parent / _name / ".bearer"
        if bearer_file.is_file():
            return bearer_file.read_text().strip()
    return ""


API_KEY = _load_api_key()


def _err(message: str, status: int, code: str = "invalid_request_error") -> web.Response:
    return web.json_response({"error": {"message": message, "type": code}}, status=status)


# CORS — EDITH runs inside the Even app's WebView and fetches this cross-origin, so preflights must be
# answered HERE (not forwarded) and responses must carry the headers. The bearer travels in the
# Authorization HEADER (not a cookie), so "*" is safe — it never weakens the token gate; a caller
# still needs the key for any real request. Answering OPTIONS here also closes the old gap where an
# unauthenticated OPTIONS reached the proxy/upstream.
_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Hermes-Session-Key",
    "Access-Control-Max-Age": "600",
}


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=_CORS)  # preflight — never auth, never proxy
    resp = await handler(request)
    if not resp.prepared:  # a streamed proxy response sets CORS itself before prepare()
        for k, v in _CORS.items():
            resp.headers.setdefault(k, v)
    return resp


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    if not API_KEY:
        return _err("sidecar misconfigured: no API_SERVER_KEY", 503, "server_error")
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip(), API_KEY):
        return await handler(request)
    log.warning("auth failure: %s %s from %s", request.method, request.path, request.remote)  # detection
    return web.json_response(
        {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
        status=401,
    )


async def handle_health(request: web.Request) -> web.Response:
    # `ok` + `proxy` are the stable contract (the installer greps `"proxy":true` to tell an audio-only
    # sidecar from a chat-forwarding one). Only the vanity `service` name is rebranded — no consumer
    # keys off it (Cerap reaches the sidecar at a user-set base URL; the installer greps only `proxy`).
    return web.json_response({"ok": True, "service": "hermes-audio-sidecar", "proxy": bool(CHAT_UPSTREAM)})


async def handle_transcriptions(request: web.Request) -> web.Response:
    """OpenAI STT shape: multipart/form-data with `file` (+ optional `model`)."""
    if not request.content_type.startswith("multipart/"):
        return _err("Use multipart/form-data with a 'file' field", 400)
    tmp_path: str | None = None
    model: str | None = None
    try:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "file":
                suffix = Path(part.filename or "audio.wav").suffix or ".wav"
                fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                size = 0
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_AUDIO_BYTES:
                            return _err("audio too large", 413)
                        f.write(chunk)
            elif part.name == "model":
                model = (await part.text()).strip() or None

        if not tmp_path or os.path.getsize(tmp_path) == 0:
            return _err("missing or empty 'file'", 400)

        # transcribe_audio is sync + blocking (provider call / subprocess) — offload.
        result = await asyncio.to_thread(transcribe_audio, tmp_path, model)
        if not result.get("success"):
            return _err(result.get("error", "transcription failed"), 502, "server_error")
        return web.json_response({"text": result.get("transcript", ""), "provider": result.get("provider")})
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def handle_speech(request: web.Request) -> web.Response:
    """OpenAI TTS shape: JSON {model, input, voice?, response_format?} -> audio bytes."""
    try:
        body = await request.json()
    except Exception:
        return _err("invalid JSON body", 400)
    text = (body.get("input") or "").strip()
    if not text:
        return _err("missing 'input'", 400)

    out_path: str | None = None
    actual_path: str | None = None
    try:
        fd, out_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        # text_to_speech_tool is sync + blocking — offload; it returns a JSON string.
        res_raw = await asyncio.to_thread(text_to_speech_tool, text, out_path)
        res = json.loads(res_raw) if isinstance(res_raw, str) else res_raw
        actual_path = res.get("file_path") or out_path
        if not res.get("success") or not actual_path or not os.path.exists(actual_path):
            return _err(res.get("error", "tts failed"), 502, "server_error")
        data = Path(actual_path).read_bytes()
        ctype = _AUDIO_CTYPE.get(Path(actual_path).suffix.lstrip(".").lower(), "audio/mpeg")
        return web.Response(body=data, content_type=ctype)
    finally:
        for p in {out_path, actual_path}:
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ── optional chat/text reverse-proxy (opt-in via HERMES_CHAT_UPSTREAM) ──────────────────────────
# Forwards everything that isn't audio/health to the local Hermes api_server, so a single-URL client
# can reach chat + audio through this one port. Additive + opt-in: an audio-only client (e.g. Cerap)
# never registers this route.
_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te",
        "trailer", "upgrade", "proxy-authorization", "proxy-authenticate", "content-encoding"}
# Also drop the browser's Origin/Referer when forwarding UPSTREAM. Hermes' api_server 403s any request
# that carries an Origin (a CSRF / DNS-rebind defense), but EDITH runs inside the Even app's WebView,
# so every fetch carries one — which killed the whole chat path from the glasses with 403. The sidecar
# already terminates CORS itself (Access-Control-Allow-Origin: * above), so Hermes must see a clean
# server-to-server request without these browser headers.
_STRIP_UPSTREAM = _HOP | {"origin", "referer"}


async def handle_proxy(request: web.Request) -> web.StreamResponse:
    session: aiohttp.ClientSession = request.app["proxy_session"]
    target = CHAT_UPSTREAM + request.rel_url.raw_path_qs
    body = await request.read()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_UPSTREAM}
    try:
        async with session.request(
            request.method, target, headers=headers, data=body, allow_redirects=False,
        ) as up:
            # STREAM the upstream response chunk-by-chunk so SSE (token-by-token chat) survives and a
            # long generation keeps emitting bytes (no Cloudflare 524, no whole-body buffering in RAM).
            out = {k: v for k, v in up.headers.items() if k.lower() not in _HOP}  # drops content-length/encoding
            resp = web.StreamResponse(status=up.status, headers=out)
            for k, v in _CORS.items():
                resp.headers.setdefault(k, v)
            await resp.prepare(request)
            async for chunk in up.content.iter_any():  # auto-decompressed chunks
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except aiohttp.ClientError as e:
        return _err(f"upstream unreachable: {type(e).__name__}", 502, "server_error")


async def _open_session(app: web.Application) -> None:
    app["proxy_session"] = aiohttp.ClientSession(auto_decompress=True)


async def _close_session(app: web.Application) -> None:
    await app["proxy_session"].close()


# Only these paths are proxied to Hermes — an explicit allowlist, NOT a catch-all, so the sidecar
# never fronts the rest of the api_server (agent/jobs/cron/file routes = RCE-as-host-user). Extend
# deliberately if a client genuinely needs more.
PROXY_ALLOWLIST = (
    ("POST", "/v1/chat/completions"),
    ("GET", "/v1/models"),
)


def make_app() -> web.Application:
    app = web.Application(
        middlewares=[cors_middleware, auth_middleware],  # cors first: answers OPTIONS before auth
        client_max_size=MAX_AUDIO_BYTES + 1024 * 1024,
    )
    app.router.add_get("/health", handle_health)
    app.router.add_post("/v1/audio/transcriptions", handle_transcriptions)
    app.router.add_post("/v1/audio/speech", handle_speech)
    if CHAT_UPSTREAM:
        app.on_startup.append(_open_session)
        app.on_cleanup.append(_close_session)
        for method, path in PROXY_ALLOWLIST:  # scoped proxy — everything else 404s
            app.router.add_route(method, path, handle_proxy)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not API_KEY:
        log.warning("No API_SERVER_KEY resolved — every request will 503 until one is set.")
    if CHAT_UPSTREAM:
        log.info("chat reverse-proxy ON — non-audio requests forward to %s", CHAT_UPSTREAM)
    log.info("hermes-audio-sidecar listening on %s:%d (hermes-agent=%s)", BIND, PORT, HERMES_AGENT_DIR)
    web.run_app(make_app(), host=BIND, port=PORT, print=None)
