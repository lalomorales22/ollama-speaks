# ollama speaks

A local voice studio for the [speech models in Ollama](https://ollama.com/blog/speech-models).
Type something and hear it in any of Phonon's voices, or have a spoken back-and-forth with a local model
while a 3D orb breathes, listens, thinks and talks in front of you. No cloud, no keys, no chatbot window.

![ollama speaks](docs/screenshot.png)

## What it does

- **Speak** mode: type a line, tap a voice button (Hadley, Zoey, Grub the Ogre, The Snow Queen, 38 English voices plus the multilingual set), press Enter. The orb lights up amber and deforms to the audio while it talks.
- **Converse** mode: talk with any chat model you have in Ollama (or a local OpenAI-compatible server). Replies stream sentence by sentence into Phonon so the voice starts within a second. Tap the mic or hold **space** to talk; turn on **hands-free** and it listens again every time it finishes speaking.
- **Local speech-to-text** with [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) (large-v3-turbo). Falls back to the browser's speech recognition if whisper is not installed.
- **Conversations save themselves** as JSON in `~/speech-app/conversations/`. Reopen them from the drawer, replay any line, download the WAV, export the transcript as text.
- `speech "hello there"` from the terminal speaks instantly without opening the UI.

## Install (macOS, Apple Silicon)

```bash
curl -fsSL https://raw.githubusercontent.com/lalomorales22/ollama-speaks/main/install.sh | bash
```

or from a clone:

```bash
git clone https://github.com/lalomorales22/ollama-speaks && cd ollama-speaks && ./install.sh
```

The installer checks Ollama (0.40.11 or newer), pulls `phonon`, builds a Python 3.12 venv with mlx-whisper, downloads the whisper weights, copies the app to `~/speech-app` and adds a `speech` alias to your shell.

Open a new terminal and type:

```
speech                     open the app
speech "hello there"       speak it right now in the default voice
speech -v Zoey "hi"        speak it in a specific voice
speech voices              list voices
speech stop | log | status
```

## Requirements

- macOS on Apple Silicon (Phonon in Ollama uses MLX; Windows and Linux are "coming soon" upstream)
- Ollama 0.40.11+
- Python 3.12 via `uv` for whisper (optional), `ffmpeg` for mic audio decoding (`brew install ffmpeg`)
- Any browser. Chrome is the smoothest for the mic.

## How it is built

Three files, no framework, no build step.

| file | role |
|---|---|
| `server.py` | stdlib `http.server`. Proxies Ollama's `/v1/audio/speech` (TTS) and `/api/chat` (streaming), runs mlx-whisper for `/api/transcribe`, stores conversations as JSON. |
| `index.html` | the UI. three.js orb driven by a Web Audio analyser on the playback and mic streams; simplex-noise vertex displacement, fresnel glow, particle shell. |
| `speech` | launcher. Starts Ollama if needed, pulls `phonon`, starts the server on `127.0.0.1:8794`, opens the browser. |

The orb has four states: **idle** (slow blue breathing), **listening** (cyan, reacts to your mic), **thinking** (violet shimmer while the model generates) and **speaking** (amber, displaced by the voice's waveform).

### Server endpoints

```
GET  /api/status              ollama version + whisper state
GET  /api/models              {tts:[...], chat:[...]}
GET  /api/voices?model=phonon voices for a TTS model (from `ollama show`)
POST /api/speak               {model, voice, input} -> audio/wav stream
POST /api/chat                {model, messages, think} -> NDJSON {delta, done}
POST /api/transcribe          raw audio body -> {text}
GET/PUT/DELETE /api/conversations[/<id>]
```

Environment knobs: `SPEECH_PORT` (default 8794), `SPEECH_WHISPER` (HF repo, default `mlx-community/whisper-large-v3-turbo`), `SPEECH_VOICE` (CLI default voice), `OLLAMA_HOST`.

## Models

- `phonon` — English, Apache 2.0. The default.
- `phonon:40m-multilingual` (and `-fr`, `-de`, `-es`, `-pt`) — CC BY-NC 4.0, noncommercial only. Pull one and pick it in the voice-model dropdown.
- Chat: any Ollama model. The app lists what you have and defaults to the first of `gemma4:12b-mlx`, `qwen3.5:2b`, `gemma4:26b`. Thinking is turned off for voice replies so they start fast.

## License

MIT. Phonon voices are from [Gradium](https://huggingface.co/gradium/phonon-voices); see their licenses above.
