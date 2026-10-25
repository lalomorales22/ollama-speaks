#!/usr/bin/env bash
# ollama-speaks installer — macOS (Apple Silicon)
#
#   curl -fsSL https://raw.githubusercontent.com/lalomorales22/ollama-speaks/main/install.sh | bash
#   or, from a clone:  ./install.sh
#
# What it does:
#   1. checks for Ollama >= 0.40.11 (installs via Homebrew if missing)
#   2. pulls the `phonon` text-to-speech model (English voices)
#   3. installs `uv` if missing and builds a Python 3.12 venv with mlx-whisper (local speech-to-text)
#   4. downloads the whisper model weights so the first mic press is instant
#   5. copies the app to ~/speech-app and adds a `speech` alias to your shell rc
set -euo pipefail

REPO="https://github.com/lalomorales22/ollama-speaks"
DEST="${SPEECH_HOME:-$HOME/speech-app}"
WHISPER="${SPEECH_WHISPER:-mlx-community/whisper-large-v3-turbo}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

c_dim=$'\033[2m'; c_ok=$'\033[38;5;79m'; c_warn=$'\033[38;5;214m'; c_err=$'\033[38;5;210m'; c_0=$'\033[0m'
step(){ printf '\n%s▸ %s%s\n' "$c_ok" "$*" "$c_0"; }
note(){ printf '  %s%s%s\n' "$c_dim" "$*" "$c_0"; }
warn(){ printf '  %s%s%s\n' "$c_warn" "$*" "$c_0"; }
die(){ printf '%s✗ %s%s\n' "$c_err" "$*" "$c_0"; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }
vergte(){ [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

[ "$(uname -s)" = "Darwin" ] || die "ollama-speaks needs macOS (Phonon TTS in Ollama is Apple Silicon only for now)."
[ "$(uname -m)" = "arm64" ] || warn "Not Apple Silicon — Phonon TTS and mlx-whisper will not run here."

# ---------------------------------------------------------------- 1. ollama
step "Ollama"
if ! have ollama; then
  have brew || die "Ollama not found and Homebrew is missing. Install from https://ollama.com/download then re-run."
  note "installing with Homebrew…"; brew install --cask ollama
fi
OV="$(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo 0)"
if ! vergte "$OV" "0.40.11"; then
  warn "Ollama $OV found; speech needs 0.40.11+. Update it (Ollama menu → Check for updates, or brew upgrade --cask ollama)."
fi
note "ollama $OV"
if ! curl -sf -m 2 http://127.0.0.1:11434/api/version >/dev/null; then
  note "starting ollama…"
  if [ -d /Applications/Ollama.app ]; then open -a Ollama; else (nohup ollama serve >/dev/null 2>&1 &); fi
  for _ in $(seq 1 40); do curl -sf -m 1 http://127.0.0.1:11434/api/version >/dev/null && break; sleep 0.5; done
fi

# ---------------------------------------------------------------- 2. phonon
step "Phonon text-to-speech model"
if curl -s http://127.0.0.1:11434/api/tags | grep -q '"phonon:latest"'; then note "already pulled"; else ollama pull phonon; fi
note "optional: ollama pull phonon:40m-multilingual   (en/fr/de/es/pt, noncommercial license)"

# ---------------------------------------------------------------- 3. app files
step "App files → $DEST"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
if [ -n "$SRC" ] && [ -f "$SRC/server.py" ] && [ -f "$SRC/index.html" ]; then
  mkdir -p "$DEST"
  if [ "$SRC" != "$DEST" ]; then cp -R "$SRC/server.py" "$SRC/index.html" "$SRC/speech" "$SRC/static" "$DEST/"; fi
else
  have git || die "git is required to fetch the app."
  TMP="$(mktemp -d)"; git clone --depth 1 "$REPO" "$TMP/ollama-speaks" >/dev/null 2>&1 || die "clone failed: $REPO"
  mkdir -p "$DEST"; cp -R "$TMP"/ollama-speaks/{server.py,index.html,speech,static} "$DEST/"; rm -rf "$TMP"
fi
mkdir -p "$DEST/conversations" "$DEST/data"; chmod +x "$DEST/speech"
note "ok"

# ---------------------------------------------------------------- 4. whisper venv
step "Local speech-to-text (mlx-whisper)"
if ! have uv; then note "installing uv…"; curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || warn "uv install failed"; fi
if have uv; then
  [ -x "$DEST/.venv/bin/python" ] || uv venv "$DEST/.venv" --python 3.12 -q
  uv pip install -q --python "$DEST/.venv/bin/python" mlx-whisper numpy && note "mlx-whisper installed"
  have ffmpeg || { have brew && { note "installing ffmpeg (audio decode)…"; brew install ffmpeg; } || warn "ffmpeg missing — mic transcription needs it (brew install ffmpeg)"; }
  note "downloading whisper weights ($WHISPER)… this is the slow part, ~1.6 GB"
  "$DEST/.venv/bin/python" - <<EOF || warn "weights download failed — it will retry when the app starts"
import numpy as np, mlx_whisper
mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo="$WHISPER", fp16=True)
print("  weights ready")
EOF
else
  warn "skipping whisper; the app falls back to the browser's speech recognition"
fi

# ---------------------------------------------------------------- 5. alias
step "Shell alias"
RC="$HOME/.zshrc"; case "${SHELL:-}" in */bash) RC="$HOME/.bashrc";; esac
if grep -q "speech-app/speech" "$RC" 2>/dev/null; then note "alias already in $RC"; else
  printf '\n# ollama-speaks — local voice studio, `speech` opens the app\nalias speech=%q\n' "$DEST/speech" >> "$RC"; note "added to $RC"; fi

printf '\n%s✓ installed%s\n\n' "$c_ok" "$c_0"
printf '  open a new terminal (or: source %s) then type:\n\n' "$RC"
printf '    speech                      open the app\n'
printf '    speech "hello there"        speak it right now\n'
printf '    speech -v Zoey "hi"         pick a voice\n'
printf '    speech voices | stop | log\n\n'
