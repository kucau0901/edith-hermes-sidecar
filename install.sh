#!/usr/bin/env bash
# EDITH · Hermes audio sidecar — one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/kucau0901/edith-hermes-sidecar/main/install.sh | bash
#
# Gives your own Hermes an HTTP voice endpoint so the EDITH plugin (Even Realities G2 glasses) can do
# speech. It installs a tiny sidecar INTO your existing Hermes venv (reusing Hermes's own STT engine +
# your existing API key), runs it under launchd (macOS) or systemd (Linux), and turns on chat-
# forwarding so EDITH reaches chat + voice through ONE URL. It never modifies Hermes itself.
#
# Overrides (env): HERMES_AGENT_DIR, HERMES_VENV_PY, HERMES_API_PORT (default 8642),
#                  HERMES_AUDIO_PORT (default 8643), HERMES_AUDIO_BIND (default 0.0.0.0 — narrow to
#                  127.0.0.1 only if you front it with a same-box tunnel), EDITH_AUDIO_BASE (fetch base).
set -euo pipefail

# Where install.sh fetches server.py from — the same public repo it lives in (raw GitHub).
BASE_URL="${EDITH_AUDIO_BASE:-https://raw.githubusercontent.com/kucau0901/edith-hermes-sidecar/main}"
PORT="${HERMES_AUDIO_PORT:-8643}"
API_PORT="${HERMES_API_PORT:-8642}"
UPSTREAM="http://127.0.0.1:${API_PORT}"
HERMES_DIR="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
INSTALL_DIR="$HOME/.hermes/hermes-audio-sidecar"
BEARER_DIR="$HOME/.hermes/hermes-audio-sidecar"  # .bearer fallback dir (server also reads the old cerap-audio-sidecar/)
LABEL="com.edith.hermes-audio"
LOG_DIR="$HOME/.hermes/logs"          # NOT world-readable /tmp — ~/.hermes is 0700
LOG="$LOG_DIR/hermes-audio-sidecar.log"
AIOHTTP_PIN="aiohttp==3.13.4"         # pinned for reproducible/auditable installs (bump deliberately)

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

bold "EDITH · Hermes audio sidecar installer"

# ── 0. Already running? ──────────────────────────────────────────────────────────────────────────
if HEALTH="$(curl -fsS "http://localhost:$PORT/health" 2>/dev/null)"; then
  if printf '%s' "$HEALTH" | grep -q '"proxy":[[:space:]]*true'; then
    info "An EDITH-ready sidecar is already running on :$PORT — nothing to do."
    bold "Done. Point EDITH's Hermes URL at this box; see the exposure note at the end."
    exit 0
  fi
  warn "A sidecar is running on :$PORT but chat-forwarding is OFF (looks like an audio-only sidecar)."
  warn "EDITH needs chat-forwarding. Add this to that service's environment and restart it:"
  warn "    HERMES_CHAT_UPSTREAM=$UPSTREAM"
  die  "Refusing to start a second sidecar on the same port. Reconfigure the existing one, then re-run to verify."
fi

# ── 1. Find Hermes's venv + confirm its STT engine imports ────────────────────────────────────────
VENV_PY="${HERMES_VENV_PY:-$HERMES_DIR/venv/bin/python}"
[ -x "$VENV_PY" ] || die "Hermes venv python not found at: $VENV_PY
  Set HERMES_AGENT_DIR (your hermes-agent folder) or HERMES_VENV_PY and re-run."
bold "Found your Hermes"
info "$VENV_PY"
if ! ( cd "$HERMES_DIR" && "$VENV_PY" -c "import tools.transcription_tools" ) >/dev/null 2>&1; then
  die "Could not import Hermes's STT engine from $HERMES_DIR.
  Make sure Hermes is installed with voice support:  pip install 'hermes-agent[voice]'"
fi
info "STT engine imports OK"

# ── 2. The one missing dependency ─────────────────────────────────────────────────────────────────
bold "Installing dependency (aiohttp) into the Hermes venv"
mkdir -p "$LOG_DIR"; chmod 700 "$LOG_DIR" 2>/dev/null || true
"$VENV_PY" -m pip install --quiet --disable-pip-version-check "$AIOHTTP_PIN" || die "pip install $AIOHTTP_PIN failed."
info "ok"

# ── 3. Fetch the sidecar ──────────────────────────────────────────────────────────────────────────
bold "Fetching the sidecar"
mkdir -p "$INSTALL_DIR"
curl -fsSL "$BASE_URL/server.py" -o "$INSTALL_DIR/server.py" \
  || die "Could not download $BASE_URL/server.py"
info "$INSTALL_DIR/server.py"

# ── 4. Bearer — reuse the Hermes API key; only ask if it can't be resolved ────────────────────────
bold "Resolving your Hermes API key"
KEY="$("$VENV_PY" - <<'PY'
import os, pathlib
k = os.environ.get("API_SERVER_KEY", "").strip()
if not k:
    try:
        import yaml
        c = yaml.safe_load((pathlib.Path.home() / ".hermes" / "config.yaml").read_text()) or {}
        k = str((((c.get("platforms") or {}).get("api_server") or {}).get("extra") or {}).get("key", "")).strip()
    except Exception:
        pass
print(k)
PY
)"
if [ -n "$KEY" ]; then
  info "Reusing the key from ~/.hermes/config.yaml (nothing written)."
else
  printf '  Enter your Hermes API_SERVER_KEY (the bearer token): '
  read -r KEY < /dev/tty
  [ -n "$KEY" ] || die "A key is required."
  mkdir -p "$BEARER_DIR"; chmod 700 "$BEARER_DIR" 2>/dev/null || true
  printf '%s' "$KEY" > "$BEARER_DIR/.bearer"
  chmod 600 "$BEARER_DIR/.bearer"
  info "Saved to $BEARER_DIR/.bearer (0600)."
fi

# ── 5. Install a persistence unit for this OS ─────────────────────────────────────────────────────
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  bold "Installing launchd agent"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$VENV_PY</string><string>$INSTALL_DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERMES_DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>HERMES_AGENT_DIR</key><string>$HERMES_DIR</string>
    <key>HERMES_AUDIO_PORT</key><string>$PORT</string>
    <key>HERMES_CHAT_UPSTREAM</key><string>$UPSTREAM</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load -w "$PLIST"
  info "Loaded $PLIST"
elif [ "$OS" = "Linux" ]; then
  bold "Installing systemd --user service"
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/hermes-audio-sidecar.service" <<EOF
[Unit]
Description=Hermes audio sidecar (EDITH)
After=network.target

[Service]
WorkingDirectory=$HERMES_DIR
Environment=HERMES_AGENT_DIR=$HERMES_DIR
Environment=HERMES_AUDIO_PORT=$PORT
Environment=HERMES_CHAT_UPSTREAM=$UPSTREAM
ExecStart=$VENV_PY $INSTALL_DIR/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now hermes-audio-sidecar.service
  loginctl enable-linger "$(id -un)" 2>/dev/null || warn "Could not enable lingering — the sidecar may stop when you log out (run: sudo loginctl enable-linger $(id -un))."
  info "Enabled hermes-audio-sidecar.service"
else
  case "$OS" in
    MINGW*|MSYS*|CYGWIN*|Windows*)
      die "Native Windows isn't supported yet. Run your Hermes AND this installer inside WSL (Ubuntu) —
  there it installs as a normal Linux systemd service. (Or start it by hand:
  '$VENV_PY $INSTALL_DIR/server.py'.)" ;;
    *)
      die "Unsupported OS: $OS (only macOS + Linux). Start it by hand: '$VENV_PY $INSTALL_DIR/server.py'." ;;
  esac
fi

# ── 6. Verify ─────────────────────────────────────────────────────────────────────────────────────
bold "Verifying"
OK=""
for _ in $(seq 1 12); do
  if H="$(curl -fsS "http://localhost:$PORT/health" 2>/dev/null)" && printf '%s' "$H" | grep -q '"proxy":[[:space:]]*true'; then
    OK=1; break
  fi
  sleep 1
done
[ -n "$OK" ] || die "Sidecar did not come up chat-forwarding — check the log: $LOG"
info "Sidecar is up on :$PORT with chat-forwarding ON ✓"

# ── 7. Last step: expose it SECURELY, then point EDITH at it ──────────────────────────────────────
bold "Done — your Hermes now speaks EDITH."

# Detect Tailscale so we can hand over the exact, already-authenticated URL.
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
TS_DNS="$(tailscale status --json 2>/dev/null | sed -n 's/.*"DNSName":"\([^"]*\)".*/\1/p' | head -1 | sed 's/\.$//' || true)"

cat <<EOF

  This one sidecar serves BOTH chat and voice, so EDITH needs just ONE URL. Reach it over a network
  that AUTHENTICATES the connection — the sidecar's bearer alone has no rate-limit or lockout, so
  don't hang it off a raw public tunnel. In order of preference:
EOF
if [ -n "$TS_IP" ]; then
  cat <<EOF

  ✅ Tailscale (recommended — detected on this box). Only your tailnet can reach it. Point EDITH at:
        http://$TS_IP:$PORT${TS_DNS:+        (or  http://$TS_DNS:$PORT)}
     Nothing else to configure. For TLS over the tailnet, optionally:  tailscale serve --bg $PORT
EOF
else
  cat <<EOF

  ✅ Tailscale (recommended). Install it here + on your phone; the tailnet authenticates the network
     layer. Then point EDITH at  http://<this-box-tailscale-ip>:$PORT  — nothing else to configure.
EOF
fi
cat <<EOF

  ✅ Cloudflare Access / mTLS — if you MUST use the public internet, put it behind Access (identity +
     logging + revocation), not a bearer-only tunnel:   https://hermes.example.com  ->  http://localhost:$PORT
     Then narrow the sidecar so only the local tunnel can reach it: set  HERMES_AUDIO_BIND=127.0.0.1.

  Then in EDITH → Assistant → Hermes: paste the URL + your API key, pick "Hermes" as the chat brain,
  and tap Test.

  (Already running Cerap or another client on this box? Untouched — this is a universal Hermes access
  shim; existing clients that route chat straight to :$API_PORT keep working, and the chat-forwarding
  you just enabled is only used by clients that point directly at :$PORT.)
EOF
