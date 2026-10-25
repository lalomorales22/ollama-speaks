#!/usr/bin/env python3
"""speech — local voice studio on top of Ollama's Phonon TTS.

Endpoints (all local, no auth):
  GET  /                        the app
  GET  /api/status              ollama + whisper status, defaults
  GET  /api/models              {tts:[...], chat:[...]}
  GET  /api/voices?model=phonon voices for a TTS model
  POST /api/speak               {model, voice, input} -> audio/wav (streamed)
  POST /api/chat                {model, messages, think} -> NDJSON stream from Ollama
  POST /api/transcribe          raw audio body (webm/ogg/wav) -> {text}
  GET  /api/conversations       list
  GET  /api/conversations/<id>  one
  PUT  /api/conversations/<id>  save (full JSON body)
  DELETE /api/conversations/<id>
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = Path(__file__).resolve().parent
CONV = APP / "conversations"
CONV.mkdir(exist_ok=True)
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
PORT = int(os.environ.get("SPEECH_PORT", "8794"))
WHISPER_REPO = os.environ.get("SPEECH_WHISPER", "mlx-community/whisper-large-v3-turbo")
CLAUDES_GUY = "http://127.0.0.1:8080"

sys.path.insert(0, str(APP))
from orb import Orb, REFLECT_PROMPT, parse_reflection  # noqa: E402
(APP / "data").mkdir(exist_ok=True)
ORB = Orb(APP / "data" / "orb.db")

# ---------------------------------------------------------------- whisper
_whisper = {"mod": None, "state": "checking", "error": ""}


def _load_whisper():
    try:
        import mlx_whisper  # noqa
        _whisper["mod"] = mlx_whisper
        _whisper["state"] = "warming"
        # Pull weights now so the first mic press is not a 1.6 GB download.
        import numpy as np
        silent = np.zeros(16000, dtype=np.float32)
        mlx_whisper.transcribe(silent, path_or_hf_repo=WHISPER_REPO, fp16=True)
        _whisper["state"] = "ready"
    except ImportError as e:
        _whisper["state"] = "missing"
        _whisper["error"] = str(e)
    except Exception as e:  # download failure etc.
        _whisper["state"] = "error"
        _whisper["error"] = str(e)[:300]


threading.Thread(target=_load_whisper, daemon=True).start()


def transcribe(raw: bytes, content_type: str) -> str:
    if _whisper["mod"] is None:
        raise RuntimeError("whisper not available: " + _whisper["error"])
    ext = ".webm"
    if "ogg" in content_type:
        ext = ".ogg"
    elif "wav" in content_type:
        ext = ".wav"
    elif "mp4" in content_type or "m4a" in content_type:
        ext = ".mp4"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(raw)
        src = f.name
    wav = src + ".wav"
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src,
             "-ac", "1", "-ar", "16000", "-f", "wav", wav],
            check=True, timeout=60,
        )
        out = _whisper["mod"].transcribe(wav, path_or_hf_repo=WHISPER_REPO, fp16=True)
        return (out.get("text") or "").strip()
    finally:
        for p in (src, wav):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------- ollama
def ollama_json(path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OLLAMA + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def list_models():
    tags = ollama_json("/api/tags").get("models", [])
    tts, chat = [], []
    for m in tags:
        name = m["name"]
        fam = (m.get("details") or {}).get("family", "") or ""
        if "phonon" in name or "tts" in fam.lower():
            tts.append(name)
            continue
        if "embedding" in name or name.startswith("x/") or "image" in name:
            continue
        chat.append(name)
    tts.sort(key=lambda n: (n != "phonon:latest", n))
    pref = ["gemma4:12b-mlx", "qwen3.5:2b", "gemma4:26b", "gpt-oss:20b"]
    chat.sort(key=lambda n: (pref.index(n) if n in pref else 99, n))
    # claudes-guy's MLX server, if up
    try:
        urllib.request.urlopen(CLAUDES_GUY + "/v1/models", timeout=0.5)
        chat.insert(1 if chat else 0, "claudes-guy")
    except Exception:
        pass
    return {"tts": tts, "chat": chat}


_voice_cache = {}


def list_voices(model):
    if model in _voice_cache:
        return _voice_cache[model]
    info = ollama_json("/api/show", {"model": model})
    voices = info.get("voices") or []
    voices = [v for v in voices if v != "default"]
    _voice_cache[model] = voices
    return voices


SENT_RE = re.compile(r"\s+")


def clean_for_speech(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"[*_`#>]+", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"<think>.*?</think>", " ", t, flags=re.S)
    return SENT_RE.sub(" ", t).strip()


# ---------------------------------------------------------------- handler
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "speech/1.0"

    def log_message(self, fmt, *a):
        if os.environ.get("SPEECH_DEBUG"):
            super().log_message(fmt, *a)

    # helpers
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _err(self, msg, code=500):
        self._json({"error": str(msg)}, code)

    def _file(self, path: Path, ctype):
        b = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    # routes
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._file(APP / "index.html", "text/html; charset=utf-8")
            if u.path.startswith("/static/"):
                fname = u.path.rsplit("/", 1)[1]
                if not re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+", fname):
                    return self._err("not found", 404)
                ext = fname.rsplit(".", 1)[-1]
                p = APP / "static" / fname
                if not p.is_file():
                    return self._err("not found", 404)
                ctype = {"js": "application/javascript", "css": "text/css",
                         "png": "image/png", "svg": "image/svg+xml"}.get(ext, "application/octet-stream")
                return self._file(p, ctype)
            if u.path == "/api/status":
                try:
                    ver = ollama_json("/api/version", timeout=2).get("version")
                except Exception:
                    ver = None
                return self._json({
                    "ollama": ver,
                    "whisper": _whisper["state"],
                    "whisper_error": _whisper["error"],
                    "whisper_model": WHISPER_REPO,
                })
            if u.path == "/api/models":
                return self._json(list_models())
            if u.path == "/api/voices":
                model = (q.get("model") or ["phonon"])[0]
                return self._json({"model": model, "voices": list_voices(model)})
            if u.path == "/api/orb":
                ORB.touch_session()
                return self._json(ORB.state())
            if u.path == "/api/orb/history":
                return self._json(ORB.history())
            if u.path == "/api/orb/persona":
                return self._json({"persona": ORB.persona()})
            if u.path == "/api/conversations":
                items = []
                for p in CONV.glob("*.json"):
                    try:
                        d = json.loads(p.read_text())
                        items.append({
                            "id": d.get("id", p.stem),
                            "title": d.get("title") or "Untitled",
                            "updated": d.get("updated", 0),
                            "count": len(d.get("messages", [])),
                            "voice": d.get("voice"),
                        })
                    except Exception:
                        pass
                items.sort(key=lambda x: -x["updated"])
                return self._json(items)
            if u.path.startswith("/api/conversations/"):
                cid = _safe_id(u.path.rsplit("/", 1)[1])
                p = CONV / f"{cid}.json"
                if not p.exists():
                    return self._err("not found", 404)
                return self._file(p, "application/json")
            self._err("not found", 404)
        except Exception as e:
            self._err(e)

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/orb/memories/"):
            ORB.forget(_safe_id(u.path.rsplit("/", 1)[1]))
            return self._json({"ok": True})
        if u.path == "/api/orb/style":
            return self._json(ORB.reset_style())
        if u.path.startswith("/api/conversations/"):
            cid = _safe_id(u.path.rsplit("/", 1)[1])
            p = CONV / f"{cid}.json"
            if p.exists():
                p.unlink()
            return self._json({"ok": True})
        self._err("not found", 404)

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path == "/api/orb/style":
            try:
                d = json.loads(self._body().decode() or "{}")
            except Exception as e:
                return self._err(f"bad json: {e}", 400)
            return self._json(ORB.set_style(d))
        if u.path == "/api/orb/name":
            d = json.loads(self._body().decode() or "{}")
            ORB.rename(d.get("name"))
            return self._json(ORB.state())
        if u.path.startswith("/api/conversations/"):
            cid = _safe_id(u.path.rsplit("/", 1)[1])
            try:
                d = json.loads(self._body().decode())
            except Exception as e:
                return self._err(f"bad json: {e}", 400)
            d["id"] = cid
            d["updated"] = time.time()
            (CONV / f"{cid}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
            return self._json({"ok": True, "updated": d["updated"]})
        self._err("not found", 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/speak":
                return self._speak()
            if u.path == "/api/chat":
                return self._chat()
            if u.path == "/api/orb/turn":
                d = json.loads(self._body().decode() or "{}")
                return self._json(ORB.turn(heard=d.get("heard") or "", spoken=d.get("spoken") or "",
                                           voice=d.get("voice"), mode=d.get("mode") or "chat", conv=d.get("conv")))
            if u.path == "/api/orb/reflect":
                return self._reflect()
            if u.path == "/api/orb/memories":
                d = json.loads(self._body().decode() or "{}")
                added = ORB.add_memories([{"kind": d.get("kind") or "fact", "text": d.get("text") or ""}])
                return self._json({"added": added, "state": ORB.state()})
            if u.path == "/api/orb/reset":
                d = json.loads(self._body().decode() or "{}")
                ORB.reset(keep_memories=bool(d.get("keep_memories")))
                return self._json(ORB.state())
            if u.path == "/api/transcribe":
                raw = self._body()
                if len(raw) < 1000:
                    return self._json({"text": ""})
                text = transcribe(raw, self.headers.get("Content-Type", ""))
                return self._json({"text": text})
            self._err("not found", 404)
        except urllib.error.HTTPError as e:
            self._err(f"ollama {e.code}: {e.read().decode(errors='replace')[:400]}", 502)
        except Exception as e:
            self._err(e)

    def _reflect(self):
        """Ask the chat model what the orb should remember from these messages."""
        body = json.loads(self._body().decode() or "{}")
        msgs = body.get("messages") or []
        model = body.get("model") or "qwen3.5:2b"
        conv = body.get("conv")
        if not msgs:
            return self._err("no messages", 400)
        existing = "; ".join(m["text"] for m in ORB.state()["memories"][:30]) or "(none)"
        transcript = "\n".join(f"{'Person' if m.get('role') == 'user' else 'Companion'}: {m.get('content') or m.get('text') or ''}"
                               for m in msgs[-16:])
        prompt = REFLECT_PROMPT % (existing, transcript)
        if model == "claudes-guy":
            payload = {"model": "default", "messages": [{"role": "user", "content": prompt}], "stream": False}
            out = ollama_json_url(CLAUDES_GUY + "/v1/chat/completions", payload)
            raw = (out.get("choices") or [{}])[0].get("message", {}).get("content", "")
        else:
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False,
                       "think": False, "format": "json", "options": {"temperature": 0.2}}
            try:
                out = ollama_json("/api/chat", payload, timeout=180)
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")
                if "think" not in msg:
                    raise
                payload.pop("think")
                out = ollama_json("/api/chat", payload, timeout=180)
            raw = (out.get("message") or {}).get("content", "")
        ref = parse_reflection(raw)
        added = ORB.add_memories(ref["memories"], conv=conv)
        if ref.get("mood"):
            ORB.set_mood(str(ref["mood"]))
        st = ORB.state()
        st["added"] = added
        return self._json(st)

    def _speak(self):
        body = json.loads(self._body().decode() or "{}")
        text = clean_for_speech(body.get("input") or "")
        if not text:
            return self._err("empty input", 400)
        payload = {
            "model": body.get("model") or "phonon",
            "voice": body.get("voice") or "Hadley",
            "input": text,
            "response_format": "wav",
        }
        req = urllib.request.Request(OLLAMA + "/v1/audio/speech",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = r.read(16384)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    def _chat(self):
        body = json.loads(self._body().decode() or "{}")
        model = body.get("model") or "qwen3.5:2b"
        messages = body.get("messages") or []
        if model == "claudes-guy":
            return self._chat_openai(CLAUDES_GUY, messages)
        payload = {"model": model, "messages": messages, "stream": True}
        if body.get("think") is False:
            payload["think"] = False
        try:
            r = _open(OLLAMA + "/api/chat", payload)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if "think" in msg and "think" in payload:
                payload.pop("think")
                r = _open(OLLAMA + "/api/chat", payload)
            else:
                return self._err(f"ollama {e.code}: {msg[:400]}", 502)
        self._stream_lines(r, lambda line: _ndjson_to_delta(line))

    def _chat_openai(self, base, messages):
        payload = {"model": "default", "messages": messages, "stream": True}
        r = _open(base + "/v1/chat/completions", payload)
        self._stream_lines(r, _sse_to_delta)

    def _stream_lines(self, r, convert):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        with r:
            for line in r:
                out = convert(line)
                if out is None:
                    continue
                b = (json.dumps(out) + "\n").encode()
                self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n")
                self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


def ollama_json_url(url, payload, timeout=180):
    with _open(url, payload) as r:
        return json.loads(r.read().decode())


def _open(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=600)


def _ndjson_to_delta(line):
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except Exception:
        return None
    msg = d.get("message") or {}
    return {"delta": msg.get("content", ""), "thinking": msg.get("thinking", ""),
            "done": bool(d.get("done"))}


def _sse_to_delta(line):
    line = line.decode(errors="replace").strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return {"delta": "", "done": True}
    try:
        d = json.loads(data)
        ch = (d.get("choices") or [{}])[0]
        return {"delta": (ch.get("delta") or {}).get("content") or "",
                "done": ch.get("finish_reason") is not None}
    except Exception:
        return None


def _safe_id(s):
    return re.sub(r"[^A-Za-z0-9_-]", "", s)[:64] or "x"


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    print(f"speech: http://127.0.0.1:{PORT}  (ollama {OLLAMA}, whisper {WHISPER_REPO})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
