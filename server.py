#!/usr/bin/env python3
"""Cerap audio sidecar for Hermes.

Exposes OpenAI-style HTTP audio endpoints that reuse THIS Hermes install's own
STT/TTS engines, so an external app (Cerap) can do speech-to-text and
text-to-speech on the user's own Hermes:

  * ``POST /v1/audio/transcriptions``  (STT)  multipart ``file`` -> ``{"text": ...}``
  * ``POST /v1/audio/speech``          (TTS)  JSON ``{input,...}`` -> audio bytes
  * ``GET  /health``                   liveness (no auth)

Why a sidecar (not a core patch): Hermes's tunneled ``api_server`` is text+image
only and the STT/TTS engines are local-file functions with no HTTP exit. This
process imports those exact engines (``tools.transcription_tools.transcribe_audio``
and ``tools.tts_tool.text_to_speech_tool``) and serves them over HTTP, **without
modifying Hermes source** (update-safe). The user's own provider config
(``stt.provider`` / ``tts.provider`` in ``~/.hermes/config.yaml``) does the work,
so Cerap pays nothing and audio never leaves the box.

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

log = logging.getLogger("cerap-audio-sidecar")

PORT = int(os.environ.get("CERAP_AUDIO_PORT", "8643"))
# Optional reverse-proxy. When set (e.g. "http://127.0.0.1:8642"), any request that is NOT /health or
# /v1/audio/* is forwarded to this Hermes api_server — so a single-base-URL client (EDITH over
# Tailscale, with no Cloudflare path-routing) can reach chat AND audio through this ONE port. OFF by
# default → behaviour is byte-for-byte the audio-only sidecar Cerap already runs (Cerap never hits
# the proxy: Cloudflare routes its chat straight to :8642).
CHAT_UPSTREAM = os.environ.get("HERMES_CHAT_UPSTREAM", "").strip().rstrip("/")
MAX_AUDIO_BYTES = int(os.environ.get("CERAP_AUDIO_MAX_BYTES", str(32 * 1024 * 1024)))
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
    bearer_file = Path(HERMES_AGENT_DIR).parent / "cerap-audio-sidecar" / ".bearer"
    if bearer_file.is_file():
        return bearer_file.read_text().strip()
    return ""


API_KEY = _load_api_key()


def _err(message: str, status: int, code: str = "invalid_request_error") -> web.Response:
    return web.json_response({"error": {"message": message, "type": code}}, status=status)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.method == "OPTIONS" or request.path == "/health":
        return await handler(request)
    if not API_KEY:
        return _err("sidecar misconfigured: no API_SERVER_KEY", 503, "server_error")
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip(), API_KEY):
        return await handler(request)
    return web.json_response(
        {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
        status=401,
    )


async def handle_health(request: web.Request) -> web.Response:
    # `proxy` lets the EDITH installer tell an audio-only sidecar (Cerap's) from a chat-forwarding one.
    return web.json_response({"ok": True, "service": "cerap-audio-sidecar", "proxy": bool(CHAT_UPSTREAM)})


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
# can reach chat + audio through this one port. Additive + opt-in: Cerap never registers this route.
_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te",
        "trailer", "upgrade", "proxy-authorization", "proxy-authenticate", "content-encoding"}


async def handle_proxy(request: web.Request) -> web.StreamResponse:
    session: aiohttp.ClientSession = request.app["proxy_session"]
    target = CHAT_UPSTREAM + request.rel_url.raw_path_qs
    body = await request.read()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    try:
        async with session.request(
            request.method, target, headers=headers, data=body, allow_redirects=False,
        ) as up:
            data = await up.read()  # session auto-decompresses; we strip content-encoding via _HOP
            out = {k: v for k, v in up.headers.items() if k.lower() not in _HOP}
            return web.Response(status=up.status, body=data, headers=out)
    except aiohttp.ClientError as e:
        return _err(f"upstream unreachable: {type(e).__name__}", 502, "server_error")


async def _open_session(app: web.Application) -> None:
    app["proxy_session"] = aiohttp.ClientSession(auto_decompress=True)


async def _close_session(app: web.Application) -> None:
    await app["proxy_session"].close()


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=MAX_AUDIO_BYTES + 1024 * 1024)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/v1/audio/transcriptions", handle_transcriptions)
    app.router.add_post("/v1/audio/speech", handle_speech)
    if CHAT_UPSTREAM:
        # Registered LAST so the specific audio/health routes win; the catch-all takes everything else.
        app.on_startup.append(_open_session)
        app.on_cleanup.append(_close_session)
        app.router.add_route("*", "/{tail:.*}", handle_proxy)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not API_KEY:
        log.warning("No API_SERVER_KEY resolved — every request will 503 until one is set.")
    if CHAT_UPSTREAM:
        log.info("chat reverse-proxy ON — non-audio requests forward to %s", CHAT_UPSTREAM)
    log.info("cerap-audio-sidecar listening on 0.0.0.0:%d (hermes-agent=%s)", PORT, HERMES_AGENT_DIR)
    web.run_app(make_app(), host="0.0.0.0", port=PORT, print=None)
