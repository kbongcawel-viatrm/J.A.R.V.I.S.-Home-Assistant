#!/usr/bin/env python3
"""Self-contained voice assistant.

Mic -> energy VAD -> STT (whisper.cpp server or faster-whisper) -> Ollama (LLM)
-> Piper/Kokoro (TTS) -> speaker.

Prereqs (all in whisper_env unless noted):
    piper-tts (or kokoro), ollama running (localhost:11434),
    soundfile, pyaudio, requests, numpy. Plus `aplay`/`paplay`/`ffplay` for playback.
    For --stt whispercpp: whisper-server from whisper.cpp on :8178.
    For --stt faster-whisper: faster-whisper package.
    The sound gate (on by default) additionally needs torch + transformers
    and downloads the AST AudioSet model on first use.

Usage:
    whisper_env/bin/python scripts/voice_assistant.py [--stt whispercpp] [--tts piper]
"""

import argparse
import io
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pyaudio
import requests
import soundfile as sf

SAMPLE_RATE = 16000
CHUNK = 512  # samples per read (~32 ms)

SYSTEM_PROMPT = (
    "You are a helpful voice assistant. Reply in one to three short spoken "
    "sentences. No markdown, no lists, no emojis."
)


def log(msg):
    print(f"[assistant] {msg}", flush=True)


class Speaker:
    """Serializes TTS synthesis + playback."""

    def __init__(self, engine, voice, effect="none"):
        self.engine = engine
        self.voice = voice
        self.effect = effect
        self._lock = threading.Lock()
        if engine == "kokoro":
            from kokoro import KPipeline
            self.pipeline = KPipeline(lang_code="a")
        else:
            from piper import PiperVoice
            model_path = Path(__file__).parent / "voices" / "lessac.onnx"
            if not model_path.exists():
                raise FileNotFoundError(
                    f"Piper voice model not found at {model_path}. "
                    "Download e.g. en_US-lessac-medium.onnx + .onnx.json there."
                )
            self.voice_obj = PiperVoice.load(str(model_path))

    def speak(self, text):
        """Synthesize `text` and play it. Thread-safe; serialized."""
        text = text.strip()
        if not text:
            return
        with self._lock:
            if self.engine == "kokoro":
                chunks = [a for _, _, a in self.pipeline(text, voice=self.voice)]
                if not chunks:
                    return
                pcm = np.concatenate(chunks)
                pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
                self._write_and_play(pcm16, 24000)
            else:
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    self.voice_obj.synthesize_wav(text, w)
                buf.seek(0)
                self._play_wav_bytes(buf.read())

    def _write_and_play(self, pcm16, rate):
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        try:
            sf.write(tmp, pcm16, rate, subtype="PCM_16")
            eff = self._effected(tmp)
            try:
                self._play(eff)
            finally:
                if eff != tmp:
                    Path(eff).unlink(missing_ok=True)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _play_wav_bytes(self, data):
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            eff = self._effected(tmp)
            try:
                self._play(eff)
            finally:
                if eff != tmp:
                    Path(eff).unlink(missing_ok=True)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _effected(self, path):
        """Return path to JARVIS-ified audio, or `path` unchanged."""
        if self.effect != "jarvis":
            return path
        out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        # pitch down ~18%, band-limit -> muffled synthetic voice
        af = ("asetrate=24000*0.82,aresample=24000,atempo=1.22,"
              "lowpass=f=3000,highpass=f=250")
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path, "-af", af, out],
            capture_output=True, timeout=60)
        if r.returncode == 0 and Path(out).exists():
            return out
        Path(out).unlink(missing_ok=True)
        return path

    @staticmethod
    def _play(path):
        for cmd in (["paplay", path], ["aplay", "-q", path],
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", path]):
            try:
                subprocess.run(cmd, check=True, timeout=120,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except (OSError, subprocess.SubprocessError):
                continue
        log("WARNING: no audio player found (paplay/aplay/ffplay)")


class SoundGate:
    """Pre-STT AudioSet classifier.

    Scores each utterance with an AST model trained on AudioSet and drops
    clips dominated by non-speech sounds (dog barks, baby cries, meows,
    crying, ...) so they never reach the transcriber.
    """

    DEFAULT_MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"
    # AST consumes ~10.24 s of audio per forward pass (1024 frames @ 100 fps)
    _CHUNK = 160_000  # samples @ 16 kHz

    # AudioSet labels meaning "not a person addressing the assistant":
    # animal sounds (dogs, pets, livestock, birds, insects) and human
    # crying/laughter/non-speech vocalizations (incl. baby cries)
    _BLOCK_LABELS = {
        # animals
        "animal", "domestic animals, pets", "wild animals",
        "dog", "bark", "yip", "howl", "bow-wow", "growling",
        "whimper (dog)", "canidae, dogs, wolves",
        "cat", "purr", "meow", "hiss", "caterwaul",
        "roaring cats (lions, tigers)",
        "livestock, farm animals, working animals",
        "horse", "clip-clop", "neigh, whinny",
        "cattle, bovinae", "moo", "pig", "oink", "goat", "bleat", "sheep",
        "fowl", "chicken, rooster", "cluck",
        "crowing, cock-a-doodle-doo", "turkey", "gobble",
        "duck", "quack", "goose", "honk",
        "bird", "bird vocalization, bird call, bird song",
        "chirp, tweet", "squawk", "pigeon, dove", "coo", "crow", "caw",
        "owl", "hoot", "bird flight, flapping wings",
        "rodents, rats, mice", "mouse", "patter",
        "insect", "cricket", "mosquito", "fly, housefly", "buzz",
        "bee, wasp, etc.", "frog", "croak", "snake", "rattle",
        "whale vocalization", "roar",
        # human crying / non-speech vocalizations
        "crying, sobbing", "baby cry, infant cry", "whimper",
        "wail, moan", "battle cry",
        "laughter", "baby laughter", "giggle", "snicker", "belly laugh",
        "chuckle, chortle",
        "gasp", "pant", "snort", "cough", "throat clearing", "sneeze",
        "sniff", "chewing, mastication", "burping, eructation",
        "hiccup", "grunt", "squeal",
    }

    def __init__(self, model_id, threshold, margin=1.5):
        import torch
        from transformers import ASTFeatureExtractor, ASTForAudioClassification
        self._torch = torch
        self.threshold = threshold
        self.margin = margin
        self.extractor = ASTFeatureExtractor.from_pretrained(model_id)
        self.model = ASTForAudioClassification.from_pretrained(model_id)
        self.model.eval()
        self._block_idx, self._speech_idx = [], []
        for idx, name in self.model.config.id2label.items():
            name = name.lower()
            if name in self._BLOCK_LABELS:
                self._block_idx.append(int(idx))
            if "speech" in name:
                self._speech_idx.append(int(idx))

    def is_blocked(self, audio):
        """True when the clip is dominated by a blocked sound class."""
        best_block = best_speech = 0.0
        for start in range(0, audio.size, self._CHUNK):
            chunk = audio[start:start + self._CHUNK]
            inputs = self.extractor(chunk, sampling_rate=SAMPLE_RATE,
                                     return_tensors="pt")
            with self._torch.no_grad():
                logits = self.model(**inputs).logits[0]
            # AudioSet is multi-label: independent sigmoid per class
            probs = self._torch.sigmoid(logits).numpy()
            if self._block_idx:
                best_block = max(best_block,
                                 float(probs[self._block_idx].max()))
            if self._speech_idx:
                best_speech = max(best_speech,
                                  float(probs[self._speech_idx].max()))
        return (best_block >= self.threshold
                and best_block > best_speech * self.margin)


class Assistant:
    def __init__(self, args):
        self.args = args
        self.whisper = None
        if args.stt == "faster-whisper":
            from faster_whisper import WhisperModel
            self.whisper = WhisperModel(args.model, device="cpu", compute_type="int8")
        self.speaker = Speaker(args.tts, args.tts_voice, args.tts_effect)
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.utterances = queue.Queue()
        self._stop = threading.Event()
        self._speaking_until = 0.0   # echo suppression until (epoch)
        self._gate = None
        self._gate_failed = False

    def _reject_non_speech(self, audio):
        """Drop clips the sound gate classifies as dog/baby cries etc."""
        if not self.args.sound_gate:
            return False
        if self._gate is None and not self._gate_failed:
            try:
                self._gate = SoundGate(self.args.gate_model,
                                       self.args.gate_threshold,
                                       self.args.gate_margin)
            except Exception as e:
                self._gate_failed = True
                log(f"sound gate unavailable, continuing without it: {e}")
        if self._gate is None:
            return False
        try:
            if self._gate.is_blocked(audio):
                log("(non-speech sound ignored)")
                return True
        except Exception as e:
            log(f"sound gate error: {e}")
        return False


    def _say(self, text):
        """Speak and mark a short echo-suppression tail."""
        self.speaker.speak(text)
        # cover the VAD silence window or our own voice leaks through as
        # a fresh utterance right after suppression expires
        self._speaking_until = time.time() + self.args.silence_seconds + 0.3

    def _play_file(self, name):
        """Play a pre-rendered asset from assets/; suppress echo for its duration."""
        path = Path(__file__).parent.parent / "assets" / name
        if not path.exists():
            log(f"missing asset: {name}")
            return
        try:
            dur = float(subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=10).stdout.strip())
        except Exception:
            dur = 15.0
        self._speaking_until = time.time() + dur + self.args.silence_seconds + 0.3
        Speaker._play(str(path))

    # window-mode (no wake word) restart needs an explicit target, so a
    # bare hallucinated "restart." can't bounce every container
    _RESTART_EXPLICIT = re.compile(
        r"\brestart\b\W*(?:the\W+)?(?:containers?|pods?|services?|everything|all)\b")

    def _command_reply(self, command, trusted=False):
        t = " ".join(command.lower().split())
        # "status" almost always means the dashboard check; whisper may
        # mishear "check status" as "czech"/"czech status" etc.
        is_status = ("status" in t or "check service" in t or "czech" in t or
                     t.strip(" .!?") in ("check", "check it", "check them"))
        if is_status:
            self._play_file("jarvis-check-status-response.wav")
            return self._service_summary()
        if "restart" in t and (trusted or self._RESTART_EXPLICIT.search(t)):
            self._play_file("jarvis-restart-response.wav")
            return self._restart_containers()
        if self.args.freeform:
            return self.ask_llm(command)
        return ""  # unrecognized command: stay silent

    # ---------- LLM ----------
    def ask_llm(self, user_text):
        self.history.append({"role": "user", "content": user_text})
        self.history = self.history[-12:]  # keep ~6 exchanges
        resp = requests.post(
            f"{self.args.ollama_url}/api/chat",
            json={
                "model": self.args.ollama_model,
                "messages": self.history,
                "stream": False,
                "options": {"num_predict": 80, "num_ctx": 1024, "temperature": 0.6},
            },
            timeout=180,
        )
        resp.raise_for_status()
        reply = resp.json()["message"]["content"].strip()
        self.history.append({"role": "assistant", "content": reply})
        return reply

    # ---------- worker: utterance -> transcript -> reply ----------
    def _worker(self):
        while not self._stop.is_set():
            try:
                item = self.utterances.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle_utterance(item)
            except Exception as e:
                log(f"ERROR handling utterance: {e}")

    def _handle_utterance(self, item):
        audio, captured_at = item
        t0 = time.time()
        if captured_at < self._speaking_until:
            log("(echo ignored)")
            return
        if self._reject_non_speech(audio):
            return
        text = self._transcribe(audio)
        if len(text) < 2 or text.lstrip()[:1] in "[(":
            return  # short/empty or whisper noise-tag like [INAUDIBLE]/(whooshing)
        if self._wake_hit(text):
            command = self._strip_wake(text).strip(" ,.!?")
            log(f"You: {command or '(wake)'}")
            if not command:
                self._play_file("jarvis-greetings-response.wav")
                reply = ""
            else:
                reply = self._command_reply(command, trusted=True)
        else:
            log(f"(heard, ignored): {text}")
            return
        log(f"AI : {reply or '(silent)'}")
        if reply:
            self._say(reply)
        log(f"(turn took {time.time() - t0:.1f}s)")

    # ---------- wake word ----------
    _WAKE_THRESHOLD = 0.65        # liberal: any jarvis-like sound triggers
    _WAKE_WINDOW_THRESHOLD = 0.70  # for words split across tokens
    # observed whisper mishearings of the wake word (add as they appear)
    _WAKE_ALIASES = {"garvey", "darwin"}

    def _wake_words(self, text):
        """Yield (start, end) span of the best wake-word match, if any."""
        import difflib
        import re
        target = self.args.wake_word.lower()
        words = [(m.start(), m.end(), m.group(0).lower().strip("'"))
                 for m in re.finditer(r"[a-zA-Z']+", text)]
        best = None
        # single tokens: liberal threshold (aliases count as exact);
        # try the bare token and a de-possessivized variant ("garveys"->"garvey")
        for i, (_, _, w) in enumerate(words):
            w = w.replace("'", "")
            cands = {w}
            if len(w) > 3 and w.endswith("s"):
                cands.add(w[:-1])
            r = max(
                1.0 if c in self._WAKE_ALIASES else
                difflib.SequenceMatcher(None, c, target).ratio()
                for c in cands)
            if r >= self._WAKE_THRESHOLD and (best is None or r > best[0]):
                best = (r, i, i + 1)
        # whisper may split the wake word across tokens ("jar vis"):
        # match concatenated 2-3 word windows at a slightly lower bar
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                c = "".join(w for _, _, w in words[i:i + n])
                r = difflib.SequenceMatcher(None, c, target).ratio()
                if r >= self._WAKE_WINDOW_THRESHOLD and \
                        (best is None or r > best[0]):
                    best = (r, i, i + n)
        if best is not None:
            _, i, j = best
            yield words[i][0], words[j - 1][1]

    def _wake_hit(self, text):
        return next(self._wake_words(text), None) is not None

    def _strip_wake(self, text):
        spans = list(self._wake_words(text))
        if not spans:
            return text
        out, pos = [], 0
        for s, e in spans:
            out.append(text[pos:s])
            pos = e
        out.append(text[pos:])
        return " ".join("".join(out).split()).replace(" ,", ",")

    # ---------- service monitor (proactive alerts) ----------
    def _monitor_loop(self):
        prev = {}
        interval = self.args.monitor_interval
        while not self._stop.is_set():
            try:
                resp = requests.get(self.args.dashboard_url, timeout=10)
                monitors = resp.json().get("monitors", {})
            except Exception:
                monitors = {}
            down, recovered = [], []
            for name, m in monitors.items():
                up = m.get("up")
                if up is False and prev.get(name) is True:
                    down.append(name)
                elif up is True and prev.get(name) is False:
                    recovered.append(name)
                if up is not None:
                    prev[name] = up
            if down:
                names = ", ".join(d.replace("pod:", "") for d in down)
                log(f"ALERT: {names} down")
            if recovered:
                names = ", ".join(d.replace("pod:", "") for d in recovered)
                self._play_file("jarvis-post-alarm-response.wav")
                log(f"RECOVERED: {names}")
            if any(m.get("up") is False for m in monitors.values()):
                self._alarm_loop()
            self._stop.wait(interval)

    def _alarm_loop(self):
        """Replay alarm.wav until every monitor reports UP again."""
        path = Path(__file__).parent.parent / "assets" / "alarm.wav"
        if not path.exists():
            return
        log("ALARM: looping alarm.wav until all monitors recover")
        self._play_file("jarvis-pre-alarm-response.wav")
        while not self._stop.is_set():
            Speaker._play(str(path))
            self._speaking_until = time.time() + self.args.silence_seconds + 0.3
            try:
                monitors = requests.get(
                    self.args.dashboard_url, timeout=10
                ).json().get("monitors", {})
                if not any(m.get("up") is False
                           for m in monitors.values()):
                    return
            except Exception:
                return  # can't verify status — stop wailing
            time.sleep(1.0)

    def _restart_containers(self):
        """Restart all running podman containers; return spoken confirmation."""
        try:
            ps = subprocess.run(
                ["podman", "ps", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=15)
            names = ps.stdout.split()
            if not names:
                return "No containers are running."
            r = subprocess.run(["podman", "restart"] + names,
                               capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                return f"Restart failed. {r.stderr.strip()[:120]}"
            return f"Restarted {len(names)} containers."
        except Exception as e:
            return f"Container restart failed. {e}"

    def _service_summary(self):
        """Speak a summary of dashboard monitor results."""
        try:
            resp = requests.get(self.args.dashboard_url, timeout=10)
            resp.raise_for_status()
            monitors = resp.json().get("monitors", {})
        except Exception as e:
            return f"I could not reach the dashboard. {e}"
        if not monitors:
            return "The dashboard has no monitors configured."
        up_names, down_names = [], []
        for name, m in monitors.items():
            label = name.replace("pod:", "")
            (up_names if m.get("up") is True else
             down_names if m.get("up") is False else []).append(label)
            if m.get("up") is None:
                down_names.append(label + " (unknown)")
        if not down_names:
            return "All services are up and running."
        names = ", ".join(down_names)
        verb = "is" if len(down_names) == 1 else "are"
        return f"The {names} {verb} currently down while the rest is up."

    def _transcribe(self, audio):
        if self.args.stt == "whispercpp":
            return self._transcribe_whispercpp(audio)
        segments, _ = self.whisper.transcribe(
            audio, language=self.args.lang, vad_filter=True,
            beam_size=1, condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    def _transcribe_whispercpp(self, audio):
        """STT via whisper.cpp server (POST /inference); returns stripped text."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
        resp = requests.post(
            self.args.stt_url.rstrip("/") + "/inference",
            files={"file": ("utterance.wav", buf.getvalue(), "audio/wav")},
            data={"response_format": "json",
                  "prompt": self.args.stt_prompt,
                  "temperature": "0.0"},
            timeout=60,
        )
        resp.raise_for_status()
        return " ".join(resp.json().get("text", "").split()).strip()

    # ---------- mic + energy VAD ----------
    def _open_stream(self, p):
        """Open mic stream; auto-pick an input device if --device not given."""
        device = self.args.device
        if device is None:
            for i in range(p.get_device_count()):
                info = p.get_device_info_by_index(i)
                if info["maxInputChannels"] <= 0:
                    continue
                try:
                    s = p.open(format=pyaudio.paInt16, channels=1,
                               rate=SAMPLE_RATE, input=True,
                               frames_per_buffer=CHUNK,
                               input_device_index=i)
                    s.close()
                    device = i
                    break
                except Exception:
                    continue
            if device is None:
                raise RuntimeError("no input device supports 16kHz")
        self._device = device
        return p.open(format=pyaudio.paInt16, channels=1,
                      rate=SAMPLE_RATE, input=True,
                      frames_per_buffer=CHUNK,
                      input_device_index=device)

    def _listen(self):
        p = pyaudio.PyAudio()
        stream = self._open_stream(p)
        log(f"Listening on device {self._device} (Ctrl+C to quit)...")

        voiced = np.zeros(0, dtype=np.int16)
        silence = 0
        in_speech = False
        max_seconds = 20

        try:
            while not self._stop.is_set():
                data = stream.read(CHUNK, exception_on_overflow=False)
                frame = np.frombuffer(data, dtype=np.int16)
                rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))

                if rms > self.args.threshold:
                    if not in_speech:
                        in_speech = True
                        voiced = np.zeros(0, dtype=np.int16)
                    voiced = np.concatenate([voiced, frame])
                    silence = 0
                    if len(voiced) >= max_seconds * SAMPLE_RATE:
                        self._push(voiced)
                        voiced = np.zeros(0, dtype=np.int16)
                        in_speech = False
                elif in_speech:
                    voiced = np.concatenate([voiced, frame])
                    silence += CHUNK
                    if silence >= int(self.args.silence_seconds * SAMPLE_RATE):
                        self._push(voiced)
                        voiced = np.zeros(0, dtype=np.int16)
                        in_speech = False
        except KeyboardInterrupt:
            pass
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()

    def _push(self, pcm):
        audio = (pcm.astype(np.float32) / 32768.0)
        self.utterances.put((audio, time.time()))

    def run(self):
        threading.Thread(target=self._worker, daemon=True).start()
        if self.args.monitor_interval > 0:
            threading.Thread(target=self._monitor_loop, daemon=True).start()
        self._listen()
        self._stop.set()


def main():
    ap = argparse.ArgumentParser(description="Self-contained voice assistant")
    ap.add_argument("--model", default="tiny", help="faster-whisper model size")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--device", type=int, default=None, help="PyAudio input device index")
    ap.add_argument("--threshold", type=float, default=500.0,
                    help="RMS speech threshold (int16 scale); raise if it triggers on noise")
    ap.add_argument("--silence-seconds", type=float, default=0.8,
                    help="Silence needed to end an utterance")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--ollama-model", default="qwen2.5:1.5b")
    ap.add_argument("--tts", choices=["piper", "kokoro"], default="piper")
    ap.add_argument("--tts-voice", default="lessac",
                    help="piper voice name (ignored for kokoro, which uses af_heart)")
    ap.add_argument("--tts-effect", choices=["none", "jarvis"], default="jarvis",
                    help="ffmpeg filter to make TTS sound more synthetic/JARVIS-like")
    ap.add_argument("--stt", choices=["whispercpp", "faster-whisper"],
                    default="whispercpp", help="STT backend")
    ap.add_argument("--stt-url", default="http://localhost:8178",
                    help="whisper.cpp server base URL (POST /inference)")
    ap.add_argument("--stt-prompt",
                    default="Jarvis. Gondar. check status. restart pods. "
                            "restart containers. dashboard. services.",
                    help="initial prompt to bias whisper decoding vocabulary")
    ap.add_argument("--dashboard-url",
                    default="https://gondar.home-security.uk/api",
                    help="dashboard /api endpoint for 'check services' command")
    ap.add_argument("--wake-word", default="jarvis",
                    help="wake word; assistant stays silent until it hears this")
    ap.add_argument("--monitor-interval", type=int, default=60,
                    help="seconds between service-status polls for alerts; 0 disables")
    ap.add_argument("--freeform", action="store_true",
                    help="send unrecognized commands to the LLM (default: whitelist only)")
    ap.add_argument("--sound-gate", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="drop dog/baby cries and other non-speech sounds "
                         "before STT (downloads the AST AudioSet model "
                         "on first use)")
    ap.add_argument("--gate-model", default=SoundGate.DEFAULT_MODEL,
                    help="AudioSet classifier model for --sound-gate")
    ap.add_argument("--gate-threshold", type=float, default=0.25,
                    help="min blocked-class probability for --sound-gate")
    ap.add_argument("--gate-margin", type=float, default=1.5,
                    help="blocked-class prob must exceed speech prob times "
                         "this for --sound-gate")
    args = ap.parse_args()
    Assistant(args).run()


if __name__ == "__main__":
    main()
