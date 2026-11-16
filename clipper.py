#!/usr/bin/env python3
"""
clipper.py - turn YouTube videos (or local files) into vertical shorts.
Free, local alternative to "paste a link and the AI clips it" services.

Needs:     sudo pacman -S yt-dlp ffmpeg python python-pip
Auto mode / captions:  pip install --user --break-system-packages faster-whisper
Optional smarter picks + titles (free, local AI):
           sudo pacman -S ollama && sudo systemctl start ollama && ollama pull llama3.1

AUTO - find the best moments for you
  ./clipper.py "https://youtu.be/XXXX" --auto                 # 5 best clips, captions on
  ./clipper.py "https://youtu.be/XXXX" --auto -k 10 --len 20-45
  ./clipper.py "https://youtu.be/XXXX" --auto --ollama llama3.1
  ./clipper.py "https://youtu.be/XXXX" --auto --dry-run       # just list picks

MANUAL - you choose the times
  ./clipper.py "https://youtu.be/XXXX" -s 1:23 -e 2:05 --captions
  ./clipper.py "https://youtu.be/XXXX" --list clips.txt

Auto mode also writes a clips.txt next to the clips - edit the times and
re-run with --list to fine-tune.

clips.txt format (one clip per line, # for comments):
  1:23  2:05  funny-moment
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

W, H = 1080, 1920                      # vertical output size
OUT_DIR = Path.home() / "Clips"
CACHE = Path.home() / ".cache" / "clipper"
YTDLP_FMT = "bv*[height<=1080]+ba/b[height<=1080]/b"


# ======================================================================
# helpers
# ======================================================================
def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def to_seconds(t: str) -> float:
    s = 0.0
    for p in t.strip().split(":"):
        s = s * 60 + float(p)
    return s


def fmt_ts(sec: float) -> str:
    h, rem = divmod(max(sec, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def short_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


WINDOWS = os.name == "nt"


def ff_filter_path(p) -> str:
    """A file path written inside an ffmpeg filter (subtitles=..., fontsdir=...).
    Windows paths need / and an escaped drive colon: C\\:/Users/..."""
    s_ = str(p).replace("\\", "/")
    return s_.replace(":", "\\:").replace("'", "\\'")


def slug(name: str, n=60) -> str:
    return (re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:n].strip("-")) or "clip"


def run(cmd):
    if subprocess.run(cmd).returncode != 0:
        die("command failed: " + " ".join(map(str, cmd[:4])) + " ...")


# ---------- graphics card for video (NVENC encode / NVDEC decode) ----------
_HW = {}


def gpu_video():
    """True if ffmpeg can encode with the NVIDIA card (checked once).
    Set CLIPPER_CPU_VIDEO=1 to force the CPU."""
    if "ok" not in _HW:
        ok = False
        if not os.environ.get("CLIPPER_CPU_VIDEO") and shutil.which("nvidia-smi"):
            r = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                                "color=black:s=320x240:d=0.3", "-c:v", "h264_nvenc", "-f", "null", "-"],
                               capture_output=True)
            ok = r.returncode == 0
        _HW["ok"] = ok
    return _HW["ok"]


def gpu_cmd(cmd, decode=True):
    """Turn a CPU ffmpeg command into one that decodes + encodes on the graphics card."""
    out, i, first = [], 0, decode
    while i < len(cmd):
        c = cmd[i]
        if c == "-i" and first:                       # decode the main video on the card
            out += ["-hwaccel", "cuda"]
            first = False
        if c == "-c:v" and i + 1 < len(cmd) and cmd[i + 1] == "libx264":
            crf = "20"
            j = i + 2
            while j + 1 < len(cmd) and cmd[j] in ("-preset", "-crf"):
                if cmd[j] == "-crf":
                    crf = cmd[j + 1]
                j += 2
            out += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(int(crf) + 3),
                    "-b:v", "0", "-maxrate", "20M", "-bufsize", "40M"]
            i = j
            continue
        out.append(c)
        i += 1
    return out


def ff(cmd):
    """Run an ffmpeg command on the graphics card if possible, else (or on failure) the CPU."""
    if gpu_video():
        if not _HW.get("nodec") and subprocess.run(gpu_cmd(cmd)).returncode == 0:
            return
        _HW["nodec"] = True                       # card can't decode this: encode only
        if subprocess.run(gpu_cmd(cmd, decode=False)).returncode == 0:
            return
        print("  ! graphics card couldn't make this video - using the CPU instead")
        _HW["ok"] = False
    run(cmd)


def need(tool):
    if not shutil.which(tool):
        win = {"ffmpeg": "Gyan.FFmpeg", "ffprobe": "Gyan.FFmpeg", "yt-dlp": "yt-dlp.yt-dlp", "git": "Git.Git"}
        die(f"'{tool}' not found - install it with: " +
            (f"winget install {win.get(tool, tool)}  (then open a new window)" if WINDOWS
             else f"sudo pacman -S {tool}"))


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "www."))


# ======================================================================
# getting video
# ======================================================================
def download_full(url):
    """Download whole video once into the cache. Returns (path, title)."""
    need("yt-dlp")
    CACHE.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["yt-dlp", "--no-warnings", "--no-playlist",
                        "--print", "%(id)s\t%(title)s", url],
                       capture_output=True, text=True)
    if r.returncode != 0 or "\t" not in r.stdout:
        die("yt-dlp couldn't read that link:\n" + r.stderr.strip())
    vid, title = r.stdout.strip().splitlines()[-1].split("\t", 1)
    path = CACHE / f"{vid}.mp4"
    if path.exists():
        print(f"> using cached download: {title}")
    else:
        print(f"> downloading: {title}")
        run(["yt-dlp", "--no-warnings", "--no-playlist", "-f", YTDLP_FMT,
             "--merge-output-format", "mp4", "-o", str(path), url])
    fetch_chat(url, vid)
    return path, title


def fetch_chat(url, vid):
    """Streams/VODs: grab the chat replay (YouTube live chat, Twitch rechat) once, if there is one.
    Chat going wild is the best highlight signal there is."""
    if any((CACHE / f"{vid}.{k}.json").is_file() for k in ("live_chat", "rechat", "chat")) \
            or (CACHE / f"{vid}.nochat").is_file():
        return
    print("  looking for a chat replay...", flush=True)
    try:
        subprocess.run(["yt-dlp", "-q", "--no-warnings", "--no-playlist", "--skip-download", "--write-subs",
                        "--sub-langs", "live_chat,rechat", "-o", str(CACHE / f"{vid}.%(ext)s"), url],
                       capture_output=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not any((CACHE / f"{vid}.{k}.json").is_file() for k in ("live_chat", "rechat")):
        (CACHE / f"{vid}.nochat").write_text("")


CAP_RES = "scale='if(gt(iw,ih),-2,min(iw,1440))':'if(gt(iw,ih),min(ih,1440),-2)'"


def fetch_segment(src, start, end, tmp: Path) -> Path:
    """Get just start..end as an mp4 in tmp."""
    out = tmp / "raw.mp4"
    if is_url(str(src)):
        need("yt-dlp")
        run(["yt-dlp", "-q", "--no-warnings", "--no-playlist", "-f", YTDLP_FMT,
             "--download-sections", f"*{start}-{end}", "--force-keyframes-at-cuts",
             "--merge-output-format", "mp4", "-o", str(out), str(src)])
    else:
        if not Path(src).exists():
            die(f"file not found: {src}")
        # 4K is far more than a 1080x1920 short needs: cap the short side at 1440
        ff(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
             "-i", str(src), "-vf", CAP_RES, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-c:a", "aac", "-b:a", "192k", str(out)])
    if not out.exists():
        die("couldn't get the video segment")
    return out


# ======================================================================
# transcription
# ======================================================================
# ---------- graphics card (CUDA) ----------
CUDA_PKGS = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]
_CUDA = {}


def preload_cuda():
    """Load NVIDIA's CUDA libraries from the pip packages (no system CUDA needed).
    Returns True if the graphics card can be used."""
    if "ok" in _CUDA:
        return _CUDA["ok"]
    import ctypes
    import glob
    import importlib
    ok = False
    try:
        for mod, pats in (("nvidia.cublas", ["libcublasLt.so.12", "libcublas.so.12"]),
                          ("nvidia.cudnn", ["libcudnn.so.9", "libcudnn_*.so.9"])):
            m = importlib.import_module(mod)
            base = Path(list(m.__path__)[0])
            if WINDOWS:                          # Windows: make the DLL folder findable
                b = base / "bin"
                if b.is_dir():
                    os.add_dll_directory(str(b))
                    os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")
                continue
            for pat in pats:
                for f in sorted(glob.glob(str(base / "lib" / pat))):
                    ctypes.CDLL(f, mode=ctypes.RTLD_GLOBAL)
        import ctranslate2
        ok = ctranslate2.get_cuda_device_count() > 0
    except Exception as e:                       # packages missing / no driver - use the CPU
        _CUDA["why"] = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
    _CUDA["ok"] = ok
    return ok


def gpu_status():
    """What the user needs to know about using the graphics card."""
    st = {"gpu": None, "driver": False, "libs": False, "ready": False, "msg": ""}
    if shutil.which("nvidia-smi"):
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            st["gpu"], st["driver"] = r.stdout.strip().splitlines()[0], True
    import importlib.util
    st["libs"] = all(importlib.util.find_spec(m) is not None for m in ("nvidia.cublas", "nvidia.cudnn")) \
        if importlib.util.find_spec("nvidia") else False
    if st["driver"] and st["libs"]:
        r = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, %r); import clipper; print(clipper.preload_cuda())"
                            % str(Path(__file__).resolve().parent)], capture_output=True, text=True, timeout=120)
        st["ready"] = r.stdout.strip().endswith("True")
    st["video"] = st["driver"] and gpu_video()
    if st["ready"]:
        st["msg"] = (f"✅ Using your graphics card ({st['gpu'].split(',')[0]}) - transcription is much faster"
                     + (", and it makes the clips too" if st["video"] else ""))
    elif not st["driver"]:
        st["msg"] = ("No NVIDIA driver found (nvidia-smi doesn't work). Install the right NVIDIA driver for your card "
                     "first - ask Claude Code: \"install the NVIDIA driver for my GTX 1050 Ti on EndeavourOS\".")
    elif not st["libs"]:
        st["msg"] = f"Driver OK ({st['gpu'].split(',')[0]}). Press Set up GPU to add the CUDA libraries (about 1 GB, no sudo)."
    else:
        st["msg"] = "Driver and CUDA libraries are there but the card didn't start - restart the PC, then try again."
    st["faces"] = face_tracking_available()
    st["msg"] += (" · 🙂 Face tracking on (Zoom follows the speaker)" if st["faces"] else
                  " · Face tracking off - to switch it on run:  pip install --user --break-system-packages "
                  "opencv-python-headless  (Windows: py -3 -m pip install --user opencv-python-headless)")
    return st


def speed_test(path, model="small.en", secs=60):
    """Transcribe the first minute on the CPU and on the graphics card and compare."""
    import time as _t
    from faster_whisper import WhisperModel
    audio = load_audio(Path(path))[: 16000 * secs]
    res = {}
    for dev in ("cpu", "cuda"):
        if dev == "cuda" and not preload_cuda():
            print(f"  graphics card: not available ({_CUDA.get('why', 'not set up')})")
            continue
        try:
            t = _t.time()
            m = WhisperModel(model, device=dev, compute_type="int8", cpu_threads=os.cpu_count() or 4)
            segs, _ = m.transcribe(audio, language="en" if model.endswith(".en") else None)
            n = sum(1 for _ in segs)
            res[dev] = _t.time() - t
            print(f"  {dev:4s}: {res[dev]:.1f}s for {secs}s of audio ({n} segments)")
        except Exception as e:
            print(f"  {dev}: failed - {str(e).splitlines()[0][:150]}")
    if "cpu" in res and "cuda" in res:
        print(f"  → graphics card is {res['cpu'] / res['cuda']:.1f}x faster")
    return res


def load_audio(path: Path, sr=16000):
    """Decode audio with ffmpeg ourselves (avoids faster-whisper's PyAV decoder,
    which breaks with newer PyAV versions)."""
    import numpy as np
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-vn",
                        "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        die("couldn't read the audio:\n" + r.stderr.decode(errors="replace").strip())
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def transcribe(path: Path, model_size: str, cache=True):
    """-> list of {start,end,text,words:[(s,e,w)]}"""
    if not has_audio(path):
        print("  (no sound track)")
        return []
    key = None
    if cache:
        st = path.stat()
        key = hashlib.md5(f"{path.resolve()}|{st.st_size}|{st.st_mtime}|{model_size}"
                          .encode()).hexdigest()
        cfile = CACHE / f"{key}.json"
        if cfile.exists():
            print("  using cached transcript")
            return json.loads(cfile.read_text())
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("faster-whisper not installed:\n"
            "  pip install --user --break-system-packages faster-whisper")

    print(f"  transcribing with whisper '{model_size}' (first run downloads the model)...")
    audio = load_audio(path)
    gpu = preload_cuda()

    def attempt(device):
        import os
        model = WhisperModel(model_size, device=device, compute_type="int8",
                             cpu_threads=os.cpu_count() or 4)
        segs, info = model.transcribe(audio, word_timestamps=True, vad_filter=True,
                                      language="en" if model_size.endswith(".en") else None)
        res = []
        for s in segs:
            res.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                        "words": [(w.start, w.end, w.word.strip()) for w in (s.words or [])],
                        "conf": round(float(getattr(s, "avg_logprob", 0) or 0), 3),
                        "nospeech": round(float(getattr(s, "no_speech_prob", 0) or 0), 3)})
            print(f"\r  {short_ts(s.end)} / {short_ts(info.duration)}", end="", flush=True)
        print()
        return res

    try:
        out = attempt("cuda" if gpu else "cpu")
    except Exception as e:
        print(f"\n  ! graphics card didn't work ({str(e).splitlines()[0][:120]})")
        print("  retrying on the CPU (slower but works everywhere)...")
        out = attempt("cpu")
    if key:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / f"{key}.json").write_text(json.dumps(out))
    return out


# ======================================================================
# finding the best moments
# ======================================================================
HOP = 0.5  # seconds per loudness sample

HOOK = re.compile(
    r"^(so+|okay|ok|listen|look|here'?s|the (thing|truth|problem|secret|reason)|"
    r"you (won'?t|will never|have to|need to|gotta)|why|how|what if|nobody|never|"
    r"this is|i (can'?t|cannot|never)|the (best|worst|craziest|biggest)|"
    r"let me tell you|imagine|did you know|hold on|wait)\b", re.I)
HYPE = re.compile(
    r"\b(oh my god|omg|no way|what the|insane|crazy|unbelievable|wow|holy|bro|dude|"
    r"wait|never|biggest|worst|best|secret|million|billion|thousand|money|"
    r"literally|actually|shocking|honestly|scared|died|lost|won|first time|"
    r"f+u+c+k\w*|shit\w*|damn)\b|[\[(](laugh\w*|applause|cheer\w*)|[$£]\d", re.I)


FUNNY = re.compile(
    r"\b(ha(ha)+|haha\w*|lol|lmao|joke|joking|funny|hilarious|i'?m dead|dead ass|bruh|nah+|"
    r"what\?!|are you serious|you'?re (lying|joking)|stop it|oh no|shut up|plot twist|turns out|"
    r"out of nowhere|i swear|no cap|deadass|embarrass\w*|awkward)\b|[\[(](laugh\w*|chuckl\w*)", re.I)
OPINION = re.compile(
    r"\b(overrated|underrated|trash|goat|the best|the worst|hate|love|honestly|the truth is|"
    r"unpopular opinion|hot take|nobody (talks|tells)|i don'?t care|disagree|wrong|stupid|"
    r"ridiculous|terrible|amazing|insane|legend\w*|never again|biggest mistake)\b", re.I)
FILLER = re.compile(r"\b(um+|uh+|erm+|you know|i mean|sort of|kind of|basically|"
                    r"like,|in terms of|at the end of the day|going forward|to be fair)\b", re.I)


def audio_envelope(path: Path, step=0.02):
    """Loudness every 20 ms (linear), cached - the raw material for laugh detection."""
    import numpy as np
    st = path.stat()
    key = hashlib.md5(f"{path.resolve()}|{st.st_size}|{st.st_mtime}|{step}".encode()).hexdigest()
    cf = CACHE / "env" / f"{key}.npy"
    if cf.is_file():
        try:
            return np.load(cf)
        except (OSError, ValueError):
            pass
    sr = 8000
    n = int(sr * step)
    p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", str(path), "-vn",
                          "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    vals, buf = [], b""
    while True:
        chunk = p.stdout.read(n * 2 * 4000)
        if not chunk:
            break
        buf += chunk
        k = len(buf) // (n * 2)
        if k:
            a = np.frombuffer(buf[:k * n * 2], dtype=np.int16).astype(np.float32).reshape(k, n)
            vals.append(np.sqrt((a ** 2).mean(axis=1)))
            buf = buf[k * n * 2:]
    p.wait()
    env = np.concatenate(vals) if vals else np.zeros(1, dtype=np.float32)
    try:
        cf.parent.mkdir(parents=True, exist_ok=True)
        np.save(cf, env.astype(np.float32))
    except OSError:
        pass
    return env


def reaction_curve(path: Path, segs):
    """0..1 every HOP seconds: how much it sounds like laughter or a crowd reacting -
    loud sound that ISN'T words, bursting on and off (ha-ha-ha) rather than steady music.
    Plus laughs that the transcript wrote down (haha, [laughter])."""
    import numpy as np
    if not has_audio(path):
        return np.zeros(max(1, int(duration_of(path) / HOP) + 1))
    step = 0.02
    env = audio_envelope(path, step)
    nf = len(env)
    covered = np.zeros(nf, dtype=bool)                  # frames where someone is saying words
    for s_ in segs:
        for w in s_.get("words") or []:
            if FUNNY.search(str(w[2])):
                continue                                # 'haha' counts as laughing, not talking
            a, b = int((w[0] - 0.05) / step), int((w[1] + 0.05) / step) + 1
            covered[max(0, a):min(nf, b)] = True
    db = 20 * np.log10(env + 1.0)
    speech_level = float(np.median(db[covered])) if covered.any() else float(np.median(db))
    nh = int(nf * step / HOP) + 1
    out = np.zeros(nh)
    win = int(1.0 / step)                               # look at 1 s around each half-second
    freqs = np.fft.rfftfreq(win, step)
    band = (freqs >= 3) & (freqs <= 8)                  # 'ha-ha-ha' rate
    allb = (freqs >= 1) & (freqs <= 20)
    for h in range(nh):
        c0 = int(h * HOP / step) + int(HOP / step / 2) - win // 2
        a, b = max(0, c0), min(nf, c0 + win)
        if b - a < win // 2:
            continue
        if covered[a:b].mean() > 0.4:
            continue                                    # mostly words - not a reaction
        level = float(np.percentile(db[a:b], 80)) - speech_level   # the bursts, not the gaps between
        if level < -8:
            continue                                    # too quiet to be a laugh
        x = env[a:b] / (env[a:b].mean() + 1e-6)
        depth = float(x.std())
        if len(x) < win:
            x = np.pad(x, (0, win - len(x)), constant_values=1.0)
        spec = np.abs(np.fft.rfft(x - 1.0)) ** 2
        ratio = float(spec[band].sum() / (spec[allb].sum() + 1e-9))
        out[h] = min(1.0, max(0.0, (level + 8) / 12) * min(1.0, depth / 0.6) * min(1.0, ratio / 0.5))
    sm = out.copy()                                     # a real laugh lasts: drop one-off blips
    for h in range(nh):
        nb = max(out[h - 1] if h > 0 else 0, out[h + 1] if h + 1 < nh else 0)
        sm[h] = min(out[h], nb)
    out = sm
    for s_ in segs:                                     # laughs the transcript caught
        for w in s_.get("words") or []:
            if FUNNY.search(str(w[2])) and re.search(r"ha|lol|lmao|laugh|chuckl", str(w[2]), re.I):
                h = int(w[0] / HOP)
                if 0 <= h < nh:
                    out[h] = max(out[h], 0.8)
    return out


CHAT_HYPE = re.compile(r"(lol|lmao|lmfao|rofl|lul|kekw|omegalul|icant|💀|😂|🤣|😭|^w+$|\bw+\b|"
                       r"clip (it|that)|no way|wtf|pog\w*|holy|bro+|insane|hahah?)", re.I)
CHAT_OFFSET_KEYS = ("videoOffsetTimeMsec", "content_offset_seconds", "contentOffsetSeconds", "offset_seconds")


def chat_files(src: Path):
    src = Path(src)
    names = [src.with_name(f"{src.stem}.{k}.json") for k in ("live_chat", "rechat", "chat")]
    return [f for f in names if f.is_file()]


def load_chat(src: Path):
    """[(seconds into the video, message text)] from a YouTube live-chat or Twitch rechat replay file."""
    msgs = []

    def text_of(o):
        out = []
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("text", "body") and isinstance(v, str):
                    out.append(v)
                elif k == "shortcuts" and isinstance(v, list) and v:
                    out.append(str(v[0]))
                elif isinstance(v, (dict, list)):
                    out += text_of(v)
        elif isinstance(o, list):
            for v in o:
                out += text_of(v)
        return out

    def walk(o):
        if isinstance(o, dict):
            for k in CHAT_OFFSET_KEYS:
                if k in o:
                    try:
                        t = float(o[k]) / (1000 if k == "videoOffsetTimeMsec" else 1)
                    except (TypeError, ValueError):
                        break
                    msgs.append((t, " ".join(text_of(o))))
                    return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for f in chat_files(src):
        txt = f.read_text(errors="ignore")
        try:
            walk(json.loads(txt))
        except ValueError:                         # YouTube: one JSON object per line
            for line in txt.splitlines():
                if line.strip().startswith("{"):
                    try:
                        walk(json.loads(line))
                    except ValueError:
                        pass
    return sorted(m for m in msgs if m[0] >= 0)


def chat_curve(src: Path, n):
    """0..1 every HOP seconds: how much chat exploded about this moment (shifted back ~4 s,
    because chat reacts after the moment happens). None if there's no chat replay."""
    import numpy as np
    msgs = load_chat(src)
    if len(msgs) < 30:
        return None
    cnt = np.zeros(n)
    for t, m in msgs:
        h = int(max(0.0, t - 4.0) / HOP)
        if h < n:
            cnt[h] += 2.0 if CHAT_HYPE.search(m) else 1.0
    k = int(4 / HOP)                               # smooth over ~4 s
    sm = np.convolve(cnt, np.ones(k) / k, mode="same")
    base = np.array([np.median(sm[max(0, i - 120):i + 120]) for i in range(0, n, 20)])
    base = np.repeat(base, 20)[:n]
    z = (sm - base) / (np.sqrt(base + 0.25) + 0.5)
    z = np.clip(z, 0, None)
    top = np.percentile(z[z > 0], 98) if (z > 0).any() else 0
    print(f"  chat replay: {len(msgs)} messages")
    return np.clip(z / top, 0, 1) if top > 0 else np.zeros(n)


FILLER_WORD = re.compile(r"^(u+m+|u+h+|e+r+m*|h+m+|a+h+)[.,!?…]*$", re.I)


def plan_pause_cuts(words_rel, dur, env=None, step=0.02):
    """Which parts of a clip to keep: drop silent pauses (> 0.6 s) and 'um/uh'.
    Pauses that are loud (laughter, a reaction) are kept. Returns [(a, b)] or None."""
    import numpy as np
    w = sorted([x for x in words_rel if x[1] > x[0]], key=lambda x: x[0])
    if len(w) < 3:
        return None
    speech_db = None
    if env is not None and len(env):
        db = 20 * np.log10(env + 1.0)
        idx = [int(x[0] / step) for x in w if int(x[0] / step) < len(db)]
        speech_db = float(np.median(db[idx])) if idx else None
    cut = []
    for a, b in zip(w, w[1:]):
        gap0, gap1 = a[1], b[0]
        if gap1 - gap0 > 0.6:
            loud = False
            if speech_db is not None:
                seg = db[int(gap0 / step):int(gap1 / step) + 1]
                loud = len(seg) and float(np.percentile(seg, 80)) > speech_db - 10
            if not loud:
                cut.append((gap0 + 0.2, gap1 - 0.15))
    for x in w:
        if FILLER_WORD.match(str(x[2]).strip()):
            cut.append((x[0] - 0.03, x[1] + 0.03))
    cut = sorted((max(0.0, a), min(dur, b)) for a, b in cut if b - a > 0.12)
    if sum(b - a for a, b in cut) < 0.4:
        return None
    keep, pos = [], 0.0
    for a, b in cut:
        if a > pos:
            keep.append((pos, a))
        pos = max(pos, b)
    if pos < dur:
        keep.append((pos, dur))
    return [(round(a, 3), round(b, 3)) for a, b in keep if b - a > 0.05]


def remap_time(t, keep):
    """Clip time before cutting -> after cutting (None if that moment was cut out)."""
    done = 0.0
    for a, b in keep:
        if t < a:
            return None
        if t <= b:
            return done + (t - a)
        done += b - a
    return None


def audio_envelope_tmp(path: Path, step=0.02):
    """Like audio_envelope but not cached (for temporary clip files)."""
    import numpy as np
    sr = 8000
    n = int(sr * step)
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
                        "-f", "s16le", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)
    k = len(a) // n
    return np.sqrt((a[:k * n].reshape(k, n) ** 2).mean(axis=1)) if k else None


def apply_cuts(raw: Path, keep, tmp: Path) -> Path:
    out = tmp / "cut.mp4"
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)
    ff(["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw),
        "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
        "-af", f"aselect='{expr}',asetpts=N/SR/TB",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(out)])
    return out


def loudness_curve(path: Path):
    """Loudness (z-scored dB) every HOP seconds, via ffmpeg -> numpy."""
    import numpy as np
    if not has_audio(path):
        return np.zeros(max(1, int(duration_of(path) / HOP) + 1))
    sr = 8000
    n = int(sr * HOP)
    p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", str(path), "-vn",
                          "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         stdout=subprocess.PIPE)
    vals, buf = [], b""
    while True:
        chunk = p.stdout.read(n * 2 * 1000)
        if not chunk:
            break
        buf += chunk
        k = len(buf) // (n * 2)
        if k:
            a = np.frombuffer(buf[:k * n * 2], dtype=np.int16).astype(np.float32).reshape(k, n)
            vals.append(20 * np.log10(np.sqrt((a ** 2).mean(axis=1)) + 1.0))
            buf = buf[k * n * 2:]
    p.wait()
    if not vals:
        return np.zeros(1)
    db = np.concatenate(vals)
    voiced = db[db > 20]                       # ignore pure silence when normalising
    ref = voiced if len(voiced) > 10 else db
    return (db - np.median(ref)) / (ref.std() + 1e-6)


def split_sentences(segs):
    """Whisper segments can hold several sentences (e.g. an answer AND the next question).
    Split them on sentence ends using the word timings, so clips can end between them."""
    out = []
    end_re = re.compile(r"[.!?…][\"')\]]*$")
    for s in segs:
        ws = s.get("words") or []
        if len(ws) < 4:
            out.append(s)
            continue
        pieces, cur = [], []
        for k, w in enumerate(ws):
            cur.append(w)
            if end_re.search(str(w[2]).strip()) and len(cur) >= 2 and len(ws) - k - 1 >= 2:
                pieces.append(cur)
                cur = []
        if cur:
            pieces.append(cur)
        for k, ws_ in enumerate(pieces):
            out.append(dict(s, words=ws_, text=" ".join(str(x[2]).strip() for x in ws_),
                            start=s["start"] if k == 0 else ws_[0][0],
                            end=s["end"] if k == len(pieces) - 1 else ws_[-1][1]))
    return out


def is_question(t):
    return bool(re.search(r"\?[\"')\]]*\s*$", (t or "").strip()))


def build_candidates(segs, min_len, max_len, target, qa=True):
    """Every sentence-aligned window between min_len and max_len seconds.
    Interview-aware: never end on a question, and don't end part-way through
    the answer to a question asked inside the clip."""
    cands = []
    ends_sentence = lambda t: bool(re.search(r"[.!?…][\"')\]]*\s*$", t.strip()))
    stretch = min(10.0, max_len * 0.25)            # allowed overrun to let a sentence finish
    n = len(segs)

    def answer_done(j):
        """After segs[j], is the answer over? (a pause, a new question, or the end)"""
        if j + 1 >= n:
            return True
        gap = segs[j + 1]["start"] - segs[j]["end"]
        return gap > 1.0 or is_question(segs[j + 1]["text"])

    for i in range(n):
        best, best_end = None, None
        last_q = None                              # last question asked inside the window
        for j in range(i, n):
            if j > i and segs[j]["start"] - segs[j - 1]["end"] > 4.0:
                break                              # long silence - don't bridge it
            if j > i and is_question(segs[j]["text"]):
                last_q = j
            dur = segs[j]["end"] - segs[i]["start"]
            if dur > max_len + stretch:
                break
            if dur < min_len:
                continue
            if qa and is_question(segs[j]["text"]):
                continue                           # never end on a question (answer would be cut)
            cut_answer = 0.0
            if qa and last_q is not None and not answer_done(j):
                cut_answer = 8.0                   # mid-answer to a question asked in the clip
            if ends_sentence(segs[j]["text"]):
                over = max(0.0, dur - max_len) * 3     # overrunning is allowed but discouraged
                cost = abs(dur - target) + over + cut_answer
                if best_end is None or cost < best_end[2]:
                    best_end = (j, dur, cost)
            if dur <= max_len and not cut_answer and (best is None or abs(dur - target) < abs(best[1] - target)):
                best = (j, dur)
        if best_end:
            best = best_end[:2]                    # a finished sentence beats a closer length
        if best:
            j, _ = best
            start, end = clean_bounds(segs, i, j)
            cands.append({"i": i, "j": j, "start": start, "end": end,
                          "text": " ".join(s["text"] for s in segs[i:j + 1])})
    return cands


def first_word_start(seg):
    return seg["words"][0][0] if seg["words"] else seg["start"]


def last_word_end(seg):
    return seg["words"][-1][1] if seg["words"] else seg["end"]


def clean_bounds(segs, i, j, pre=0.45, post=1.1):
    """Cut on word timings with breathing room, without catching the
    tail of the previous word or the start of the next one."""
    s = first_word_start(segs[i])
    start = s - pre
    if i > 0:
        prev = last_word_end(segs[i - 1])
        start = max(start, prev + (s - prev) * 0.5) if prev < s else s - 0.05
    e = last_word_end(segs[j])
    end = e + post
    if j + 1 < len(segs):
        nxt = first_word_start(segs[j + 1])
        end = min(end, e + (nxt - e) * 0.7) if nxt > e else e + 0.15
        end = max(end, min(e + 0.35, nxt - 0.06))   # whisper ends words a touch early,
                                                     # but don't catch the next sentence's first word
    return max(0.0, start), end


def score_candidates(cands, segs, loud, laugh=None, words=True):
    import numpy as np
    feats = []
    if laugh is None:
        laugh = np.zeros(len(loud))
    for c in cands:
        a, b = int(c["start"] / HOP), max(int(c["end"] / HOP), int(c["start"] / HOP) + 1)
        w = loud[a:b] if a < len(loud) else np.zeros(1)
        w = w if len(w) else np.zeros(1)
        dur = c["end"] - c["start"]
        first = segs[c["i"]]["text"]
        last = segs[c["j"]]["text"]
        nwords = sum(len(s["words"]) or len(s["text"].split()) for s in segs[c["i"]:c["j"] + 1])
        feats.append([
            float(np.mean(np.sort(w)[-max(1, len(w) // 4):])),      # loud moments
            float((w > 1.5).sum()) / dur * 10,                       # spikes / reactions
            1.0 if (HOOK.search(first) or "?" in first) else 0.0,    # strong opening
            len(HYPE.findall(c["text"])) / dur * 30,                 # hype words
            nwords / dur,                                            # energy of speech
            1.0 if re.search(r"[.!?]$", last.strip()) else 0.0,      # clean ending
            1.0 if (c["i"] == 0 or re.search(r"[.!?]\W*$", segs[c["i"] - 1]["text"])) else 0.0,
                                                                     # starts a new sentence
            1.0 if re.match(r"(and|but|or|because|which|that|then)\b", first, re.I) else 0.0,
                                                                     # starts mid-thought
        ] + (content_feats(c, segs, laugh, dur, nwords) if words
             else [content_feats(c, segs, laugh, dur, nwords)[0], 0, 0, 0, 0, 0]))
    F = np.array(feats)
    Z = (F - F.mean(axis=0)) / (F.std(axis=0) + 1e-6)
    #                 loud spike hook hype rate end new mid | laugh funny opinion filler ramble q&a
    weights = np.array([1.0, 0.8, 1.2, 0.9, 0.5, 0.6, 1.0, -0.8, 1.5, 0.9, 0.6, -0.7, -0.5, 0.4])
    for c, z, f in zip(cands, Z @ weights, F):
        c["score"] = float(z)
        tags = []
        if f[8] >= 0.45:
            tags.append("😂 laughter")
        if f[9] > 0:
            tags.append("🤣 funny line")
        if f[10] > 0:
            tags.append("🔥 strong opinion")
        if f[2] > 0 and f[11] <= 0.12:
            tags.append("🪝 hook opening")
        if f[11] > 0.12:
            tags.append("😴 lots of filler")
        c["tags"] = tags
    return sorted(cands, key=lambda c: c["score"], reverse=True)


def content_feats(c, segs, laugh, dur, nwords):
    """What makes a clip worth watching beyond loudness: laughs, jokes, opinions -
    and what makes it boring: filler words and long rambling sentences."""
    text = c["text"]
    a = int((first_word_start(segs[c["i"]]) + 0.75) / HOP)   # a laugh BEFORE the clip's first line isn't its laugh
    b = int((c["end"] + 1.5) / HOP) + 1                 # a laugh just AFTER the line counts too
    lw = laugh[a:b] if a < len(laugh) else []
    lmax = float(max(lw)) if len(lw) else 0.0
    sents = [x for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
    avg_sent = nwords / max(1, len(sents))
    qa = 0.0
    for k in range(c["i"], c["j"]):                     # a question answered inside the clip
        if is_question(segs[k]["text"]) and not is_question(segs[k + 1]["text"]):
            qa = 1.0
            break
    return [lmax,
            min(3, len(FUNNY.findall(text))) / max(dur, 1) * 30,
            min(3, len(OPINION.findall(text))) / max(dur, 1) * 30,
            len(FILLER.findall(text)) / max(1, nwords) * 5,
            max(0.0, avg_sent - 22) / 10,               # long winding sentences
            qa]


def add_chat_bonus(src, cands, n=None):
    """Stream replays: boost moments where chat went wild."""
    try:
        if not chat_files(Path(src)):
            return
        n = n or int(duration_of(Path(src)) / HOP) + 1
        chat = chat_curve(Path(src), n)
    except Exception as e:                         # noqa - never let chat stop clipping
        print(f"  (chat skipped: {e})")
        return
    if chat is None:
        return
    hot = 0
    for c in cands:
        a, b = int(c["start"] / HOP), int(c["end"] / HOP) + 1
        peak = float(chat[a:b].max()) if a < len(chat) and b > a else 0.0
        c["score"] += 3.0 * peak
        if peak >= 0.5:
            c.setdefault("tags", []).insert(0, "💬 chat went wild")
            hot += 1
    cands.sort(key=lambda c: c["score"], reverse=True)
    print(f"  {hot} possible clips where chat went wild")


def extend_for_laugh(c, segs, laugh):
    """If people laugh right after the clip's last line, keep the laugh in."""
    a = int(c["end"] / HOP)
    tail = [h for h in range(a, min(len(laugh), a + 6)) if laugh[h] >= 0.45]
    if not tail:
        return
    new_end = (tail[-1] + 1) * HOP + 0.2
    j = c["j"]
    if j + 1 < len(segs):
        new_end = min(new_end, first_word_start(segs[j + 1]) - 0.06)
    if new_end > c["end"]:
        c["end"] = round(min(new_end, c["end"] + 3.0), 2)


def pick(cands, k, gap=5.0, fill=True):
    chosen = []
    for c in cands:
        if all(c["end"] + gap <= d["start"] or c["start"] >= d["end"] + gap for d in chosen):
            chosen.append(c)
        if len(chosen) == k:
            break
    if fill and len(chosen) < k:
        # short video: add variations that overlap but open on a different line
        for c in cands:
            if len(chosen) == k:
                break
            if c in chosen:
                continue
            if all(abs(c["start"] - d["start"]) >= 4.0 and c["i"] != d["i"] for d in chosen):
                c["variation"] = True
                chosen.append(c)
        n = sum(1 for c in chosen if c.get("variation"))
        if n:
            print(f"  (video is short - {n} clip(s) overlap but start on a different line)")
    return chosen


# ======================================================================
# optional AI brain: local Ollama, or Claude with your own API key
# ======================================================================
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(d):
    SETTINGS_FILE.write_text(json.dumps(d, indent=2))
    try:
        SETTINGS_FILE.chmod(0o600)                  # the API key stays private
    except OSError:
        pass


def ai_name(ollama=None):
    st = load_settings()
    if ollama:
        return f"Ollama ({ollama})"
    if st.get("ai") == "claude" and st.get("claude_key"):
        return f"Claude ({st.get('claude_model') or 'claude-haiku-5-5'})"
    if st.get("ai") == "ollama" and st.get("ollama_model"):
        return f"Ollama ({st['ollama_model']})"
    return None


def ai_json(prompt, ollama=None, timeout=600):
    """Ask the configured AI for a JSON answer. None if no AI is set up or it fails."""
    st = load_settings()
    if ollama:
        provider, model = "ollama", ollama
    elif st.get("ai") == "claude" and st.get("claude_key"):
        provider, model = "claude", st.get("claude_model") or "claude-haiku-5-5"
    elif st.get("ai") == "ollama" and st.get("ollama_model"):
        provider, model = "ollama", st["ollama_model"]
    else:
        return None
    try:
        if provider == "claude":
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps({"model": model, "max_tokens": 2048,
                                 "messages": [{"role": "user", "content": prompt}]}).encode(),
                headers={"x-api-key": st["claude_key"], "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
            text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        else:
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                                 "format": "json"}).encode(),
                headers={"Content-Type": "application/json"})
            text = json.loads(urllib.request.urlopen(req, timeout=timeout).read())["response"]
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m[0]) if m else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        print(f"  ! AI ({provider}) said no: {e.code} {detail}")
    except Exception as e:
        print(f"  ! AI ({provider}) didn't answer: {e}")
    return None


def ollama_pick(cands, k, model):
    """Let a local LLM choose the k most viral candidates and title them."""
    pool = cands[:max(k * 4, 12)]
    listing = "\n\n".join(f"[{n}] ({c['end'] - c['start']:.0f}s) {c['text'][:900]}"
                          for n, c in enumerate(pool))
    prompt = (
        f"You are an expert short-form video editor. Below are candidate clips from a "
        f"long video. Pick the {k} that would perform best as TikTok/YouTube Shorts: "
        f"strong hook in the first seconds, emotion or surprise, a complete thought, "
        f"understandable without context. Give each a short catchy title (max 8 words).\n"
        f'Reply ONLY with JSON: {{"picks":[{{"id":0,"title":"..."}}]}}\n\n{listing}')
    try:
        print(f"  asking AI - {ai_name(model)} - to pick...")
        data = ai_json(prompt, ollama=model)
        if not data:
            return None
        picks = data["picks"]
        out = []
        for p in picks:
            idx = int(p["id"])
            if 0 <= idx < len(pool) and pool[idx] not in out:
                pool[idx]["title"] = str(p.get("title", "")).strip()
                out.append(pool[idx])
        out = pick(out, k, gap=0)               # drop overlaps
        if out:
            return out
        print("  ! the AI gave no usable picks, using built-in scoring")
    except Exception as e:
        print(f"  ! AI picking failed ({e}), using built-in scoring")
    return None


# ======================================================================
# captions + rendering
# ======================================================================
FONTS_DIR = Path(__file__).resolve().parent / "fonts"

# caption looks. "accent" = which colour "match video colours" replaces
CAPTION_PRESETS = {
    "highlight": dict(label="Word highlight", font="DejaVu Sans", size=88, text="#FFFFFF",
                      highlight="#FFE14D", outline="#000000", outline_w=7, shadow=2,
                      box=False, upper=True, pos="lower", words=3, pop=True, glow=False,
                      accent="highlight"),
    "classic":   dict(label="Classic", font="DejaVu Sans", size=86, text="#FFFFFF",
                      highlight="", outline="#000000", outline_w=6, shadow=2,
                      box=False, upper=True, pos="lower", words=3, pop=False, glow=False,
                      accent="outline"),
    "box":       dict(label="Black box", font="DejaVu Sans", size=72, text="#FFFFFF",
                      highlight="", outline="#000000", outline_w=16, shadow=0,
                      box=True, upper=False, pos="lower", words=4, pop=False, glow=False,
                      accent="box"),
    "neon":      dict(label="Neon glow", font="DejaVu Sans", size=84, text="#FFFFFF",
                      highlight="", outline="#00E5FF", outline_w=5, shadow=0,
                      box=False, upper=True, pos="lower", words=3, pop=True, glow=True,
                      accent="outline"),
    "minimal":   dict(label="Minimal", font="DejaVu Sans", size=64, text="#FFFFFF",
                      highlight="", outline="#000000", outline_w=3, shadow=1,
                      box=False, upper=False, pos="bottom", words=5, pop=False, glow=False,
                      accent="highlight"),
    "bold-pop":  dict(label="Bold pop", font="DejaVu Sans", size=110, text="#FFE14D",
                      highlight="", outline="#000000", outline_w=9, shadow=3,
                      box=False, upper=True, pos="middle", words=2, pop=True, glow=False,
                      accent="text"),
}
POSITIONS = {  # alignment, margin from edge
    "top": (8, 480), "middle": (5, 0), "lower": (2, 520), "bottom": (2, 230),
}


def ass_col(hexcol, alpha=0):
    h = (hexcol or "#FFFFFF").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = "FFFFFF"
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_inline(hexcol):
    h = (hexcol or "#FFFFFF").lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def shade(hexcol, f):
    h = hexcol.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(c * f))) for c in (r, g, b))


def accent_color(video: Path):
    """Most eye-catching colour in the clip (for 'match video colours')."""
    import colorsys
    import numpy as np
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf",
                        "fps=2,scale=64:36", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True)
    px = np.frombuffer(r.stdout, dtype=np.uint8).reshape(-1, 3).astype(float) / 255
    if len(px) < 50:
        return None
    mx, mn = px.max(axis=1), px.min(axis=1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    keep = (sat > 0.35) & (mx > 0.35)
    if keep.mean() < 0.01:
        return None                                   # black & white footage
    sel = px[keep]
    hues = np.array([colorsys.rgb_to_hsv(*p)[0] for p in sel[:: max(1, len(sel) // 4000)]])
    w = (sat[keep] * mx[keep])[:: max(1, len(sel) // 4000)]
    hist, edges = np.histogram(hues, bins=24, range=(0, 1), weights=w)
    b = hist.argmax()
    hue = (edges[b] + edges[b + 1]) / 2
    rr, gg, bb = colorsys.hsv_to_rgb(hue, 0.85, 1.0)  # punchy, readable version
    return "#%02X%02X%02X" % (int(rr * 255), int(gg * 255), int(bb * 255))


def analyse_footage(video: Path):
    """Brightness, colourfulness and busyness of the clip (0..1 each)."""
    import numpy as np
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-vf",
                        "fps=2,scale=96:54", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    if a.size < 96 * 54 * 3:
        return None
    f = a[: a.size // (96 * 54 * 3) * 96 * 54 * 3].reshape(-1, 54, 96, 3).astype(float) / 255
    lum = (0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2])
    mx, mn = f.max(axis=-1), f.min(axis=-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    busy = (np.abs(np.diff(lum, axis=1)).mean() + np.abs(np.diff(lum, axis=2)).mean())
    return {"lum": float(lum.mean()), "sat": float(sat.mean()), "busy": float(min(1.0, busy * 6))}


def auto_style(video, words=None, duration=None):
    """-> (preset, match_colours, words_per_line, reason)"""
    info = analyse_footage(video) if video is not None else None
    wps = (len(words) / duration) if words and duration else 2.5
    per = 2 if wps > 3.2 else 3
    if not info:
        return "highlight", False, per, "default"
    if info["busy"] > 0.55 or info["lum"] > 0.62:
        return "box", True, per + 1, "busy/bright footage - box keeps it readable"
    if info["lum"] < 0.3 and info["sat"] > 0.22:
        return "neon", True, per, "dark, colourful footage - neon glow in its colour"
    if info["sat"] > 0.18:
        return "highlight", True, per, "colourful footage - highlight in its colour"
    return "highlight", False, per, "plain footage - classic yellow highlight"


def resolve_style(args, video=None, words=None, duration=None):
    preset = getattr(args, "style", None) or "auto"
    match = getattr(args, "match_colors", False)
    auto_words = None
    if preset == "auto":
        preset, match, auto_words, why = auto_style(video, words, duration)
        print(f"  auto style: {CAPTION_PRESETS[preset]['label']} ({why})")
    st = dict(CAPTION_PRESETS.get(preset, CAPTION_PRESETS["highlight"]))
    st["preset"] = preset
    if auto_words:
        st["words"] = auto_words
    camp = getattr(args, "campaign_data", None)
    if camp and isinstance(camp.get("caption_style"), dict):
        st.update(camp["caption_style"])
    if getattr(args, "style", None) != "auto":       # manual tweaks only apply to a chosen look
        for k, v in (getattr(args, "cap", None) or {}).items():
            if v is not None and k in st:
                st[k] = v
        if getattr(args, "words", None):
            st["words"] = args.words
    if match and video is not None:
        acc = accent_color(video)
        if acc:
            print(f"  caption colour matched to video: {acc}")
            slot = st.get("accent", "highlight")
            if slot == "box":
                st["outline"] = shade(acc, 0.32)
            elif slot == "outline":
                st["outline"] = acc
            elif slot == "text":
                st["text"] = acc
            else:
                st["highlight"] = acc
    return st


def build_ass(words, offset, tmp: Path, style=None, overlay=None, duration=0.0):
    """words: [(start,end,text)] in source time; offset = clip start.
    style: dict from resolve_style(). overlay: fixed text at the top for the whole clip."""
    st = style or dict(CAPTION_PRESETS["highlight"])
    words = [w for w in words if w[2]]
    if not words and not overlay:
        return None
    align, margin = POSITIONS.get(st.get("pos"), POSITIONS["lower"])
    box = bool(st.get("box"))
    out_c = ass_col(st["outline"], 0x30 if box else 0)
    font = str(st.get("font") or "DejaVu Sans").replace(",", " ")
    ass = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{int(st['size'])},{ass_col(st['text'])},{ass_col(st['text'])},"
        f"{out_c},{out_c if box else ass_col('#000000', 0x60)},-1,0,0,0,100,100,0,0,"
        f"{3 if box else 1},{float(st['outline_w']):g},{float(st['shadow']):g},{align},70,70,{margin},1",
        f"Style: Top,{font},58,&H00FFFFFF,&H00FFFFFF,&H40000000,&H40000000,"
        "-1,0,0,0,100,100,0,0,3,18,0,8,90,90,250,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    per = max(1, int(st.get("words") or 3))
    glow = r"\blur4" if st.get("glow") else ""
    pop = r"\fscx72\fscy72\t(0,90,\fscx100\fscy100)" if st.get("pop") else ""
    hl = st.get("highlight") or ""

    def clean(t):
        t = t.replace("{", "(").replace("}", ")").replace("\\", "/")
        return t.upper() if st.get("upper") else t

    for i in range(0, len(words), per):
        chunk = words[i:i + per]
        c_start = chunk[0][0] - offset
        c_end = (words[i + per][0] if i + per < len(words) else chunk[-1][1]) - offset
        c_end = min(c_end, chunk[-1][1] - offset + 0.6)
        texts = [clean(w[2]) for w in chunk]
        if hl and len(chunk) > 0:
            # one event per word, active word in the highlight colour
            for j, w in enumerate(chunk):
                s0 = c_start if j == 0 else w[0] - offset
                e0 = (chunk[j + 1][0] - offset) if j + 1 < len(chunk) else c_end
                if e0 <= s0:
                    continue
                parts = [(f"{{\\c{ass_inline(hl)}}}{t}{{\\c{ass_inline(st['text'])}}}" if k == j else t)
                         for k, t in enumerate(texts)]
                tags = glow + (pop if j == 0 else "")
                pre = f"{{{tags}}}" if tags else ""
                ass.append(f"Dialogue: 0,{fmt_ts(s0)},{fmt_ts(e0)},Cap,,0,0,0,,{pre}" + " ".join(parts))
        else:
            tags = glow + pop
            pre = f"{{{tags}}}" if tags else ""
            ass.append(f"Dialogue: 0,{fmt_ts(c_start)},{fmt_ts(c_end)},Cap,,0,0,0,,{pre}" + " ".join(texts))
    if overlay:
        safe_txt = overlay.replace("{", "(").replace("}", ")").replace("\n", "\\N")
        ass.append(f"Dialogue: 1,{fmt_ts(0)},{fmt_ts(duration + 1)},Top,,0,0,0,,{safe_txt}")
    path = tmp / "caps.ass"
    path.write_text("\n".join(ass), encoding="utf-8")
    return path


def list_fonts():
    fams = set()
    try:
        r = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=10)
        fams |= {f.split(",")[0].strip() for f in r.stdout.splitlines() if f.strip()}
    except (OSError, subprocess.TimeoutExpired):
        pass
    dirs = [FONTS_DIR]
    if WINDOWS:
        dirs += [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"]
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix.lower() not in (".ttf", ".otf"):
                continue
            fam = None
            if shutil.which("fc-scan"):
                r = subprocess.run(["fc-scan", "--format", "%{family}\n", str(f)],
                                   capture_output=True, text=True)
                fam = next((x.split(",")[0].strip() for x in r.stdout.splitlines() if x.strip()), None)
            fams.add(fam or font_family(f) or f.stem)
    fams.discard("")
    return sorted(fams, key=str.lower)


def font_family(path):
    """Read the family name from a .ttf/.otf file (no fontconfig needed)."""
    import struct
    try:
        data = Path(path).read_bytes()
        n = struct.unpack(">H", data[4:6])[0]
        for i in range(n):
            tag, _, off, _ = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
            if tag != b"name":
                continue
            _, cnt, so = struct.unpack(">HHH", data[off:off + 6])
            best = None
            for j in range(cnt):
                pid, eid, lid, nid, ln, lo = struct.unpack(">6H", data[off + 6 + 12 * j: off + 18 + 12 * j])
                if nid not in (1, 16):
                    continue
                raw = data[off + so + lo: off + so + lo + ln]
                txt = raw.decode("utf-16-be", "ignore") if pid in (0, 3) else raw.decode("latin-1", "ignore")
                if txt and (best is None or nid == 16):
                    best = txt
            return best
    except (OSError, struct.error, IndexError):
        return None
    return None


AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus"}


def pick_music(path):
    """A file, or a random track from a folder."""
    import random
    if not path:
        return None
    p = Path(path).expanduser()
    if p.is_dir():
        tracks = [f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXT]
        if not tracks:
            die(f"no music files in {p}")
        return random.choice(tracks)
    if not p.is_file():
        die(f"music not found: {p}")
    return p


def duration_of(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def has_audio(path: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


LOGO_W = 230          # logo width on the 1080-wide frame


def logo_xy(raw: Path, mode: str, pos: str):
    """Where the logo goes: just above the video in blur mode (clear of captions and the
    on-screen text), otherwise the top-right corner."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(raw)], capture_output=True, text=True)
    try:
        rw, rh = (int(x) for x in r.stdout.strip().split(",")[:2])
    except ValueError:
        rw, rh = 16, 9
    top = (H - round(W * rh / rw)) // 2 if mode == "blur" else 0
    pos = pos or "auto"
    if pos == "auto":
        pos = "above-video" if top > 420 else "top-right"
    return {"above-video": (f"W-w-48", f"{top}-h-28"), "top-right": ("W-w-48", "190"),
            "top-left": ("48", "190"), "top-center": ("(W-w)/2", "120")}.get(pos, ("W-w-48", "190"))


def video_dims(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
        return w, h
    except ValueError:
        return None


def face_tracking_available():
    try:
        import cv2                                   # noqa: F401  (pip: opencv-python-headless)
        return True
    except ImportError:
        return False


def face_model(name):
    """OpenCV's face-detector file: shipped with OpenCV 4, downloaded once otherwise (OpenCV 5 dropped them)."""
    try:
        import cv2
        p = Path(getattr(cv2, "data", None).haarcascades) / name
        if p.is_file():
            return p
    except (ImportError, AttributeError, TypeError):
        pass
    p = CACHE / "face" / name
    if p.is_file() and p.stat().st_size > 10000:
        return p
    try:
        import urllib.request
        p.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/{name}"
        data = urllib.request.urlopen(url, timeout=30).read()
        if b"<opencv_storage>" in data[:500]:
            p.write_bytes(data)
            return p
    except OSError:
        pass
    return None


def face_track_x(raw: Path):
    """Zoom mode: follow the main face instead of always cutting out the middle.
    Returns an ffmpeg expression for the crop's x position over time, or None (= centre)."""
    try:
        import cv2
    except ImportError:
        return None
    import numpy as np
    dims = video_dims(raw)
    if not dims:
        return None
    rw, rh = dims
    sw = rw * H / rh                                  # width after scaling to 1920 high
    if sw <= W * 1.05:
        return None                                   # already (nearly) vertical - nothing to follow
    fps, fh = 3, 360
    fw = int(round(rw * fh / rh / 2) * 2)
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(raw), "-an", "-vf",
                        f"fps={fps},scale={fw}:{fh},format=gray", "-f", "rawvideo", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    n = a.size // (fw * fh)
    if n < 2:
        return None
    front_f, side_f = face_model("haarcascade_frontalface_default.xml"), face_model("haarcascade_profileface.xml")
    if not front_f:
        print("  ! face model missing (no internet?) - keeping the middle")
        return None
    front = cv2.CascadeClassifier(str(front_f))
    side = cv2.CascadeClassifier(str(side_f)) if side_f else cv2.CascadeClassifier()
    xs, prev = [], None
    for i in range(n):
        img = a[i * fw * fh:(i + 1) * fw * fh].reshape(fh, fw)
        found = [tuple(f) for f in front.detectMultiScale(img, 1.1, 4, minSize=(24, 24))]
        if not found and not side.empty():
            found = [tuple(f) for f in side.detectMultiScale(img, 1.1, 4, minSize=(24, 24))]
            flip = cv2.flip(img, 1)
            found += [(fw - x - w, y, w, h) for x, y, w, h in side.detectMultiScale(flip, 1.1, 4, minSize=(24, 24))]
        best, bscore = None, 0
        for x, y, w, h in found:
            cx = (x + w / 2) / fw
            near = 1.0 if prev is None else max(0.25, 1 - abs(cx - prev) * 2)
            sc = w * h * near
            if sc > bscore:
                best, bscore = cx, sc
        xs.append(best)
        if best is not None:
            prev = best
    hits = sum(x is not None for x in xs)
    if hits < max(2, 0.25 * n):
        print("  no clear face to follow - keeping the middle")
        return None
    first = next(x for x in xs if x is not None)    # fill gaps: hold the last face position
    filled, last = [], first
    for x in xs:
        last = x if x is not None else last
        filled.append(last)
    med = [sorted(filled[max(0, i - 2):i + 3])[len(filled[max(0, i - 2):i + 3]) // 2] for i in range(n)]
    # a calm "camera": ignore small moves, glide to new positions, cut on big jumps
    cam, pos, far = [], med[0], 0
    for t in med:
        d = t - pos
        far = far + 1 if abs(d) > 0.3 else 0
        if far >= 2:
            pos, far = t, 0                           # speaker / shot changed: cut straight there
        elif abs(d) > 0.06:
            pos += d * 0.35
        cam.append(pos)
    maxx = sw - W
    px = [min(max(c * sw - W / 2, 0), maxx) for c in cam]
    pts = [(i / fps, p) for i, p in enumerate(px)]

    def rdp(ps, eps=10):                              # keep only the points that matter
        if len(ps) < 3:
            return ps
        (t0, x0), (t1, x1) = ps[0], ps[-1]
        dmax, idx = 0, 0
        for k in range(1, len(ps) - 1):
            t, x = ps[k]
            lin = x0 + (x1 - x0) * (t - t0) / ((t1 - t0) or 1)
            if abs(x - lin) > dmax:
                dmax, idx = abs(x - lin), k
        if dmax > eps:
            return rdp(ps[:idx + 1], eps)[:-1] + rdp(ps[idx:], eps)
        return [ps[0], ps[-1]]
    key = rdp(pts)[:80]
    print(f"  following the speaker's face ({hits}/{n} frames)")
    if len(key) == 1 or max(p for _, p in key) - min(p for _, p in key) < 6:
        return f"{key[0][1]:.0f}"
    expr = f"{key[-1][1]:.0f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(key, key[1:]))):
        dt = max(t1 - t0, 0.01)
        expr = f"if(lt(t,{t1:.2f}),{x0:.0f}+({x1 - x0:.0f})*(t-{t0:.2f})/{dt:.2f},{expr})"
    return expr


def render(raw: Path, out: Path, mode: str, subs, music=None, music_vol=0.15, music_start=0.0,
           logo=None, logo_pos="auto", track=True):
    if mode == "crop":      # zoom to fill, cut the sides off - following the face if we can see one
        fx = face_track_x(raw) if track else None
        if fx:
            v = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H}:x='{fx}':y='(ih-{H})/2'[v0]")
        else:
            v = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}[v0]"
    elif mode == "blur":    # whole video in the middle, blurred copy behind
        v = (f"[0:v]split[a][b];"
             f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
             f"boxblur=25:2[bg];[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
    else:
        v = "[0:v]null[v0]"
    d = duration_of(raw) or 60
    vfade = f"fade=t=in:d=0.12,fade=t=out:st={max(d - 0.3, 0):.2f}:d=0.3"
    if subs:
        fd = f":fontsdir='{ff_filter_path(FONTS_DIR)}'" if FONTS_DIR.is_dir() else ""
        v += f";[v0]subtitles='{ff_filter_path(subs)}'{fd},{vfade}[v]"
    else:
        v += f";[v0]{vfade}[v]"

    inputs = ["-i", str(raw)]
    amap = "0:a?"
    afade = f"afade=t=in:d=0.06,afade=t=out:st={max(d - 0.45, 0):.2f}:d=0.45"
    if not music and has_audio(raw):
        v += f";[0:a]{afade}[a]"
        amap = "[a]"
    if logo and Path(logo).is_file():
        li = 2 if music else 1
        x, y = logo_xy(raw, mode, logo_pos)
        v = v[: v.rindex("[v]")] + "[vl]" + v[v.rindex("[v]") + 3:]     # final video -> [vl]
        v += (f";[{li}:v]scale={LOGO_W}:-1,format=rgba[lg];"
              f"[vl][lg]overlay=x={x}:y={y}:shortest=1[v]")
    if music:
        inputs += ["-stream_loop", "-1", "-ss", f"{music_start:.2f}", "-i", str(music)]
        # music bed: fade in/out, then duck it whenever someone speaks
        v += (f";[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
              f"volume={music_vol},afade=t=in:d=0.8,"
              f"afade=t=out:st={max(d - 1.5, 0):.2f}:d=1.5[m];"
              f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit[sp1][sp2];"
              f"[m][sp1]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[md];"
              f"[sp2][md]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{afade}[a]")
        amap = "[a]"
    if logo and Path(logo).is_file():
        inputs += ["-loop", "1", "-i", str(logo)]
    ff(["ffmpeg", "-loglevel", "error", "-stats", "-y", *inputs,
         "-filter_complex", v, "-map", "[v]", "-map", amap,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", "-f", "mp4", str(out)])


def make_clip(src, start, end, out: Path, args, words=None, meta=None, broll=None):
    """words given -> reuse transcript; else transcribe the clip if captions wanted."""
    if end <= start:
        die(f"end must be after start ({out.name})")
    print(f"> {out.stem}: {short_ts(start)} -> {short_ts(end)}  ({end - start:.0f}s)")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        raw = fetch_segment(src, start, end, tmp)
        if broll:
            raw = mix_broll(raw, broll, tmp)
        win = []                                  # words in source time
        if words is not None:
            win = [tuple(w) for w in words if w[0] >= start - 0.05 and w[1] <= end + 0.05]
        elif args.captions:
            segs = transcribe(raw, args.model, cache=False)
            win = [(w[0] + start, w[1] + start, w[2]) for s in segs for w in s["words"]]
        keep = None
        if getattr(args, "cut_pauses", True) and win and not broll:
            try:
                rel = [(w[0] - start, w[1] - start, w[2]) for w in win]
                keep = plan_pause_cuts(rel, end - start, audio_envelope_tmp(raw))
            except Exception as e:                  # noqa - a clip without cuts is still a clip
                print(f"  (pause cutting skipped: {e})")
                keep = None
            if keep:
                kept = sum(b - a for a, b in keep)
                print(f"  cut {end - start - kept:.1f}s of pauses / ums")
                raw = apply_cuts(raw, keep, tmp)
        cap_words, cap_dur = win, end - start
        if keep:                                    # captions follow the cut-down timing
            cap_words = []
            for w in win:
                a, b = remap_time(w[0] - start, keep), remap_time(w[1] - start, keep)
                if a is not None and not FILLER_WORD.match(str(w[2]).strip()):
                    cap_words.append((start + a, start + (b if b is not None else a + 0.2), w[2]))
            cap_dur = sum(b - a for a, b in keep)
        need_video = getattr(args, "style", None) == "auto" or getattr(args, "match_colors", False)
        style = getattr(args, "fixed_style", None) or resolve_style(args, raw if need_video else None,
                                                                     win, end - start)
        subs = build_ass(cap_words if args.captions else [], start, tmp, style,
                         overlay=getattr(args, "on_screen", None), duration=cap_dur)
        music = pick_music(getattr(args, "music", None))
        if music:
            print(f"  music: {music.name}")
        # render next to the clip, then swap it in: the old clip stays playable
        # (and safe) until the new one is completely finished
        part = out.with_name(out.name + ".part")
        try:
            render(raw, part, args.mode, subs, music,
                   getattr(args, "music_vol", 0.15), getattr(args, "music_start", 0.0),
                   getattr(args, "logo", None), getattr(args, "logo_pos", "auto"),
                   getattr(args, "track", True))
            os.replace(part, out)
        finally:
            part.unlink(missing_ok=True)
    side = {"source": str(src), "start": start, "end": end, "words": win, "style": style,
            "mode": args.mode, "captions": bool(args.captions),
            "music": str(music) if music else None, "music_vol": getattr(args, "music_vol", 0.15),
            "music_start": getattr(args, "music_start", 0.0), "on_screen": getattr(args, "on_screen", None),
            "logo": getattr(args, "logo", None), "logo_pos": getattr(args, "logo_pos", "auto"),
            "track": getattr(args, "track", True), "cut_pauses": bool(keep),
            "cut_pauses_opt": getattr(args, "cut_pauses", True)}
    old = out.with_suffix(".clip.json")
    if meta is None and old.is_file():              # restyle: keep the original pick info
        try:
            meta = json.loads(old.read_text()).get("meta")
        except ValueError:
            meta = None
    side["meta"] = meta or {}
    if broll:
        side["broll"] = broll
    out.with_suffix(".clip.json").write_text(json.dumps(side))
    print(f"  saved {out}")


def mix_broll(raw: Path, inserts, tmp: Path) -> Path:
    """Keep raw's sound, cut to silent b-roll shots: inserts = [(at, dur, src, src_start)]"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(raw)], capture_output=True, text=True)
    try:
        w, h, fr = r.stdout.strip().split(",")[:3]
        w, h = int(w), int(h)
        num, den = (fr.split("/") + ["1"])[:2]
        fps = float(num) / float(den or 1)
    except ValueError:
        return raw
    inputs = ["-i", str(raw)]
    graph, prev = [], "0:v"
    for k, (at, dur, bsrc, bstart) in enumerate(inserts, 1):
        inputs += ["-ss", f"{bstart:.2f}", "-t", f"{dur + 0.5:.2f}", "-i", str(bsrc)]
        graph.append(f"[{k}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
                     f"fps={fps:.3f},setpts=PTS-STARTPTS+{at:.3f}/TB[b{k}]")
        graph.append(f"[{prev}][b{k}]overlay=enable='between(t,{at:.3f},{at + dur:.3f})':eof_action=pass[v{k}]")
        prev = f"v{k}"
    out = tmp / "mixed.mp4"
    ff(["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", ";".join(graph),
         "-map", f"[{prev}]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-c:a", "copy", str(out)])
    return out


def plan_broll(clip, words, broll_pool, n_start=0):
    """Pick where to cut to b-roll inside a clip and which silent shots to use."""
    dur = clip["end"] - clip["start"]
    n = max(1, min(3, round(dur / 12)))
    rel = [w[0] - clip["start"] for w in words if clip["start"] <= w[0] <= clip["end"]]
    inserts = []
    for i in range(n):
        target = 2.5 + (dur - 4.5) * (i + 0.5) / n        # spread out, keep the hook and the ending
        at = min(rel, key=lambda t: abs(t - target)) if rel else target   # cut on a word
        at = max(2.5, min(at, dur - 4.0))
        length = min(3.2, dur - 1.5 - at)
        if length < 1.5 or any(abs(at - a) < 4 for a, *_ in inserts):
            continue
        idx = n_start * 3 + len(inserts)
        b = broll_pool[idx % len(broll_pool)]
        reuse = idx // len(broll_pool)                    # same shot again? use a later part of it
        bdur = duration_of(Path(b["src"])) or (b["end"] + 10)
        bstart = b["start"] + 0.3 + reuse * (length + 0.7)
        if bstart + length > bdur:
            bstart = max(0.0, (bstart + length) % max(bdur - length, 1.0))
        inserts.append((round(at, 2), round(length, 2), b["src"], round(bstart, 2)))
    return inserts


def variations_mode(args):
    """Three new versions of one clip: a tight short cut, a longer one with more context,
    and the same moment in the other look with a hook line on screen."""
    clip = Path(args.variations).expanduser()
    sf = clip.with_suffix(".clip.json")
    if not sf.is_file():
        die("no saved settings for that clip - make it again first")
    sd = json.loads(sf.read_text())
    src, start, end = sd["source"], sd["start"], sd["end"]
    if not is_url(src) and not Path(src).is_file():
        die(f"the original video isn't there any more: {src}")
    args.captions = sd.get("captions", True)
    args.music, args.music_vol = sd.get("music"), sd.get("music_vol", 0.15)
    args.music_start = sd.get("music_start", 0.0)
    args.logo, args.logo_pos = sd.get("logo"), sd.get("logo_pos", "auto")
    args.track = sd.get("track", True)
    if sd.get("style"):
        args.fixed_style = sd["style"]
    words = [tuple(w) for w in sd.get("words", [])]
    all_words = words
    if not is_url(src) and sd.get("captions", True):
        try:                                        # whole transcript (cached) for the longer cut
            all_words = [tuple(w) for s_ in transcribe(Path(src), args.model) for w in s_.get("words", [])]
        except (Exception, SystemExit):
            pass
    ends = [w[1] for w in words if re.search(r"[.!?]$", str(w[2]).strip())]
    starts = [w[0] for k, w in enumerate(words) if k == 0 or re.search(r"[.!?]$", str(words[k - 1][2]).strip())]
    made = 0
    meta = (sd.get("meta") or {}) | {"variant": True}
    base_mode, base_screen = sd.get("mode", "blur"), sd.get("on_screen")

    def one(tag, s0, e0, mode, on_screen):
        nonlocal made
        if e0 - s0 < 4:
            return
        args.mode, args.on_screen = mode, on_screen
        out = clip.with_name(f"{clip.stem}-{tag}.mp4")
        make_clip(src, round(s0, 2), round(e0, 2), out, args, all_words, meta=meta, broll=None)
        made += 1

    # 1. short: end on the clip's last line (the payoff), start as late as still gives 8-20 s
    last = max(ends) + 0.5 if ends else end
    s_short = next((s for s in sorted(starts, reverse=True) if last - s >= 8), start)
    if last - s_short > 20:
        s_short = next((s for s in sorted(starts) if last - s <= 20), s_short)
    if (end - start) - (last - s_short) > 3:
        one("short", s_short - 0.3, min(end, last + 0.4), base_mode, base_screen)
    # 2. long: one more sentence of setup before, and of reaction after (max +15 s)
    before = [w for w in all_words if start - 15 <= w[0] < start - 0.2]
    s_long = start
    for k in range(len(before) - 1, -1, -1):
        if k == 0 or re.search(r"[.!?]$", str(before[k - 1][2]).strip()):
            s_long = before[k][0] - 0.4
            break
    after = [w for w in all_words if end + 0.2 <= w[0] <= end + 15]
    e_long = end
    for w in after:
        if re.search(r"[.!?]$", str(w[2]).strip()):
            e_long = w[1] + 0.6
            break
    if (s_long < start - 1) or (e_long > end + 1):
        one("long", s_long, e_long, base_mode, base_screen)
    # 3. other look + the most gripping line on screen as a hook
    other = "crop" if base_mode == "blur" else "blur"
    hook = base_screen
    if not hook and words:
        sents, cur = [], []
        for w in words:
            cur.append(str(w[2]).strip())
            if re.search(r"[.!?]$", cur[-1]):
                sents.append(" ".join(cur))
                cur = []
        sents = [x for x in sents if 3 <= len(x.split()) <= 12] or sents
        if sents:
            hook = max(sents, key=lambda x: len(HYPE.findall(x)) * 2 + len(FUNNY.findall(x)) * 2 +
                       len(OPINION.findall(x)) + (1 if "?" in x else 0) - len(x.split()) / 20)
            hook = " ".join(hook.split()[:12])
    one("look2", start, end, other, hook)
    print(f"made {made} variation(s)")


def restyle_mode(args):
    """Re-render one clip with a new caption style (uses the saved .clip.json)."""
    clip = Path(args.restyle).expanduser()
    side_f = clip.with_suffix(".clip.json")
    if not side_f.is_file():
        die(f"no saved settings for {clip.name} - it was made before Restyle existed, make it again")
    sd = json.loads(side_f.read_text())
    args.mode = sd.get("mode", args.mode)
    if getattr(args, "track", True):                 # keep how the clip was made unless told --no-track
        args.track = sd.get("track", True)
    if getattr(args, "cut_pauses", True):            # keep the clip's own setting unless told --keep-pauses
        args.cut_pauses = sd.get("cut_pauses_opt", True)
    args.captions = sd.get("captions", True) if (args.trim_start is not None or args.trim_end is not None) else True
    args.music = sd.get("music")
    args.music_vol = sd.get("music_vol", 0.15)
    args.music_start = sd.get("music_start", 0.0)
    args.on_screen = sd.get("on_screen")
    args.logo, args.logo_pos = sd.get("logo"), sd.get("logo_pos", "auto")
    src = sd["source"]
    if not is_url(src) and not Path(src).is_file():
        die(f"the original video isn't there any more: {src}")
    start, end = sd["start"], sd["end"]
    words = [tuple(w) for w in sd.get("words", [])]
    if args.trim_start is not None or args.trim_end is not None:
        start = max(0.0, args.trim_start if args.trim_start is not None else start)
        end = args.trim_end if args.trim_end is not None else end
        dur = duration_of(Path(src)) if not is_url(src) else None
        if dur:
            end = min(end, dur)
        if end - start < 1:
            die("the clip would be shorter than a second")
        if sd.get("style"):
            args.fixed_style = sd["style"]               # same look, new length
        if sd.get("captions", True) and not is_url(src) and (start < sd["start"] - 0.05 or end > sd["end"] + 0.05):
            try:                                        # captions for the extra bit: cached transcript
                segs = transcribe(Path(src), args.model)
                words = [tuple(w) for s_ in segs for w in s_.get("words", [])]
            except (Exception, SystemExit) as e:        # noqa - keep going without the new words
                print(f"  ! couldn't get captions for the extra part: {e}")
        print(f"> trimming {clip.name}: {short_ts(sd['start'])}-{short_ts(sd['end'])} -> "
              f"{short_ts(start)}-{short_ts(end)}")
        sd.pop("broll", None)                           # b-roll timings belonged to the old length
    else:
        print(f"> restyling {clip.name}")
    make_clip(src, start, end, clip, args, words, broll=sd.get("broll"))


# ======================================================================
# campaigns  (rules for each clipping campaign live in campaigns/*.json)
# ======================================================================
def projects_root():
    """The user's chosen main folder for everything (settings: projects_dir), or None for the default."""
    p = str(load_settings().get("projects_dir") or "").strip()
    if p:
        p = Path(p).expanduser()
        if p.is_dir():
            return p.resolve()
    return None


PROJECTS = projects_root()
# campaigns live in the projects folder if one is chosen, else next to clipper.py (as before)
CAMPAIGN_DIR = PROJECTS or Path(__file__).resolve().parent / "campaigns"
if PROJECTS:                                     # free clips (no campaign) go in there too
    OUT_DIR = PROJECTS / "Free clips"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def tidy_campaigns():
    """Move loose campaigns/<name>.json files into campaigns/<agency>/<name>/campaign.json"""
    if not CAMPAIGN_DIR.is_dir():
        return
    for f in CAMPAIGN_DIR.glob("*.json"):
        try:
            c = json.loads(f.read_text())
        except ValueError:
            print(f"! {f.name} isn't valid JSON - leaving it where it is")
            continue
        dest = CAMPAIGN_DIR / slug(c.get("agency", "other")).lower() / f.stem
        for sub in ("source", "clips"):
            (dest / sub).mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest / "campaign.json"))
        print(f"> organised campaign '{c.get('name', f.stem)}' into {dest}")


def list_campaigns():
    """-> [(id, name, agency)]   id is the folder path, e.g. clipping-culture/cod-dmz"""
    tidy_campaigns()
    out = []
    if CAMPAIGN_DIR.is_dir():
        for f in sorted(CAMPAIGN_DIR.glob("*/*/campaign.json")):
            try:
                c = json.loads(f.read_text())
            except ValueError:
                print(f"! {f} isn't valid JSON - skipping")
                continue
            cid = f.parent.relative_to(CAMPAIGN_DIR).as_posix()
            out.append((cid, c.get("name", f.parent.name), c.get("agency", "Other")))
    return out


def campaign_dir(cid) -> Path:
    return CAMPAIGN_DIR / cid


def load_campaign(cid):
    known = list_campaigns()
    ids = [c for c, _, _ in known]
    if cid not in ids:                       # allow the short name, e.g. "cod-dmz"
        matches = [c for c in ids if c.split("/")[-1] == cid]
        if len(matches) != 1:
            die(f"no campaign '{cid}' (have: {', '.join(ids) or 'none yet'})")
        cid = matches[0]
    f = campaign_dir(cid) / "campaign.json"
    try:
        c = json.loads(f.read_text())
    except ValueError as e:
        die(f"{f} has a mistake in it: {e}")
    c["id"] = cid
    for sub in ("source", "clips"):
        (campaign_dir(cid) / sub).mkdir(parents=True, exist_ok=True)
    return c


def file_into_campaign(src: str, c) -> str:
    """Move a downloaded video into the campaign's source folder."""
    if is_url(src):
        return src
    f = Path(src).expanduser().resolve()
    dest_dir = (campaign_dir(c["id"]) / "source").resolve()
    if not f.is_file() or dest_dir in f.parents:
        return str(f)
    dest = dest_dir / f.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{f.stem}-{n}{f.suffix}"
        n += 1
    shutil.move(str(f), str(dest))
    print(f"> moved {f.name} into {dest_dir}")
    return str(dest)


def best_settings(c, cdir=None):
    """The page settings that suit this campaign's brief best, with the reasons."""
    brief = ""
    if cdir and (Path(cdir) / "brief.txt").is_file():
        try:
            brief = (Path(cdir) / "brief.txt").read_text(errors="ignore").lower()
        except OSError:
            brief = ""
    text = brief + " " + " ".join(str(c.get(k) or "") for k in ("caption_rules", "name")).lower()
    why = []
    out = {"captions": bool(c.get("captions", True)), "music": bool(c.get("music", True)),
           "mode": c.get("mode") or "blur", "len": c.get("len") or "", "track": True, "cap_auto": True,
           "smart": {"laughs": True, "laugh_tail": True, "chat": True, "words": True,
                     "cut_pauses": True, "qa": True}}
    if out["len"]:
        why.append(f"{out['len'].replace('-', '–')} s clips")
    why.append({"blur": "Blur bg (whole frame)", "crop": "Zoom", "none": "original framing"}.get(out["mode"], out["mode"]))
    if not out["music"]:
        why.append("no added music")
    if not out["captions"]:
        why.append("no captions")
    if re.search(r"\b(podcast|two people|both (hosts|guests)|duo|panel)\b", text) and out["mode"] == "crop":
        out["track"] = False
        why.append("follow face off (often 2 people in shot)")
    if re.search(r"\b(no|don'?t|do not|avoid) (jump ?cuts?|cutting|editing the (speech|audio))\b", text):
        out["smart"]["cut_pauses"] = False
        why.append("pauses kept (brief bans jump cuts)")
    elif re.search(r"fast[- ]pac|no (dead|slow)|keep it (snappy|tight)|cut slow", text):
        why.append("pauses cut (brief wants fast pacing)")
    if re.search(r"\b(trailer|cinematic|gameplay only|no commentary)\b", text):
        out["smart"]["words"] = False
        why.append("judged on action, not words (trailer/gameplay)")
    if re.search(r"\b(stream|vod|twitch|kick|livestream|live)\b", text):
        why.append("stream chat highlights on")
    if re.search(r"\b(funny|comedy|laugh|meme|reaction)\b", text):
        why.append("laughs & funny lines boosted")
    out["why"] = why
    return out


def apply_campaign(args, c):
    """Force the campaign's rules onto the settings."""
    print(f"> campaign: {c.get('name', c['id'])}  ({c.get('agency', 'Other')})")
    if args.out_dir is None:
        args.out_dir = campaign_dir(c["id"]) / "clips"
    if args.source and not Path(str(args.source)).expanduser().is_dir():
        args.source = file_into_campaign(args.source, c)
    if not c.get("music", True) and getattr(args, "music", None):
        print("  ! this campaign doesn't allow added music - music turned OFF")
    if not c.get("music", True):
        args.music = None
    if c.get("len"):
        args.len = c["len"]
    lo, hi = (float(x) for x in args.len.split("-"))
    mn = float(c.get("min_len", 0))
    if lo < mn:
        lo = mn + 1
        hi = max(hi, lo + 5)
        args.len = f"{lo:g}-{hi:g}"
    if c.get("mode") and args.mode == "blur":
        args.mode = c["mode"]
    if "captions" in c:
        args.captions = bool(c["captions"])
    args.on_screen = c.get("on_screen")
    args.logo, args.logo_pos = None, c.get("logo_pos", "auto")
    if c.get("logo"):
        lf = Path(c["logo"]).expanduser()
        lf = lf if lf.is_absolute() else campaign_dir(c["id"]) / lf
        if lf.is_file():
            args.logo = str(lf)
            print(f"  logo: {lf.name}")
        else:
            print(f"  ! logo file not found: {lf} - clips will have NO logo")


def ollama_caption(c, text, model):
    prompt = (
        f"Write ONE short social media caption (max 10 words) for a TikTok clip.\n"
        f"Rules: {c.get('caption_rules', '')}\n"
        f"Only describe what is actually said or shown in the transcript. "
        f"No made-up facts, stats or dates. No hashtags, no emojis, no clickbait.\n"
        f"Style examples: {' | '.join(c.get('example_captions', []))}\n"
        f'Reply ONLY with JSON: {{"caption":"..."}}\n\nTranscript of the clip:\n{text[:1500]}')
    try:
        cap = (ai_json(prompt, ollama=model, timeout=300) or {})["caption"]
        cap = re.sub(r"#\w+", "", str(cap)).strip().strip('"')
        return cap if 3 < len(cap) < 120 else None
    except Exception:
        return None


def make_caption(c, text, n, ollama=None):
    if c.get("exact_captions") and c.get("example_captions"):
        caps = c["example_captions"]                 # approved list: copy word for word, nothing added
        return caps[n % len(caps)]
    line = ollama_caption(c, text, ollama) if (ollama or ai_name()) else None
    examples = c.get("example_captions") or ["(write your caption here)"]
    line = line or examples[n % len(examples)]
    m = c.get("mention")
    if m and m.lower() not in line.lower():
        line = f"{line} {m}"
    lines = [line] + list(c.get("required_lines", []))
    if c.get("disclosure"):
        lines.append(c["disclosure"])
    tags = [t for t in c.get("hashtags", []) if t.lower() != str(c.get("disclosure", "")).lower()]
    if tags:
        lines.append(" ".join(tags[:c.get("max_extra_hashtags", 3)]))
    return "\n".join(lines)


# ---------- reading a pasted campaign brief ----------
CAMPAIGN_FIELDS = ["name", "agency", "platforms", "source_note", "music", "min_len", "len", "mode",
                   "captions", "keywords", "on_screen", "mention", "required_lines", "disclosure",
                   "hashtags", "max_extra_hashtags", "caption_rules", "example_captions",
                   "exact_captions", "checklist", "rate", "manual_checks", "logo", "logo_pos", "mentions"]


def _clean_line(t):
    return re.sub(r"\s+", " ", re.sub(r"^[\s•\-–*✔✅❌⚠️️\d.)]+", "", t)).strip()


PLATFORM_ALIASES = {"tiktok": "TikTok", "tik tok": "TikTok", "instagram": "Instagram Reels", "ig": "Instagram Reels",
                    "reels": "Instagram Reels", "youtube": "YouTube Shorts", "youtube shorts": "YouTube Shorts",
                    "yt": "YouTube Shorts", "shorts": "YouTube Shorts", "x": "X", "twitter": "X"}


def brief_platform_tags(raw):
    """'TikTok @thearenahq · Instagram @thearena' -> a tag per platform (only if they differ)."""
    found = {}
    for m in re.finditer(r"\b(tik ?tok|instagram|ig|reels|youtube(?: shorts)?|yt|shorts|twitter|x)\b\s*[:\-–·(]?\s*(@[\w.]+)",
                         raw, re.I):
        plat = PLATFORM_ALIASES.get(m[1].lower())
        if plat and plat not in found:
            found[plat] = m[2].rstrip(".")
    return found if len(set(v.lower() for v in found.values())) > 1 else {}


def caption_for(caption, camp, platform):
    """The campaign's caption with the right @tag for this platform."""
    tags = (camp or {}).get("mentions") or {}
    main = (camp or {}).get("mention") or ""
    want = tags.get(platform)
    if caption and main and want and want.lower() != main.lower():
        return re.sub(re.escape(main), want, caption, flags=re.I)
    return caption


def parse_brief(text):
    """Best-effort reading of a campaign brief into campaign settings."""
    raw = text.replace("\r", "")
    if raw.count("\n") < 3 and len(raw) > 200:          # pasted as one long line
        raw = re.sub(r"\s(0\d|[1-9]\d?)\s(?=[A-Z][a-z])", r"\n\1 ", raw)
    low = raw.lower()
    lines = [l.strip() for l in raw.split("\n")]
    nonempty = [l for l in lines if l]
    c = {k: None for k in CAMPAIGN_FIELDS}

    # name
    m = re.search(r"campaign brief\s+(.+?)\s+clipping\b", raw, re.I)
    name = m[1] if m else (nonempty[0] if nonempty else "New campaign")
    name = re.sub(r"\s*[-–]?\s*clipping\s*$", "", name, flags=re.I).strip()
    letters = [ch for ch in name if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.8:
        name = name.title().replace(" X ", " x ").replace("Mvp", "MVP").replace("Pfl", "PFL")
    c["name"] = name[:80] or "New campaign"

    # agency
    m = (re.search(r"contact the (.+?) team", raw, re.I) or re.search(r"^([A-Z][A-Z ]{2,30}?) CAMPAIGNS", raw, re.M)
         or re.search(r"([A-Z][\w ]{2,30}?) •", raw))
    c["agency"] = (m[1].strip().title() if m and m[1].isupper() else m[1].strip()) if m else "Other"

    # platforms
    plats = []
    if "tiktok" in low:
        plats.append("TikTok")
    if "instagram" in low or "reels" in low:
        plats.append("Instagram Reels")
    if "youtube" in low or "shorts" in low:
        plats.append("YouTube Shorts")
    if re.search(r"(?<![\w@#])X(?=\s*[•,/|]|\s+and\b|\s*$)", raw, re.M) or "twitter" in low:
        plats.append("X")
    c["platforms"] = plats or ["TikTok", "Instagram Reels"]

    # footage link
    m = re.search(r"https?://\S+", raw)
    c["source_note"] = f"Use only the supplied content folder: {m[0]}" if m else ""

    # music
    no_music = re.search(r"(background )?music[^\n.]{0,40}(not allowed|no\b|prohibited|banned|isn'?t allowed)", low) \
        or re.search(r"(no|any added|❌[^\n]{0,10})\s*(background )?music", low)
    c["music"] = not no_music

    # lengths
    m = re.search(r"minimum (?:video |clip )?(?:length|duration)[^\d\n]{0,25}(\d+)\s*(s|sec|seconds)?", low)
    mn = int(m[1]) if m else 0
    m = re.search(r"maximum (?:video |clip )?(?:length|duration)[^\d\n]{0,25}(\d+)\s*(s|sec|seconds|m|min)?", low)
    mx = (int(m[1]) * (60 if m[2] and m[2].startswith("m") else 1)) if m else 0
    lo = max(mn + 2, 15) if mn else 20
    hi = min(mx, 60) if mx else (45 if mn else 60)
    c["min_len"], c["len"] = mn, f"{lo}-{max(hi, lo + 10)}"
    c["mode"], c["captions"] = "blur", True

    # tag / disclosure
    m = re.search(r"tag\s+(@[\w.]+)", raw, re.I)
    c["mention"] = m[1] if m else ""
    if not c["mention"]:
        m = re.search(r"includes? (@[\w.]+)", raw, re.I)
        c["mention"] = m[1] if m else ""
    c["disclosure"] = "#Ad" if re.search(r"#ad\b|#advertisement|#sponsored|ftc|disclos", low) else ""
    c["mentions"] = brief_platform_tags(raw)
    if c["mentions"] and not c["mention"]:
        c["mention"] = next(iter(c["mentions"].values()))

    # required line (e.g. "Use one of these lines:" -> next line)
    req = []
    for i, l in enumerate(lines):
        if re.search(r"(use one of these lines|must (include|say|mention) (this|the following)|required line)", l, re.I):
            for nxt in lines[i + 1:i + 4]:
                t = _clean_line(nxt)
                if t and re.fullmatch(r"(#\w+\s*)+", t):    # just a hashtag (e.g. #ad) - not a line
                    break
                if t and not t.isupper() and len(t) < 90:
                    req.append(t)
                    break
    c["required_lines"] = req

    # on-screen text
    m = re.search(r"[“\"]([^”\"]{3,60})[”\"][^.\n]{0,60}on-?screen", raw, re.I)
    if m:
        c["on_screen"] = m[1]
    elif req and re.search(r"on-?screen", low):
        c["on_screen"] = req[0]
    else:
        c["on_screen"] = ""

    # keywords
    kws = []
    for m in re.finditer(r"mention ([A-Z][\w&]+)(?: or | and |, )([A-Z][\w&]+)", raw):
        kws += [m[1], m[2]]
    for m in re.finditer(r"make the (.{3,40}?) the (?:main|central)", raw, re.I):
        kws.append(m[1].strip())
    for m in re.finditer(r"[“\"]([^”\"]{3,40})[”\"]", raw):
        if re.search(r"merger|launch|free|new", m[1], re.I):
            kws.append(m[1])
    c["keywords"] = list(dict.fromkeys(k for k in kws if k))[:6]

    # example captions: lines under an EXAMPLES heading
    ex, grab = [], False
    for l in lines:
        t = l.strip()
        if re.fullmatch(r"(caption )?examples?:?", t, re.I):
            grab = True
            continue
        if grab:
            if not t:
                continue
            if (t.isupper() and len(t) > 3) or t.endswith(":") or re.match(r"(note|same rule|[^\w]*[A-Z ]{4,}$)", t, re.I):
                grab = False
                continue
            t2 = re.sub(r"[#@][\w.]+", "", t).strip(" -–")
            if 8 < len(t2) < 90:
                ex.append(t2)
    c["example_captions"] = list(dict.fromkeys(ex))[:8]

    # approved captions that must be copied word for word
    appr, grab = [], False
    for l in lines:
        t = l.strip()
        if re.fullmatch(r"approved captions?:?", t, re.I):
            grab = True
            continue
        if grab:
            if not t:
                if appr:
                    grab = False
                continue
            if t.isupper() or t.endswith(":"):
                grab = False
                continue
            appr.append(t)
    c["exact_captions"] = bool(appr) and bool(re.search(r"word for word|approved (list|caption)|caption not from", low))
    if c["exact_captions"]:
        c["example_captions"] = appr[:20]

    # caption tone rules
    tone = [_clean_line(l) for l in lines if re.match(r"\s*(keep it|write|no made-up|no exaggerat)", l, re.I)]
    c["caption_rules"] = " ".join(tone)[:400] or "Describe what actually happens in the clip. No made-up facts or hype."

    # checklist: ticks/crosses/bullets
    chk = []
    for line in raw.split("\n"):
        for j, part in enumerate(line.split("•")):
            t = re.sub(r"\s+\d{1,2}\s+[A-Z][\w ]*$", "", part.strip())   # drop "03 Next section"
            if not t:
                continue
            if t.startswith("❌"):
                chk.append("Don't: " + _clean_line(t))
            elif t.startswith(("✔", "✅", "⚠")) or (j > 0 and 10 < len(t) < 200):
                chk.append(_clean_line(t))
    chk = [x for x in dict.fromkeys(chk) if 6 < len(x) < 160][:25]
    if re.search(r"paid partnership", low):
        chk.append("Turn on the platform's paid partnership label")
    chk.append("Submit the post link in Whop within 30 minutes of posting")
    c["checklist"] = chk
    c["hashtags"], c["max_extra_hashtags"] = [], 3
    rates = [float(x) for x in re.findall(r"\$\s?(\d+(?:\.\d+)?)\s*(?:cpm|per\s*1,?000|per\s*1k|/\s*1k)", raw, re.I)]
    rates += [float(x) for x in re.findall(r"(?:cpm|per 1k views|per 1,000 views)[^$\n]{0,20}\$\s?(\d+(?:\.\d+)?)", raw, re.I)]
    c["rate"] = max(rates) if rates else None
    c["manual_checks"] = manual_checks_from(low)
    c["logo"] = "logo.png" if re.search(r"logo watermark|add the (official )?.{0,30}logo", low) else ""
    if c["logo"]:
        c["manual_checks"].append("The logo is clear and easy to see")
    return c


def manual_checks_from(low):
    """Rules in a brief that need a human to look at the clip."""
    out = []
    if re.search(r"\b(child|children|kids?|minors?|teens?)\b", low):
        out.append("No children using, setting up or talking through the product - and none mentioned in text")
    if re.search(r"\brenders?\b|\bcgi\b|computer.generated", low):
        out.append("Renders/CGI aren't presented as real recorded footage")
    if re.search(r"(face|name)s? (of|or).{0,30}(person|people)|real people|not in .{0,20}content", low):
        out.append("No faces or names of people who aren't in the brand's own footage")
    if re.search(r"made.up (features|specs|prices|claims)|no made-up|false or made-up", low):
        out.append("On-screen text and caption make no made-up features, prices, dates or claims")
    if re.search(r"\bai[- ](made|generated|voice)|no ai\b", low):
        out.append("No AI-made footage, images or voices")
    if re.search(r"(political|controversial|nsfw|violent)", low):
        out.append("Nothing political, violent, NSFW or controversial in the clip")
    return out


def ollama_brief(text, model):
    """Ask a local AI to read the brief (better than the pattern matcher)."""
    prompt = (
        "Read this clipping campaign brief and fill in this JSON. Use only what the brief says.\n"
        '{"name": short campaign name, "agency": who runs it, "platforms": list from '
        '["TikTok","Instagram Reels","YouTube Shorts","X"], "source_note": "footage link if given", '
        '"music": true if background music is allowed else false, "min_len": minimum seconds (0 if none), '
        '"len": "LOW-HIGH" good clip length range in seconds, "keywords": words clips should mention, '
        '"on_screen": text that must be on screen ("" if none), "mention": "@account to tag" or "", '
        '"required_lines": lines that must be in every caption, "disclosure": "#Ad" if paid disclosure needed else "", '
        '"hashtags": up to 3 relevant hashtags, "caption_rules": one sentence on caption style, '
        '"example_captions": example or approved captions, "exact_captions": true if captions must be copied '
        'word for word from an approved list (then keep example_captions exactly as written), '
        '"checklist": short rules to check before posting}\n'
        "Reply ONLY with the JSON.\n\nBRIEF:\n" + text[:8000])
    return ai_json(prompt, ollama=model, timeout=300)


def read_brief(text, model=None):
    c = parse_brief(text)
    ai = ollama_brief(text, model) if (model or ai_name()) else None
    if isinstance(ai, dict):
        for k in CAMPAIGN_FIELDS:
            v = ai.get(k)
            if v not in (None, "", []) and (k not in ("music", "exact_captions") or isinstance(v, bool)):
                c[k] = v
        c["_via"] = ai_name(model)
    else:
        c["_via"] = "patterns"
    return c


def clean_campaign(d):
    """Validate a campaign coming from the web form."""
    out = {}
    s_ = lambda v, n=200: str(v or "").strip()[:n]
    lst = lambda v, n=120, m=30: [s_(x, n) for x in (v if isinstance(v, list) else str(v or "").split("\n"))
                                  if s_(x, n)][:m]
    out["name"] = s_(d.get("name"), 80) or "New campaign"
    out["agency"] = s_(d.get("agency"), 40) or "Other"
    out["platforms"] = [p for p in lst(d.get("platforms")) if p in ("TikTok", "Instagram Reels", "YouTube Shorts", "X")]
    out["source_note"] = s_(d.get("source_note"), 300)
    out["music"] = bool(d.get("music"))
    try:
        out["min_len"] = max(0, min(600, int(d.get("min_len") or 0)))
    except ValueError:
        out["min_len"] = 0
    out["len"] = s_(d.get("len"), 10) if re.fullmatch(r"\d{1,3}-\d{1,3}", s_(d.get("len"), 10)) else "20-60"
    out["mode"] = d.get("mode") if d.get("mode") in ("blur", "crop", "none") else "blur"
    out["captions"] = d.get("captions", True) is not False
    out["keywords"] = lst(d.get("keywords"), 40, 10)
    out["on_screen"] = s_(d.get("on_screen"), 120)
    m = s_(d.get("mention"), 40)
    out["mention"] = ("@" + m.lstrip("@")) if m else ""
    out["required_lines"] = lst(d.get("required_lines"), 120, 5)
    out["disclosure"] = s_(d.get("disclosure"), 20)
    out["hashtags"] = ["#" + h.lstrip("#") for h in lst(d.get("hashtags"), 40, 6)]
    out["max_extra_hashtags"] = 3
    out["caption_rules"] = s_(d.get("caption_rules"), 500)
    out["example_captions"] = lst(d.get("example_captions"), 200, 25)
    out["exact_captions"] = bool(d.get("exact_captions"))
    try:
        r = float(str(d.get("rate") or "").replace("$", "").strip() or 0)
        out["rate"] = r if 0 < r < 100 else None
    except ValueError:
        out["rate"] = None
    out["checklist"] = lst(d.get("checklist"), 200, 30)
    out["manual_checks"] = lst(d.get("manual_checks"), 200, 10)
    out["logo"] = s_(d.get("logo"), 200)
    tags = d.get("mentions") if isinstance(d.get("mentions"), dict) else {}
    out["mentions"] = {p: "@" + s_(v, 40).lstrip("@") for p, v in tags.items()
                       if p in ("TikTok", "Instagram Reels", "YouTube Shorts", "X") and s_(v, 40).lstrip("@")}
    out["logo_pos"] = d.get("logo_pos") if d.get("logo_pos") in ("auto", "above-video", "top-right", "top-left", "top-center") else "auto"
    return out


def downloads_dir():
    """The real Downloads folder - on Windows it's often moved (e.g. into OneDrive or another drive)."""
    if WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                v, _ = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
            p = Path(os.path.expandvars(v))
            if p.is_dir():
                return p
        except OSError:
            pass
    elif shutil.which("xdg-user-dir"):
        try:
            r = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=5)
            p = Path(r.stdout.strip())
            if r.returncode == 0 and p.is_dir() and p != Path.home():
                return p
        except (OSError, subprocess.TimeoutExpired):
            pass
    return Path.home() / "Downloads"


DOWNLOADS = downloads_dir()


_DUR_CACHE = {}


def downloads_list(days=30):
    """Recent videos and zips in ~/Downloads (lengths checked in parallel, cached)."""
    from concurrent.futures import ThreadPoolExecutor
    out = []
    if not DOWNLOADS.is_dir():
        return out
    cutoff = __import__("time").time() - days * 86400
    for f in DOWNLOADS.iterdir():
        try:
            st = f.stat()
        except OSError:
            continue
        if not f.is_file() or st.st_mtime < cutoff:
            continue
        ext = f.suffix.lower()
        if ext in VIDEO_EXT or ext == ".zip":
            out.append({"path": str(f), "name": f.name, "size": st.st_size, "mtime": st.st_mtime,
                        "zip": ext == ".zip", "dur": 0})

    def length(item):
        if item["zip"]:
            return
        key = (item["path"], item["size"], item["mtime"])
        if key not in _DUR_CACHE:
            _DUR_CACHE[key] = duration_of(Path(item["path"]))
        item["dur"] = _DUR_CACHE[key]

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(length, out))
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def import_downloads(cid, paths):
    """Move chosen Downloads videos into a campaign; unpack videos from zips."""
    import zipfile
    src_dir = campaign_dir(cid) / "source"
    src_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    dl = DOWNLOADS.resolve()
    for p in paths:
        f = Path(p).resolve()
        if dl not in f.parents or not f.is_file():
            continue
        if f.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(f) as z:
                    for info in z.infolist():
                        name = Path(info.filename).name
                        if info.is_dir() or Path(name).suffix.lower() not in VIDEO_EXT or name.startswith("."):
                            continue
                        dest = src_dir / name
                        n = 1
                        while dest.exists():
                            dest = src_dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
                            n += 1
                        with z.open(info) as zin, open(dest, "wb") as zout:
                            shutil.copyfileobj(zin, zout)
                        moved.append(dest.name)
                keep = campaign_dir(cid) / "zips"
                keep.mkdir(exist_ok=True)
                shutil.move(str(f), str(keep / f.name))
            except zipfile.BadZipFile:
                continue
        elif f.suffix.lower() in VIDEO_EXT:
            moved.append(Path(file_into_campaign(str(f), {"id": cid})).name)
    return moved


def save_campaign(c, brief=None, cid=None):
    if cid and (CAMPAIGN_DIR / cid / "campaign.json").is_file():
        dest = CAMPAIGN_DIR / cid
    else:
        tidy = lambda t, n=60: re.sub(r"-{2,}", "-", slug(t, n)).strip("-").lower()
        dest = CAMPAIGN_DIR / tidy(c["agency"]) / tidy(c["name"], 40)
    for sub in ("source", "clips"):
        (dest / sub).mkdir(parents=True, exist_ok=True)
    (dest / "campaign.json").write_text(json.dumps(c, indent=2, ensure_ascii=False))
    if brief:
        (dest / "brief.txt").write_text(brief)
    return dest.relative_to(CAMPAIGN_DIR).as_posix()


# ---------- pre-flight checks ----------
_PROBE = {}


def _probe(path: Path):
    """(has_audio, duration, short side in px) - cached by file + mtime"""
    try:
        st = path.stat()
    except OSError:
        return (False, 0.0, 0)
    key = (str(path), st.st_mtime, st.st_size)
    if key not in _PROBE:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                            "-show_entries", "format=duration", "-of", "json", str(path)],
                           capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
        except ValueError:
            j = {}
        streams = j.get("streams", [])
        vid = [x for x in streams if x.get("codec_type") == "video"]
        short = min(vid[0].get("width", 0), vid[0].get("height", 0)) if vid else 0
        _PROBE[key] = (any(x.get("codec_type") == "audio" for x in streams),
                       float(j.get("format", {}).get("duration") or 0), short)
    return _PROBE[key]


def clip_checks(f: Path, c=None):
    """Problems worth a look before posting this clip."""
    out = []
    audio, dur, _ = _probe(f)
    c = c or {}
    if not audio:
        out.append("No sound" + (" - and music isn't allowed" if c.get("music") is False else ""))
    if c.get("min_len") and dur and dur < float(c["min_len"]) - 0.2:
        out.append(f"Only {dur:.0f}s - minimum is {c['min_len']}s")
    if dur > 180:
        out.append("Over 3 minutes - won't count as a Short")
    capf = f.with_suffix(".txt")
    cap = capf.read_text().strip() if capf.is_file() else ""
    side = {}
    sf = f.with_suffix(".clip.json")
    if sf.is_file():
        try:
            side = json.loads(sf.read_text())
        except ValueError:
            side = {}
    if c and cap:
        if c.get("exact_captions"):
            if cap not in [x.strip() for x in c.get("example_captions", [])]:
                out.append("Caption isn't one of the approved ones")
        else:
            if c.get("mention") and c["mention"].lower() not in cap.lower() and \
                    not any(t.lower() in cap.lower() for t in (c.get("mentions") or {}).values()):
                out.append(f"Caption is missing {c['mention']}")
            if c.get("disclosure") and c["disclosure"].lower() not in cap.lower():
                out.append(f"Caption is missing {c['disclosure']}")
    kws = [k.lower() for k in c.get("keywords", []) if k]
    if kws:
        said = " ".join(w[2] for w in side.get("words", [])).lower() + " " + cap.lower() \
            + " " + str(c.get("on_screen") or "").lower()
        if not any(k in said for k in kws):
            out.append("Doesn't mention " + " / ".join(c.get("keywords", [])[:3]))
    src = side.get("source")
    if src and not is_url(src) and Path(src).is_file():
        short = _probe(Path(src))[2]
        if short and short < 720:
            out.append(f"Low-res source ({short}p) - may look blurry")
    return out


def clip_meta(c):
    return {"score": round(float(c.get("score", 0)), 3), "visual": bool(c.get("visual")),
            "kw": int(c.get("keyword_hits", 0)), "len": round(c["end"] - c["start"], 1),
            "mix": bool(c.get("mix")), "tags": c.get("tags", [])}


def rate_clip(f: Path, c=None, checks=None, learn=None, rank=None):
    """1-5 stars for how worthwhile a clip is to post, plus a short reason."""
    import math
    c = c or {}
    side = {}
    try:
        side = json.loads(f.with_suffix(".clip.json").read_text())
    except (OSError, ValueError):
        pass
    meta = side.get("meta") or {}
    audio, dur, _ = _probe(f)
    why = []
    if "score" in meta:
        moment = 1 / (1 + math.exp(-(meta["score"] - 1.0) / 1.5))
        if rank is not None:                       # how it compares with the rest of its batch
            moment = 0.55 * moment + 0.45 * rank
    else:
        moment = 0.5
    stars = 1 + 2.4 * moment
    why.append("strong moment" if moment > 0.7 else "decent moment" if moment > 0.45 else "weak moment")
    rate = c.get("rate")
    try:
        rate = float(rate) if rate not in (None, "") else None
    except ValueError:
        rate = None
    if rate:
        stars += min(1.0, rate / 2.5)
        if rate >= 2:
            why.append(f"top pay (${rate:g}/1k)")
        elif rate <= 0.6:
            why.append(f"low pay (${rate:g}/1k)")
    else:
        stars += 0.4
    if audio:
        stars += 0.4
        why.append("mixed b-roll" if meta.get("mix") else "has sound")
    else:
        stars -= 0.6
        why.append("silent")
    if c.get("keywords") and meta.get("kw"):
        stars += 0.2
        why.append("mentions " + c["keywords"][0])
    tags = meta.get("tags") or []
    good = [t for t in tags if not t.startswith("😴")]
    stars += min(0.5, 0.25 * len(good)) - (0.3 if len(good) < len(tags) else 0)
    why += tags[:2]
    probs = [w for w in (checks or []) if not w.startswith("No sound")]
    for w in probs:
        stars -= 0.3 if w.startswith("Low-res") else 0.6
    if probs:
        why.append(f"{len(probs)} warning{'s' if len(probs) > 1 else ''}")
    if learn:
        bonus, note = learn_bonus(learn, meta, dur)
        stars += bonus
        if note:
            why.append(note)
    stars = max(1.0, min(5.0, round(stars * 2) / 2))
    return stars, ", ".join(why[:4]).capitalize()


RESULTS_FILE = Path(__file__).resolve().parent / "results.json"


def load_results():
    try:
        d = json.loads(RESULTS_FILE.read_text())
        return d if isinstance(d, dict) else {"posts": []}
    except (OSError, ValueError):
        return {"posts": []}


def save_results(d):
    tmp = RESULTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, RESULTS_FILE)


def fetch_post_stats(url):
    """Read a post's views/likes from its link with yt-dlp (no download).
    Instagram often needs you logged in - then it uses Firefox's login if there is one."""
    if not shutil.which("yt-dlp") or not re.match(r"https?://", url or ""):
        return None
    tries = [[]]
    ff_dir = (Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox") if WINDOWS \
        else (Path.home() / ".mozilla" / "firefox")
    if "instagram.com" in url and ff_dir.is_dir():
        tries.append(["--cookies-from-browser", "firefox"])
    for extra in tries:
        try:
            r = subprocess.run(["yt-dlp", "-q", "--no-warnings", "--skip-download", "--no-playlist",
                                "-J", *extra, url], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode != 0 or not r.stdout.strip():
            continue
        try:
            info = json.loads(r.stdout)
        except ValueError:
            continue
        views = info.get("view_count") or info.get("play_count")
        likes = info.get("like_count")
        if views is not None or likes is not None:
            return {"views": views, "likes": likes}
    return None


def refresh_post_stats(ids=None):
    """Update views/likes for logged posts that have a link. Returns (updated, failed platforms)."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    posts = [p for p in load_results().get("posts", []) if p.get("url") and (not ids or p.get("id") in ids)]
    with ThreadPoolExecutor(4) as ex:
        got = dict(zip([p["id"] for p in posts], ex.map(lambda p: fetch_post_stats(p["url"]), posts)))
    data = load_results()
    ok, failed = 0, []
    for p in data.get("posts", []):
        if p.get("id") not in got:
            continue
        st = got[p["id"]]
        if not st:
            failed.append(p.get("platform", ""))
            continue
        for k in ("views", "likes"):
            if st.get(k) is not None:
                p[k] = int(st[k])
        p["updated"] = time.time()
        p["auto"] = time.time()
        ok += 1
    save_results(data)
    return ok, failed


def len_bucket(sec):
    return "short" if sec <= 22 else "medium" if sec <= 40 else "long"


def load_learning():
    """What has done well so far: average views per kind of clip. None until there's enough data."""
    posts = [p for p in load_results().get("posts", []) if p.get("views") is not None]
    if len(posts) < 6:
        return None
    avg = sum(p["views"] for p in posts) / len(posts) or 1
    buckets = {}
    for p in posts:
        m = p.get("meta") or {}
        for key in (f"len:{len_bucket(m.get('len', 30))}", f"kind:{'visual' if m.get('visual') else 'speech'}"):
            buckets.setdefault(key, []).append(p["views"])
    return {"avg": avg, "n": len(posts),
            "b": {k: (sum(v) / len(v), len(v)) for k, v in buckets.items() if len(v) >= 3}}


def learn_bonus(learn, meta, dur):
    """Small push up (or down) for clips like the ones that got views before."""
    import math
    if not learn:
        return 0.0, ""
    bonus, notes = 0.0, []
    for key, label in ((f"len:{len_bucket(meta.get('len', dur or 30))}", "{} clips"),
                       (f"kind:{'visual' if meta.get('visual') else 'speech'}",
                        "{} clips")):
        if key in learn["b"]:
            avg, n = learn["b"][key]
            b = max(-0.35, min(0.35, math.log2(max(avg, 1) / learn["avg"]) * 0.3))
            bonus += b
            if b > 0.12:
                notes.append(label.format(key.split(":")[1]))
    return bonus, (f"your {' & '.join(notes)} do well" if notes else "")


def bf_needs_logo(d: Path):
    bf = d / "brief.txt"
    return bf.is_file() and bool(re.search(r"logo watermark|add the (official )?.{0,30}logo", bf.read_text().lower()))


def campaign_checks(cid):
    """Does the campaign's setup match its brief? -> list of (level, message)"""
    out = []
    d = campaign_dir(cid)
    try:
        c = json.loads((d / "campaign.json").read_text())
    except (OSError, ValueError):
        return [("error", "Campaign file can't be read")]
    if re.fullmatch(r"\s*[#@][\w.]+\s*", str(c.get("on_screen") or "")):
        out.append(("warn", f"On-screen text is just \"{c['on_screen']}\" - probably a mistake"))
    if c.get("exact_captions") and not c.get("example_captions"):
        out.append(("error", "Word-for-word captions is on, but there are no approved captions listed"))
    if not c.get("platforms"):
        out.append(("warn", "No platforms ticked"))
    bf = d / "brief.txt"
    if bf.is_file():
        b = parse_brief(bf.read_text())
        if c.get("music") and not b["music"]:
            out.append(("warn", "Music is allowed here, but the brief looks like it bans it"))
        if b["exact_captions"] and not c.get("exact_captions"):
            out.append(("warn", "The brief has an approved caption list, but word-for-word is off"))
        if b["mention"] and not c.get("mention") and not c.get("exact_captions"):
            out.append(("warn", f"The brief asks you to tag {b['mention']}"))
        if b["min_len"] and int(c.get("min_len") or 0) < b["min_len"]:
            out.append(("warn", f"The brief's minimum length is {b['min_len']}s"))
        if b.get("manual_checks") and not c.get("manual_checks"):
            out.append(("warn", "The brief has rules only you can check (e.g. children, renders) - add them to "
                                "'Things to check by eye' so Review asks you"))
        if b["disclosure"] and not c.get("disclosure") and not c.get("exact_captions"):
            out.append(("warn", "The brief needs a disclosure like #ad"))
    if c.get("logo"):
        lf = Path(c["logo"]).expanduser()
        lf = lf if lf.is_absolute() else d / lf
        if not lf.is_file():
            out.append(("error", f"Logo needed but there's no '{c['logo']}' in the campaign folder - "
                                 f"put the logo image (PNG) there"))
    elif bf_needs_logo(d):
        out.append(("warn", "The brief asks for a logo watermark, but no logo is set"))
    vids = [f for f in (d / "source").iterdir() if f.suffix.lower() in VIDEO_EXT] if (d / "source").is_dir() else []
    if not vids:
        out.append(("info", "No footage in this campaign yet"))
    elif c.get("music") is False:
        silent = sum(1 for f in vids if not _probe(f)[0])
        if silent:
            out.append(("info", f"{silent} of {len(vids)} videos have no sound (and music isn't allowed) - they're ranked last"))
    return out


def write_campaign_files(c, folder, picks, ollama=None):
    for n, p in enumerate(picks):
        cap = make_caption(c, p["text"], n, ollama)
        (folder / f"{p['name']}.txt").write_text(cap + "\n")
    check = [f"POSTING CHECKLIST - {c.get('name', c['id'])}",
             f"Post to: {', '.join(c.get('platforms', []))}", ""]
    check += [f"[ ] {x}" for x in c.get("checklist", [])]
    check += ["", "Captions are in the .txt file next to each clip -",
              "read them, make sure they match the clip and are true, then paste."]
    (folder / "POSTING-CHECKLIST.txt").write_text("\n".join(check) + "\n")
    print(f"  captions + checklist written to {folder}")


# ======================================================================
# modes
# ======================================================================
def motion_curve(path: Path):
    """How much the picture changes every HOP seconds (0..1), plus scene-cut times."""
    import numpy as np
    st = path.stat()
    key = hashlib.md5(f"{path.resolve()}|{st.st_size}|{st.st_mtime}|{HOP}".encode()).hexdigest()
    cf = CACHE / "motion" / f"{key}.json"
    if cf.is_file():
        try:
            d = json.loads(cf.read_text())
            print("  using cached movement scan")
            return np.array(d["diff"]), d["cuts"]
        except (ValueError, KeyError):
            pass
    print("  scanning for movement and scene cuts (no real speech)...", flush=True)
    fps = 1 / HOP
    cmd = ["ffmpeg", "-loglevel", "error", "-i", str(path), "-an", "-vf",
           f"fps={fps:g},scale=64:36,format=gray", "-f", "rawvideo", "-"]
    r = None
    if gpu_video():
        r = subprocess.run(gpu_cmd(cmd), capture_output=True)
        if r.returncode != 0 or not r.stdout:
            r = None
    if r is None:
        r = subprocess.run(cmd, capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    n = a.size // (64 * 36)
    if n < 2:
        return np.zeros(1), []
    f = a[: n * 64 * 36].reshape(n, 36, 64).astype(float)
    diff = np.concatenate([[0.0], np.abs(np.diff(f, axis=0)).mean(axis=(1, 2)) / 255])
    thr = diff.mean() + 2.5 * diff.std()
    cuts = [i * HOP for i in range(1, n) if diff[i] > max(thr, 0.08)]
    try:
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps({"diff": [round(float(x), 5) for x in diff], "cuts": cuts}))
    except OSError:
        pass
    return diff, cuts


def visual_moments(src, lo, hi, target):
    """No speech: pick clips from movement + sound instead of words."""
    import numpy as np
    dur = duration_of(src)
    if dur < max(3.0, lo - 1):                 # shorter than the campaign allows
        return []
    motion, cuts = motion_curve(src)
    loud = loudness_curve(src)
    n = len(motion)
    lz = np.zeros(n)
    m = min(n, len(loud))
    lz[:m] = np.clip(loud[:m], -2, 3) / 3
    act = motion / (motion.mean() + 1e-6) * 0.7 + lz * 0.3      # activity per half-second
    base = float(np.clip(motion.mean() * 12, 0, 2))              # how lively the whole video is
    if not has_audio(src):
        base -= 1.5                                               # silent clips perform badly
    stretch = min(10.0, hi * 0.25)
    if dur <= hi + stretch:                                       # short video: use all of it
        return [{"i": 0, "j": 0, "start": 0.0, "end": dur, "text": "", "visual": True,
                 "score": base + 0.5 - abs(dur - target) / max(target, 1) * 0.5}]
    cands = []
    starts = sorted(set([0.0] + [c for c in cuts if c < dur - lo]))
    t = 0.0
    while t < dur - lo:                                           # every 2s, plus every scene cut
        starts.append(t)
        t += 2.0
    for st in sorted(set(round(x, 1) for x in starts)):
        en = min(dur, st + target)
        nxt = [c for c in cuts if st + lo <= c <= st + hi]        # end on a cut if one is close
        if nxt:
            en = min(nxt, key=lambda c: abs(c - (st + target)))
        a, b = int(st / HOP), max(int(en / HOP), int(st / HOP) + 1)
        sc = float(act[a:b].mean()) if b <= n else float(act[a:].mean() if a < n else 0)
        sc += 0.3 if st in cuts or st == 0 else 0                 # starting on a fresh shot
        cands.append({"i": 0, "j": 0, "start": st, "end": en, "text": "", "visual": True,
                      "score": base + sc})
    return sorted(cands, key=lambda c: c["score"], reverse=True)


def find_moments(src, args, lo, hi, target):
    """Transcribe (cached), then score every possible clip in one video.
    Videos with (almost) no speech fall back to picking by movement and sound."""
    raw_segs = [s for s in transcribe(src, args.model) if s["text"]]
    # drop shaky "speech" - sung lyrics, music, noise - so it can't turn into nonsense captions
    segs = [s for s in raw_segs if s.get("conf", 0) > -1.0 and s.get("nospeech", 0) < 0.6]
    if len(segs) < len(raw_segs):
        print(f"  ignored {len(raw_segs) - len(segs)} unclear bit(s) of 'speech' (music/lyrics)")
    words = sum(len(s["text"].split()) for s in segs)
    if words < 12:
        cands = visual_moments(src, lo, hi, target)
        if not cands:
            die("video too short to use")
        print(f"  no real speech - picking by movement and sound ({len(cands)} options)")
        if getattr(args, "chat", True):
            add_chat_bonus(src, cands)
        learn = load_learning()
        if learn:
            for c in cands:
                c["score"] += learn_bonus(learn, {"len": c["end"] - c["start"], "visual": True}, 0)[0] * 2
            cands.sort(key=lambda c: c["score"], reverse=True)
        return segs, cands
    loud = loudness_curve(src)
    if getattr(args, "qa", True):
        segs = split_sentences(segs)               # answers and questions become separate pieces
    cands = build_candidates(segs, lo, hi, target, qa=getattr(args, "qa", True))
    if not cands:
        die(f"no {args.len}s windows found - try a different --len")
    try:
        if not getattr(args, "laughs", True):
            raise RuntimeError("turned off")
        laugh = reaction_curve(src, segs)
        n_laughs = int(((laugh[1:] >= 0.45) & (laugh[:-1] < 0.45)).sum()) if len(laugh) > 1 else 0
        if n_laughs:
            print(f"  heard {n_laughs} laugh / reaction moment(s)")
    except Exception as e:                         # noqa - never let this stop clipping
        print(f"  (laugh detection {'off' if str(e) == 'turned off' else 'skipped: ' + str(e)})")
        laugh = None
    cands = score_candidates(cands, segs, loud, laugh, words=getattr(args, "smart_words", True))
    if laugh is not None and getattr(args, "laugh_tail", True):
        for c in cands:
            extend_for_laugh(c, segs, laugh)
    if getattr(args, "chat", True):
        add_chat_bonus(src, cands, len(loud))
    camp = getattr(args, "campaign_data", None)
    kws = [k.lower() for k in (camp or {}).get("keywords", []) if k]
    if kws:
        hits = 0
        for c in cands:
            n = sum(1 for k in kws if re.search(r"\b" + re.escape(k) + r"\b", c["text"].lower()))
            c["keyword_hits"] = n
            c["score"] += 3.0 * min(n, 2)          # strongly prefer clips that say the keywords
            hits += bool(n)
        cands.sort(key=lambda c: c["score"], reverse=True)
        print(f"  {hits} possible clips mention: {', '.join(kws)}")
    learn = load_learning()
    if learn:                                        # lean towards what has got views before
        for c in cands:
            b, _ = learn_bonus(learn, {"len": c["end"] - c["start"], "visual": c.get("visual")}, 0)
            c["score"] += b * 2
        cands.sort(key=lambda c: c["score"], reverse=True)
    return segs, cands


def auto_mode(args):
    lo, hi = (float(x) for x in args.len.split("-"))
    target = args.target or (lo + hi) / 2
    if is_url(args.source):
        src, title = download_full(args.source)
    else:
        src = Path(args.source).expanduser()
        if not src.exists():
            die(f"file not found: {src}")
        title = src.stem

    print("> analysing")
    segs, cands = find_moments(src, args, lo, hi, target)

    k = args.k
    if not k:                                  # auto: about one clip per 2.5 minutes
        if cands and cands[0].get("visual"):
            dur = duration_of(src)
            k = max(1, min(6, round(dur / 60)))
            print(f"  {short_ts(dur)} of footage - making {k} clip(s)")
        else:
            dur = segs[-1]["end"] - segs[0]["start"]
            k = max(2, min(10, round(dur / 150)))
            print(f"  {short_ts(dur)} of speech - making {k} clips")
    picks = ollama_pick(cands, k, args.ollama) if (args.ollama or ai_name()) else None
    picks = picks or pick(cands, k)
    for c in picks:
        if not c.get("title"):
            c["title"] = (f"{Path(str(src)).stem} at {short_ts(c['start'])}" if c.get("visual")
                          else " ".join(segs[c["i"]]["text"].split()[:6]))

    folder = args.out_dir / slug(title, 50)
    folder.mkdir(parents=True, exist_ok=True)
    lines = [f"# clips picked from: {title}", f"# source: {args.source}",
             "# edit times and re-run with --list to fine-tune", ""]
    print(f"\n> top {len(picks)} moments:")
    for n, c in enumerate(picks, 1):
        c["name"] = f"{n:02d}-{slug(c['title'], 40)}"
        preview = c["text"][:110] + ("..." if len(c["text"]) > 110 else "")
        print(f"  {n}. {short_ts(c['start'])}-{short_ts(c['end'])}  {c['title']}\n"
              f"     \"{preview}\"")
        lines += [f"# {c['title']}  (score {c.get('score', 0):.2f})",
                  f"# \"{preview}\"",
                  f"{short_ts(c['start'])} {short_ts(c['end'])} {c['name']}", ""]
    (folder / "clips.txt").write_text("\n".join(lines))
    if getattr(args, "campaign_data", None):
        write_campaign_files(args.campaign_data, folder, picks, args.ollama)
    print(f"\n  list saved to {folder / 'clips.txt'}")
    if args.dry_run:
        return

    words = [w for s in segs for w in s["words"]]
    for c in sorted(picks, key=lambda c: c["start"]):
        make_clip(src, c["start"], c["end"], folder / f"{c['name']}.mp4", args, words, clip_meta(c))
    print(f"\ndone - {len(picks)} clips in {folder}")


def audio_mode(args):
    """Rip audio only: link, playlist or local file -> mp3 (optionally just -s/-e)."""
    out_dir = Path(args.audio_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    src = args.source
    if is_url(src):
        need("yt-dlp")
        cmd = ["yt-dlp", "--no-warnings", "-x", "--audio-format", args.audio_format,
               "--audio-quality", "0", "--embed-metadata",
               "-o", str(out_dir / "%(title)s.%(ext)s")]
        if args.start or args.end:
            st = to_seconds(args.start) if args.start else 0
            en = to_seconds(args.end) if args.end else "inf"
            cmd += ["--download-sections", f"*{st}-{en}", "--force-keyframes-at-cuts"]
        run(cmd + [src])
    else:
        f = Path(src).expanduser()
        if not f.is_file():
            die(f"file not found: {f}")
        out = out_dir / f"{f.stem}.{args.audio_format}"
        cmd = ["ffmpeg", "-loglevel", "error", "-stats", "-y"]
        if args.start:
            cmd += ["-ss", str(to_seconds(args.start))]
        if args.end:
            cmd += ["-to", str(to_seconds(args.end))]
        codec = {"mp3": ["-c:a", "libmp3lame", "-q:a", "0"],
                 "wav": ["-c:a", "pcm_s16le"],
                 "flac": ["-c:a", "flac"],
                 "m4a": ["-c:a", "aac", "-b:a", "256k"]}[args.audio_format]
        run(cmd + ["-i", str(f), "-vn", *codec, str(out)])
    print(f"\ndone - audio saved in {out_dir}")


def bulk_mode(args):
    """Scan EVERY video in a folder and keep the best clips overall."""
    import math
    import time as _t
    c = getattr(args, "campaign_data", None)
    if args.source:
        folder = Path(args.source).expanduser()
        if not folder.is_dir():
            die(f"--bulk needs a folder: {folder}")
    elif c:
        folder = campaign_dir(c["id"]) / "source"
    else:
        die("--bulk needs a campaign (--campaign NAME) or a folder of videos")
    vids = sorted(f for f in folder.iterdir() if f.suffix.lower() in VIDEO_EXT)
    if c and folder.resolve() != (campaign_dir(c["id"]) / "source").resolve():
        vids = [Path(file_into_campaign(str(v), c)) for v in vids]
    if not vids:
        die(f"no videos in {folder}")
    lo, hi = (float(x) for x in args.len.split("-"))
    target = args.target or (lo + hi) / 2
    seen, uniq = {}, []
    for v in vids:                                   # "Clip 03.mp4" and "Clip 03 (1).mp4" twice
        key = (v.stat().st_size, round(duration_of(v), 1))
        if key in seen:
            print(f"  skipping {v.name} (same as {seen[key]})")
            continue
        seen[key] = v.name
        uniq.append(v)
    vids = uniq
    print(f"> bulk scrape: {len(vids)} videos")
    pool, words_of, speech, visual = [], {}, 0.0, 0.0
    for i, v in enumerate(vids, 1):
        print(f"\n=== scan {i}/{len(vids)}: {v.name} ===")
        try:
            segs, cands = find_moments(v, args, lo, hi, target)
        except SystemExit:
            print(f"  ! nothing usable in {v.name}")
            continue
        if cands and cands[0].get("visual"):
            visual += min(duration_of(v), hi)
        else:
            speech += segs[-1]["end"] - segs[0]["start"]
        words_of[str(v)] = [w for s in segs for w in s["words"]]
        for cd in cands:
            cd["src"], cd["vid"] = str(v), v.stem
            if not cd.get("title"):
                cd["title"] = (f"at {short_ts(cd['start'])}" if cd.get("visual")
                               else " ".join(segs[cd["i"]]["text"].split()[:6]))
        pool += cands
    if not pool:
        die("no usable moments found in any video")
    k = args.k or max(3, min(30, round(speech / 150 + visual / 60)))
    cap = max(2, math.ceil(k * 0.4))           # keep some variety between videos
    pool.sort(key=lambda x: x["score"], reverse=True)
    picks, per = [], {}
    for cd in pool:
        if per.get(cd["src"], 0) >= cap:
            continue
        if any(d["src"] == cd["src"] and not (cd["end"] + 5 <= d["start"] or cd["start"] >= d["end"] + 5)
               for d in picks):
            continue
        picks.append(cd)
        per[cd["src"]] = per.get(cd["src"], 0) + 1
        if len(picks) == k:
            break
    # silent b-roll + no music allowed -> mix it into clips that have sound
    mixes = []
    want = args.mix if args.mix is not None else (2 if (c and c.get("music") is False) else 0)
    if want:
        silent = {}
        for cd in pool:
            if cd.get("visual") and not _probe(Path(cd["src"]))[0]:
                if cd["src"] not in silent or cd["score"] > silent[cd["src"]]["score"]:
                    silent[cd["src"]] = cd                 # liveliest bit of each silent video
        broll_pool = sorted(silent.values(), key=lambda x: -x["score"])
        bases = [cd for cd in picks if _probe(Path(cd["src"]))[0] and cd["end"] - cd["start"] >= 10]
        if broll_pool and bases:
            for i, base in enumerate(bases[:want]):
                ins = plan_broll(base, words_of.get(base["src"], []), broll_pool, i)
                if ins:
                    m = dict(base, mix=True, broll=ins, title=base["title"], score=base["score"] + 0.5)
                    mixes.append(m)
            print(f"  + {len(mixes)} mixed clip(s): sound from the best clips, cut with silent product shots")
    picks = picks + mixes
    out = args.out_dir / f"bulk-scrape-{_t.strftime('%d%b-%H%M')}"
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# bulk scrape of {folder}", "# best clips across all videos", ""]
    print(f"\n> top {len(picks)} moments:")
    for n, cd in enumerate(picks, 1):
        cd["name"] = f"{n:02d}-{'mix-' if cd.get('mix') else ''}{slug(cd['vid'], 18)}-{slug(cd['title'], 28)}"
        prev = cd["text"][:100] + ("..." if len(cd["text"]) > 100 else "")
        print(f"  {n}. [{cd['vid'][:30]}] {short_ts(cd['start'])}-{short_ts(cd['end'])}  {cd['title']}")
        lines += [f"# {cd['title']}  (score {cd['score']:.2f})  from {cd['vid']}", f"# \"{prev}\"",
                  f"# source: {cd['src']}", f"{short_ts(cd['start'])} {short_ts(cd['end'])} {cd['name']}", ""]
    (out / "clips.txt").write_text("\n".join(lines))
    if c:
        write_campaign_files(c, out, picks, args.ollama)
    used = ", ".join(f"{Path(s).stem[:25]} ×{n}" for s, n in per.items())
    print(f"  clips per video: {used}")
    if args.dry_run:
        return
    print("\n=== rendering ===")
    for cd in picks:
        make_clip(cd["src"], cd["start"], cd["end"], out / f"{cd['name']}.mp4", args, words_of[cd["src"]],
                  clip_meta(cd), broll=cd.get("broll"))
    print(f"\ndone - {len(picks)} clips in {out}")


def batch_mode(args):
    """Clip every video in a folder (default: the campaign's source folder)."""
    c = getattr(args, "campaign_data", None)
    if args.source:
        folder = Path(args.source).expanduser()
        if not folder.is_dir():
            die(f"--batch needs a folder: {folder}")
    elif c:
        folder = campaign_dir(c["id"]) / "source"
    else:
        die("--batch needs a campaign (--campaign NAME) or a folder of videos")
    vids = sorted(f for f in folder.iterdir() if f.suffix.lower() in VIDEO_EXT)
    if c and folder.resolve() != (campaign_dir(c["id"]) / "source").resolve():
        vids = [Path(file_into_campaign(str(v), c)) for v in vids]   # file them into the campaign
    todo = []
    for v in vids:
        done = args.out_dir / slug(v.stem, 50)
        if not args.redo and done.is_dir() and any(done.glob("*.mp4")):
            print(f"  skipping {v.name} (already has clips - use --redo to do it again)")
            continue
        todo.append(v)
    if not todo:
        print("nothing new to clip")
        return
    print(f"> batch: {len(todo)} video(s)")
    failed = []
    for i, v in enumerate(todo, 1):
        print(f"\n=== video {i}/{len(todo)}: {v.name} ===")
        args.source = str(v)
        try:
            auto_mode(args)
        except SystemExit:
            failed.append(v.name)
            print(f"  ! skipped {v.name}")
    print(f"\nbatch done - {len(todo) - len(failed)} of {len(todo)} videos clipped"
          + (f" (problems with: {', '.join(failed)})" if failed else ""))


def list_mode(args):
    jobs = []
    for i, line in enumerate(Path(args.list).read_text().splitlines(), 1):
        line = line.split("#")[0].strip()
        if not line:
            continue
        bits = line.split(maxsplit=2)
        if len(bits) < 2:
            die(f"{args.list} line {i}: need 'start end [name]'")
        jobs.append((to_seconds(bits[0]), to_seconds(bits[1]),
                     bits[2] if len(bits) > 2 else f"clip{len(jobs) + 1:02d}"))
    src = args.source
    if is_url(src) and len(jobs) > 1:
        src, _ = download_full(src)          # one download beats many partial ones
    for st, en, nm in jobs:
        make_clip(src, st, en, args.out_dir / f"{slug(nm)}.mp4", args)


# ======================================================================
# web interface  (./clipper.py --ui)
# ======================================================================
PWA_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#020418"/>
<text x="256" y="350" font-size="300" text-anchor="middle">👽</text></svg>"""
PWA_MANIFEST = json.dumps({
    "name": "Clipper", "short_name": "Clipper", "start_url": "/", "scope": "/", "display": "standalone",
    "background_color": "#0e0f13", "theme_color": "#0e0f13",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]})
# no offline caching - clipper only works while the PC is on anyway
PWA_SW = "self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('fetch',()=>{});"


def phone_setup(port):
    """Linux: a systemd user service so the page is always on (starts with the PC), plus
    'tailscale serve' for a secure https link (needed for the home-screen app + share-to-gallery)."""
    import time
    if WINDOWS:
        print("On Windows: put the Clipper desktop shortcut in your Startup folder (Win+R → shell:startup)\n"
              "and in Tailscale run:  tailscale serve --bg %d" % port)
        return
    me = Path(__file__).resolve()
    unit = Path.home() / ".config" / "systemd" / "user" / "clipper.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"""[Unit]
Description=Clipper web page
After=network-online.target

[Service]
WorkingDirectory={me.parent}
ExecStart={sys.executable} {me} --ui --port {port}
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
""")
    print(f"✓ wrote {unit}")
    subprocess.run(["pkill", "-f", "clipper.py --ui"], capture_output=True)   # stop a copy started by hand
    time.sleep(1)
    ok = True
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "clipper"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            ok = False
            print("! " + " ".join(cmd) + ": " + (r.stderr or r.stdout).strip())
    if ok:
        r = subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], capture_output=True, text=True)
        print("✓ clipper now runs in the background and starts when the PC boots" +
              ("" if r.returncode == 0 else " (after you log in)"))
        print("  restart it with:  systemctl --user restart clipper")
    else:
        print("! couldn't set up the background service - start clipper by hand as before")
    if shutil.which("tailscale"):
        print("\nSetting up the secure link with Tailscale...\n"
              "  If a link appears below, open it in your browser and switch it on - this waits for you.\n"
              "  (Ctrl+C to skip.)\n", flush=True)
        try:                                   # show Tailscale's own messages - it may wait for approval
            r = subprocess.run(["tailscale", "serve", "--bg", str(port)])
            if r.returncode == 0:
                print("✓ secure link ready - press 📱 on the page to see it")
            else:
                print("! not set up. If it said 'access denied', run once:  sudo tailscale set --operator=$USER"
                      "\n  then run  ./clipper.py --phone-setup  again.")
        except KeyboardInterrupt:
            print("\nskipped - the normal http link still works")
    else:
        print("! Tailscale isn't installed - install it to reach clipper away from home")
    print("Logs:  journalctl --user -u clipper -f")


def phone_info(port):
    """How to reach this clipper from a phone: Tailscale name / LAN address."""
    out = {"tailscale": False, "urls": [], "https": False}
    ts = shutil.which("tailscale")
    if ts:
        try:
            r = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=5)
            st = json.loads(r.stdout or "{}")
            name = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".")
            ips = (st.get("Self") or {}).get("TailscaleIPs") or []
            if name:
                out["tailscale"] = True
                r2 = subprocess.run([ts, "serve", "status", "--json"], capture_output=True, text=True, timeout=5)
                if '"443"' in (r2.stdout or "") or ":443" in (r2.stdout or ""):
                    out["https"] = True
                    out["urls"].append({"url": f"https://{name}/", "what": "Tailscale (secure) - best"})
                out["urls"].append({"url": f"http://{name}:{port}/", "what": "Tailscale"})
                out["urls"] += [{"url": f"http://{ip}:{port}/", "what": "Tailscale IP"} for ip in ips[:1]]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    try:
        import socket
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("10.255.255.255", 1))
        out["urls"].append({"url": f"http://{sk.getsockname()[0]}:{port}/", "what": "Home Wi-Fi only"})
        sk.close()
    except OSError:
        pass
    return out


UI_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0e0f13">
<title>Clipper</title>
<link rel="manifest" href="/manifest.json"><meta name="theme-color" content="#0e0f13">
<link rel="apple-touch-icon" href="/icon.svg"><meta name="apple-mobile-web-app-capable" content="yes">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>👽</text></svg>">
<script>try{var t=localStorage.getItem('clipper_theme');if(t)document.documentElement.dataset.theme=t}catch(e){}</script>
<style>
:root{--field:#0b0c10;--acc3:#ff6a3d;--bg:#0e0f13;--card:#181a21;--line:#262934;--text:#eceef3;--dim:#8b90a0;--acc:#ff3d6e;--acc2:#ffb03d;--ok:#3ddc97}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
     padding:16px 14px 60px;max-width:760px;margin:auto}
h1{font-size:26px;letter-spacing:-.5px;display:flex;align-items:center;gap:8px;margin-bottom:14px}
h1 span{background:linear-gradient(90deg,var(--acc),var(--acc2));-webkit-background-clip:text;color:transparent}
h2{font-size:13px;text-transform:uppercase;letter-spacing:1.2px;color:var(--dim);margin:26px 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}
input[type=text]{width:100%;background:var(--field);border:1px solid var(--line);color:var(--text);
     border-radius:12px;padding:14px;font-size:16px;outline:none}
input[type=text]:focus{border-color:var(--acc)}
.row{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.field{flex:1;min-width:140px}
.field label{display:block;font-size:12px;color:var(--dim);margin-bottom:5px}
.seg{display:flex;background:var(--field);border:1px solid var(--line);border-radius:10px;padding:3px}
.seg button{flex:1;background:none;border:0;color:var(--dim);padding:8px 4px;border-radius:8px;font:inherit;font-size:13px}
.seg button.on{background:var(--line);color:var(--text)}
input[type=range]{width:100%;accent-color:var(--acc)}
select{width:100%;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:10px;
       padding:10px;font:inherit;font-size:14px}
.toggle{display:flex;align-items:center;gap:8px;font-size:14px;margin-top:14px}
.toggle input{width:20px;height:20px;accent-color:var(--acc)}
.go{width:100%;margin-top:16px;padding:15px;border:0;border-radius:12px;font:600 17px system-ui;color:#fff;
    background:linear-gradient(90deg,var(--acc),var(--acc3));cursor:pointer}
.go:active{transform:scale(.98)}
.go:disabled{opacity:.5}
details{margin-top:12px;color:var(--dim);font-size:13px}
summary{cursor:pointer}
.job{margin-bottom:10px}
.job .top{display:flex;justify-content:space-between;gap:10px;align-items:start}
.job .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job .s{color:var(--dim);font-size:13px}
.bar{height:6px;background:var(--field);border-radius:4px;margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .6s}
.job.done .bar i{background:var(--ok)}
.job.error .s{color:var(--acc)}
pre{white-space:pre-wrap;font-size:11.5px;background:var(--field);padding:10px;border-radius:8px;margin-top:8px;
    max-height:220px;overflow:auto;color:#b6bac6}
.small{background:none;border:1px solid var(--line);color:var(--dim);border-radius:8px;padding:5px 10px;font-size:12px}
.group{margin-bottom:20px}
.gname{font-weight:600;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.clip{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.clip video{width:100%;aspect-ratio:9/16;background:#000;display:block;object-fit:cover}
.clip .n{font-size:12px;padding:7px 8px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.clip .a{display:flex;gap:6px;padding:7px 8px 9px}
.clip .a a,.clip .a button{flex:1;text-align:center;text-decoration:none;font-size:12px;padding:7px 0;border-radius:8px;
    border:0;background:var(--line);color:var(--text)}
.clip .a button{background:none;border:1px solid var(--line);color:var(--dim)}
.empty{color:var(--dim);text-align:center;padding:24px}
.capbtn{display:block;width:calc(100% - 16px);margin:7px 8px 0;padding:7px 0;border:0;border-radius:8px;
  background:linear-gradient(90deg,var(--acc),var(--acc3));color:#fff;font-size:12px;font-weight:600}
select:disabled{opacity:.45}
.capdet{margin-top:10px}
.smartgrid{display:grid;grid-template-columns:1fr 1fr;gap:4px 14px;margin:8px 0 4px}
@media (max-width:620px){.smartgrid{grid-template-columns:1fr}}
.rvman{display:flex;gap:10px;align-items:flex-start;background:#2a2110;border:1px solid #6b5420;border-radius:10px;
  padding:8px 10px;margin:0 0 8px;font-size:12.5px;color:#ffd98a}
.rvman input{width:20px;height:20px;margin-top:2px;accent-color:#ffb03d;flex-shrink:0}
.rvman.shake{animation:shk .35s}@keyframes shk{25%{transform:translateX(-6px)}75%{transform:translateX(6px)}}
.gpubox{margin-top:14px;background:var(--field);border:1px solid var(--line);border-radius:12px;padding:10px}
.plset summary{color:var(--dim);font-size:13px;cursor:pointer}
.pllist{max-height:55vh;overflow:auto}
.plday{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin:12px 0 4px}
.plrow{display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line);font-size:13px}
.plrow img{width:30px;aspect-ratio:9/16;object-fit:cover;border-radius:4px}
.plrow .tm{width:46px;font-weight:600}.plrow .pf{width:66px;color:var(--acc2)}
.plrow .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plrow button{font-size:11px;padding:4px 7px}.plrow.done{opacity:.4}
.rstats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.rstat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px;text-align:center}
.rstat b{display:block;font-size:20px}.rstat span{font-size:11px;color:var(--dim)}
.rtable{width:100%;border-collapse:collapse;font-size:13px}
.rtable td,.rtable th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
.rtable th{color:var(--dim);font-weight:500;font-size:11.5px}
.rtable a,#pdlist a{color:var(--acc2)}
.rtable input{width:80px;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:6px;padding:4px 6px}
.rtable tr.stale input{border-color:#ffb03d}
.rsec{font-size:13px;color:var(--dim);margin:14px 0 6px;text-transform:uppercase;letter-spacing:1px}
.trrow{display:flex;gap:6px;align-items:center;margin-top:8px}.trrow b{width:44px}.trrow code{flex:1;text-align:center;font-size:14px}
.trwords{font-size:12px;color:var(--dim);margin:3px 0 0 50px;min-height:16px}.trwords i{color:var(--text);font-style:normal;background:var(--line);padding:0 3px;border-radius:4px}
.phurl{display:flex;gap:8px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:8px;margin:6px 0}
.phurl code{flex:1;word-break:break-all;font-size:12.5px}.phurl span{font-size:11px;color:var(--dim)}
.fplist{max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.fplist div{padding:8px 10px;border-bottom:1px solid var(--line);cursor:pointer;font-size:13.5px}
.fplist div:hover{background:var(--line)}
#fpnew{background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px 10px;font:inherit}
.projroot{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:10px}
.projroot code{font-size:12.5px;word-break:break-all}
.postedb{padding:2px 8px 0;font-size:11px;color:var(--ok)}
.capsplit{display:flex;gap:5px;margin:7px 8px 0}.capsplit .capbtn{margin:0;width:auto;flex:1;font-size:11px}
.plats{display:flex;flex-wrap:wrap;gap:5px;padding:7px 8px 0}
.pt{flex:1 1 auto;min-width:0;border:1px solid var(--line);background:none;color:var(--dim);border-radius:8px;
  font-size:11px;padding:5px 6px;cursor:pointer;line-height:1.25;text-align:center;white-space:nowrap}
.pt b{display:block;font-size:12px;color:var(--fg,#eee)}
.pt.now{border-color:#ff4d8d;color:#ff8db5}
.pt.timer{border-color:#ffb03d;color:#ffb03d}
.pt.late{border-color:#ff4d4d;color:#ff6b6b;background:#ff4d4d1a}
.pt.done{border-color:var(--ok);color:var(--ok);cursor:default}
.pt.off{opacity:.45}
.stars{padding:7px 8px 0;color:#ffcf3d;font-size:14px;letter-spacing:1px}.stars b{color:var(--text);font-size:12px;letter-spacing:0}
.why{padding:1px 8px 0;font-size:11px;color:var(--dim)}
.readybar{display:flex;gap:10px;align-items:center;background:#123726;border:1px solid var(--ok);border-radius:14px;
  padding:10px 12px;margin-bottom:14px;font-size:14px}.readybar span{flex:1}
#updbtn.has{border-color:var(--acc2);color:var(--acc2)}
.warns{padding:4px 8px 0}
.warn{font-size:11px;color:#ffb03d;line-height:1.35}
.cchk{margin-top:6px;font-size:12.5px}.cchk div{margin-top:2px}
.cchk .warn,.cchk .error{color:#ffb03d}.cchk .error{color:var(--acc)}.cchk .info{color:var(--dim)}.cchk .ok{color:var(--ok)}
.gwarn{color:#ffb03d;font-size:12px}
.tabs{display:flex;gap:6px;margin:26px 0 10px}
.tab{background:none;border:1px solid var(--line);color:var(--dim);border-radius:20px;padding:6px 14px;font:inherit;font-size:13px}
.tab.on{background:var(--line);color:var(--text)}
.hist{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px;margin-bottom:8px}
.hist .top{display:flex;gap:10px;align-items:baseline;justify-content:space-between}
.hist .st{font-size:11px;padding:2px 8px;border-radius:10px;white-space:nowrap}
.st.done{background:#173d2c;color:var(--ok)}.st.error{background:#3d1722;color:var(--acc)}.st.cancelled{background:#2b2b2b;color:var(--dim)}
.hist .meta{color:var(--dim);font-size:12px;margin-top:3px}
.gbtn{background:none;border:1px solid var(--acc2);color:var(--acc2);border-radius:8px;padding:4px 10px;font-size:12px}
.rv{background:#08090c;color:var(--text);border:1px solid var(--line);border-radius:18px;padding:14px;width:min(560px,96vw);max-height:96vh}
.rv::backdrop{background:#000d}
.rvtop{display:flex;justify-content:space-between;align-items:start;gap:10px}
.rvmain{display:flex;align-items:center;gap:6px;margin-top:10px}
.rvvid{position:relative;flex:1;display:flex;justify-content:center}
.rvvid video{height:min(62vh,640px);aspect-ratio:9/16;background:#000;border-radius:12px;max-width:100%}
.rvstamp{position:absolute;top:10px;left:50%;transform:translateX(-50%);font-weight:700;padding:4px 12px;border-radius:20px;display:none}
.rvstamp.keep{display:block;background:#173d2cee;color:var(--ok)}.rvstamp.drop{display:block;background:#3d1722ee;color:var(--acc)}
.rvnav{background:var(--card);border:1px solid var(--line);color:var(--text);border-radius:50%;width:40px;height:40px;font-size:24px;line-height:1}
.rvcap{font-size:12px;color:var(--dim);white-space:pre-line;margin:8px 0;max-height:60px;overflow:auto}
.rvbtns{display:flex;gap:10px}
.rvbtns button{flex:1;padding:13px;border-radius:12px;font:600 16px system-ui;border:0}
.rvbtns small{opacity:.6;font-weight:400;font-size:11px;margin-left:4px}
.rvkeep{background:linear-gradient(90deg,#1fb37a,#3ddc97);color:#06281a}
.rvdrop{background:#2a1a20;color:var(--acc);border:1px solid #4a2230 !important}
.rvstrip{display:flex;gap:5px;overflow-x:auto;margin:10px 0}
.rvstrip img{width:40px;aspect-ratio:9/16;object-fit:cover;border-radius:5px;border:2px solid transparent;opacity:.75;cursor:pointer;flex-shrink:0}
.rvstrip img.cur{border-color:#fff;opacity:1}.rvstrip img.keep{border-color:var(--ok);opacity:1}.rvstrip img.drop{opacity:.25}
.cdlg{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:16px;padding:16px;
  width:min(680px,94vw);max-height:90vh}
.cdlg::backdrop{background:#000b}
.cdhead{display:flex;justify-content:space-between;align-items:center;font-size:17px}
.cdlg textarea{width:100%;background:var(--field);border:1px solid var(--line);color:var(--text);border-radius:10px;
  padding:10px;font:inherit;font-size:13px;margin-top:4px;resize:vertical}
.cdgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cdgrid>label{font-size:12px;color:var(--dim);display:block}
.cdgrid>label input[type=text]{margin-top:4px;padding:9px;font-size:14px}
.cdgrid .wide{grid-column:1/-1}
.dlbox{margin-top:14px;background:var(--field);border:1px solid var(--line);border-radius:12px;padding:10px}
.dlhead{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}
.dllist{margin-top:8px;max-height:220px;overflow:auto}
.dllist label{display:flex;gap:8px;align-items:center;padding:6px 2px;border-bottom:1px solid #1c1f28;font-size:13.5px}
.dllist label span.m{margin-left:auto;color:var(--dim);font-size:12px;white-space:nowrap}
.dllist .match{color:var(--acc2);font-size:11px}
.cdgrid .chk{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--text)}
.chips{display:flex;gap:12px;flex-wrap:wrap;margin-top:6px;color:var(--text);font-size:14px}
@media(max-width:520px){.cdgrid{grid-template-columns:1fr}}
.bulkbox{margin-top:12px;background:#10131c;border:1px solid #2b3550;border-radius:12px;padding:12px}
.bulkhead{font-weight:700;font-size:15px}
.autonote{font-size:12.5px;color:var(--dim);background:var(--field);border-radius:10px;padding:10px;margin-top:10px}
.looks{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.looks button{background:var(--field);border:1px solid var(--line);color:var(--dim);border-radius:999px;
  padding:7px 12px;font:inherit;font-size:13px;cursor:pointer}
.looks button.on{border-color:var(--acc);color:var(--text);background:#2a1a22}
.autonote a{color:var(--acc2)}
.capwrap.auto .capctl{opacity:.6}
.capwrap.auto .pv{opacity:.2}
.autotag{display:none;position:absolute;inset:0;align-items:center;justify-content:center;text-align:center;
  font-size:12px;font-weight:600;color:#ddd}
.capwrap.auto .autotag{display:flex}
#capbody{font-size:13px;color:var(--dim)}
dialog#rsdlg{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:16px;padding:14px;
  width:calc(100% - 24px);max-width:560px;max-height:calc(100dvh - 24px);overflow:auto;margin:auto}
dialog#rsdlg::backdrop{background:#000b}
.rshead{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:15px}
.rshead b{word-break:break-all}
.rsnow{font-size:12.5px;color:var(--dim);margin-top:6px}
.clip .vw{position:relative}
.clip .ov{display:none;position:absolute;inset:0;background:#000b;color:#fff;align-items:center;justify-content:center;
  text-align:center;font-size:13px;font-weight:600;padding:10px}
.clip.busy .ov{display:flex}
.clip .badge{display:none;position:absolute;top:8px;left:8px;background:var(--ok);color:#06210f;font-size:12px;
  font-weight:700;border-radius:8px;padding:3px 8px}
.clip.fresh{border-color:var(--ok);box-shadow:0 0 0 2px var(--ok)}
.clip.fresh .badge{display:block}
.clip .a button.rs{border-color:var(--acc2);color:var(--acc2)}
.capdet summary{color:var(--dim);font-size:14px}
.capwrap{display:flex;gap:14px;margin-top:10px;flex-wrap:wrap}
.capctl{flex:1;min-width:220px}
.capctl label{font-size:12px;color:var(--dim)}
.cols{display:flex;gap:10px}.cols>div{flex:1}
input[type=color]{width:100%;height:36px;border:1px solid var(--line);border-radius:8px;background:var(--field);padding:2px}
.prev{width:162px;height:288px;border-radius:14px;overflow:hidden;position:relative;flex-shrink:0;margin:auto;
  background:linear-gradient(160deg,#3a2f1a,#14161c 45%,#1d2a33);display:flex;justify-content:center;border:1px solid var(--line)}
.pv{position:absolute;left:8px;right:8px;text-align:center;font-weight:800;line-height:1.15}
.pvmatch{position:absolute;top:6px;right:6px;font-size:9px;background:#0009;color:#ffb03d;padding:2px 5px;border-radius:5px;display:none}

/* ===== terminal-style themes: San Jose Sharks + SNXFFBXXTS (kitty prompt colours) ===== */
.themepick{width:auto;font-size:12px;padding:5px 8px;border-radius:8px}
html[data-theme=sharks]{--bg:#030708;--card:#081113;--line:#0f3a3e;--text:#cfeeed;--dim:#5b8587;
  --acc:#00a3ad;--acc2:#e57200;--acc3:#007f88;--ok:#2fd6a3;--field:#010405;
  --onacc:#001214;--glow:#00a3ad66;--scan:#00a3ad08;--bulkbg:#04191b;--segon:#06363a;--readybg:#05282b;--pre:#7fd8d4;
  --hot:#e57200;--title:var(--acc);--brk:var(--acc2)}
html[data-theme=snxff]{--bg:#020418;--card:#060a24;--line:#16234a;--text:#e6fff9;--dim:#5e6a7d;
  --acc:#00ffcc;--acc2:#ff69b4;--acc3:#56e0d0;--ok:#00ffcc;--field:#010312;
  --onacc:#020418;--glow:#00ffcc55;--scan:#00ffcc07;--bulkbg:#081036;--segon:#0d2a3a;--readybg:#062a2a;--pre:#56e0d0;
  --hot:#ff5f00;--title:#ff69b4;--brk:#00ffcc;--gold:#cecb00}
:is(html[data-theme=sharks],html[data-theme=snxff]) body{font:14px/1.5 "JetBrains Mono","Fira Code","Cascadia Mono","DejaVu Sans Mono",Consolas,monospace;letter-spacing:.2px}
:is(html[data-theme=sharks],html[data-theme=snxff]) *{border-radius:0 !important}
:is(html[data-theme=sharks],html[data-theme=snxff]) button,:is(html[data-theme=sharks],html[data-theme=snxff]) input,:is(html[data-theme=sharks],html[data-theme=snxff]) select,:is(html[data-theme=sharks],html[data-theme=snxff]) textarea,:is(html[data-theme=sharks],html[data-theme=snxff]) .go{font-family:inherit !important}
:is(html[data-theme=sharks],html[data-theme=snxff]) body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:9999;
  background:repeating-linear-gradient(0deg,#0000 0 2px,var(--scan) 2px 3px)}
:is(html[data-theme=sharks],html[data-theme=snxff]) h1{font-weight:700;letter-spacing:0}
:is(html[data-theme=sharks],html[data-theme=snxff]) h1 span{background:none;color:var(--title);-webkit-background-clip:initial;text-shadow:0 0 8px var(--glow)}
:is(html[data-theme=sharks],html[data-theme=snxff]) h1 span::before{content:"[ ";color:var(--brk)}
:is(html[data-theme=sharks],html[data-theme=snxff]) h1 span::after{content:" ]_";color:var(--brk);animation:blink 1.1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
:is(html[data-theme=sharks],html[data-theme=snxff]) .card,:is(html[data-theme=sharks],html[data-theme=snxff]) .job,:is(html[data-theme=sharks],html[data-theme=snxff]) .clip,:is(html[data-theme=sharks],html[data-theme=snxff]) .bulkbox,:is(html[data-theme=sharks],html[data-theme=snxff]) .projroot,:is(html[data-theme=sharks],html[data-theme=snxff]) .group,:is(html[data-theme=sharks],html[data-theme=snxff]) .hist{border-style:dashed !important;border-color:var(--line) !important}
:is(html[data-theme=sharks],html[data-theme=snxff]) .bulkbox{background:var(--bulkbg) !important}
:is(html[data-theme=sharks],html[data-theme=snxff]) .go,:is(html[data-theme=sharks],html[data-theme=snxff]) #bulkgo{background:var(--acc) !important;color:var(--onacc) !important;
  text-transform:uppercase;letter-spacing:1px;box-shadow:0 0 0 1px var(--acc),0 0 14px var(--glow)}
:is(html[data-theme=sharks],html[data-theme=snxff]) .go::before{content:"> "}
:is(html[data-theme=sharks],html[data-theme=snxff]) .capbtn,:is(html[data-theme=sharks],html[data-theme=snxff]) .rvkeep{background:var(--acc2) !important;color:var(--onacc) !important}
:is(html[data-theme=sharks],html[data-theme=snxff]) .tab::before{content:"[ "}:is(html[data-theme=sharks],html[data-theme=snxff]) .tab::after{content:" ]"}
:is(html[data-theme=sharks],html[data-theme=snxff]) .tab.on{background:var(--acc);color:var(--onacc)}
:is(html[data-theme=sharks],html[data-theme=snxff]) .bar i{background:repeating-linear-gradient(90deg,var(--acc) 0 8px,#0000 8px 10px)}
:is(html[data-theme=sharks],html[data-theme=snxff]) .seg button.on,:is(html[data-theme=sharks],html[data-theme=snxff]) .looks button.on{background:var(--segon) !important;color:var(--text)}
:is(html[data-theme=sharks],html[data-theme=snxff]) .looks button.on{border-color:var(--acc)}
:is(html[data-theme=sharks],html[data-theme=snxff]) .stars{color:var(--gold,var(--acc2))}
:is(html[data-theme=sharks],html[data-theme=snxff]) .readybar{background:var(--readybg)}
:is(html[data-theme=sharks],html[data-theme=snxff]) h2::before{content:"// ";color:var(--hot)}
:is(html[data-theme=sharks],html[data-theme=snxff]) pre{color:var(--pre)}
:is(html[data-theme=sharks],html[data-theme=snxff]) .pt.now{border-color:var(--hot);color:var(--hot)}
:is(html[data-theme=sharks],html[data-theme=snxff]) .pt.timer{border-color:var(--acc);color:var(--acc)}
:is(html[data-theme=sharks],html[data-theme=snxff]) .gname>span:first-child{color:var(--acc3)}
html[data-theme=snxff] h1{color:var(--dim)}
html[data-theme=snxff] .gbtn{color:var(--hot) !important;border-color:var(--hot) !important}
html[data-theme=snxff] .warn,html[data-theme=snxff] .gwarn{color:var(--gold)}
</style></head><body>
<h1>👽 <span>Clipper</span>
  <select id="themepick" class="themepick" title="Theme" style="margin-left:auto">
    <option value="">Woke as fuck</option><option value="sharks">San Jose Sharks</option><option value="snxff">SNXFFBXXTS</option></select>
  <button type="button" class="small" onclick="phoneDlg()" title="Use clipper on your phone">📱</button>
  <button type="button" class="small" id="updbtn" title="Get the latest clipper from GitHub">⟳ Update</button>
  <button type="button" class="small" id="aibtn">⚙️ AI</button></h1>
<div id="readybar" class="readybar" style="display:none"><span id="readytxt"></span>
  <button type="button" class="go" id="readygo" style="margin:0;padding:8px 14px;width:auto">🎬 Review now</button>
  <button type="button" class="small" onclick="$('#readybar').style.display='none'">✕</button></div>
<dialog id="aidlg" class="cdlg">
 <div class="cdhead"><b>AI brain</b><button type="button" class="small" id="aiclose">✕</button></div>
 <p class="s" style="color:var(--dim);font-size:13px;margin:8px 0">Optional. With an AI switched on, clipper uses it to read
   campaign briefs, pick the best moments and write captions. Without one it uses its built-in rules.</p>
 <div class="seg" id="aiprov"><button type="button" data-v="">Off</button><button type="button" data-v="ollama">Ollama (free, on this PC)</button>
   <button type="button" data-v="claude">Claude (API key)</button></div>
 <div id="ai_ollama" class="cdgrid" style="margin-top:12px"><label class="wide">Ollama model
   <input type="text" id="ai_om" placeholder="e.g. llama3.2"></label>
   <div class="wide s" style="font-size:12px;color:var(--dim)">Install Ollama (Linux: <code>sudo pacman -S ollama</code> + <code>sudo systemctl enable --now ollama</code>; Windows: <code>winget install Ollama.Ollama</code>), then
   <code>ollama pull llama3.2</code></div></div>
 <div id="ai_claude" class="cdgrid" style="margin-top:12px"><label class="wide">API key <span id="ai_haskey" style="color:var(--ok)"></span>
   <input type="text" id="ai_key" placeholder="sk-ant-… (leave empty to keep the saved key)" autocomplete="off"></label>
   <label class="wide">Model<select id="ai_cm"><option value="claude-haiku-5-5">Claude Haiku 5.5 - cheapest, fast</option>
     <option value="claude-sonnet-5-5">Claude Sonnet 5.5 - smarter, costs more</option></select></label>
   <div class="wide s" style="font-size:12px;color:var(--dim)">Get a key at console.anthropic.com (pay as you go). It's saved only on this PC, in settings.json.</div></div>
 <div class="gpubox"><b>⚡ Speed</b><div id="gpumsg" class="s" style="font-size:13px;margin:4px 0">Checking your graphics card…</div>
   <button type="button" class="small" id="gpusetup" style="display:none">Set up GPU</button></div>
 <div class="row" style="margin-top:12px"><button type="button" class="small" id="aitest">Test</button>
   <span id="aimsg" class="s" style="font-size:13px;align-self:center"></span>
   <button type="button" class="go" id="aisave" style="margin:0;flex:1">Save</button></div>
</dialog>

<form class="card" id="f">
  <input type="text" id="url" placeholder="Paste a YouTube link (or a file path on the PC)" autocomplete="off" required>
  <div style="margin-top:12px"><label style="font-size:12px;color:var(--dim)">Campaign</label>
    <div style="display:flex;gap:8px;margin-top:5px">
      <select id="camp" style="flex:1"><option value="">No campaign (free clipping)</option></select>
      <button type="button" class="small" id="campbest" title="Set everything below to what suits this campaign's brief" style="display:none;white-space:nowrap">🎯 Best for brief</button>
      <button type="button" class="small" id="campedit" title="Edit this campaign" style="display:none">✏️</button>
      <button type="button" class="small" id="campnew" style="white-space:nowrap">+ New</button></div>
    <div id="campnote" class="s" style="color:var(--acc2);font-size:12px;margin-top:5px"></div>
    <div id="campbestmsg" class="s" style="color:var(--ok);font-size:12px;margin-top:3px"></div>
    <div id="campchk" class="cchk"></div>
    <div id="srcbox" style="display:none;margin-top:10px"><label style="font-size:12px;color:var(--dim)">Footage</label>
      <select id="src" style="margin-top:5px"></select>
      <div id="bulkbox" class="bulkbox" style="display:none">
        <div class="bulkhead">🔎 Bulk folder scrape</div>
        <div class="s" id="bulkinfo" style="font-size:12.5px;color:var(--dim);margin:4px 0 8px"></div>
        <label class="toggle" id="mvdlbox" style="margin:2px 0 10px;display:none"><input type="checkbox" id="mvdl" checked>
          <span id="mvdltxt"></span></label>
        <label style="font-size:12px;color:var(--dim)">Best clips overall: <b id="bulkkv">Auto</b></label>
        <input type="range" id="bulkk" min="0" max="30" value="0">
        <button type="button" class="go" id="bulkgo" style="margin-top:10px;padding:12px;
          background:linear-gradient(90deg,#3d7bff,#7a3dff)">🔎 Scrape the whole folder</button>
        <div style="margin-top:8px"><button type="button" class="small" id="batchgo" style="display:none"></button></div>
        <div class="s" id="batchnote" style="font-size:12px;color:var(--dim);margin-top:6px"></div>
      </div></div></div>
  <div class="row">
    <div class="field"><label>Clips: <b id="kv">5</b></label>
      <input type="range" id="k" min="1" max="15" value="5"></div>
    <div class="field"><label>Length</label>
      <div class="seg" data-name="len">
        <button type="button" data-v="15-30">Short</button>
        <button type="button" data-v="20-60" class="on">Medium</button>
        <button type="button" data-v="45-90">Long</button></div></div>
  </div>
  <div class="row">
    <div class="field"><label>Style</label>
      <div class="seg" data-name="mode">
        <button type="button" data-v="blur" class="on">Blur bg</button>
        <button type="button" data-v="crop" title="Zoom to fill the screen - follows the speaker's face (needs face tracking: see the Speed box in ⚙️ AI)">Zoom</button>
        <button type="button" data-v="none">Original</button></div>
      <label class="toggle" id="trackbox" style="display:none;margin-top:6px" title="Turn off when there's more than one person in shot - Zoom then keeps the middle">
        <input type="checkbox" id="track" checked> 🙂 Follow face</label>
    </div>
    <div class="field"><label>Transcription</label>
      <div class="seg" data-name="model">
        <button type="button" data-v="base.en">Fast</button>
        <button type="button" data-v="small.en" class="on">Good</button>
        <button type="button" data-v="medium.en">Best</button></div></div>
  </div>
  <details class="capdet smartdet"><summary>✨ Smart picking: <b id="smartsum">all on</b></summary>
   <div class="smartgrid">
    <label class="toggle" title="Listens for laughter and crowd reactions and prefers clips with them">
      <input type="checkbox" class="smart" data-k="laughs" checked> 😂 Laugh &amp; reaction detection</label>
    <label class="toggle" title="If people laugh right after the punchline, the clip runs on so the laugh is in it">
      <input type="checkbox" class="smart" data-k="laugh_tail" checked> 🎤 Keep the laugh after the punchline</label>
    <label class="toggle" title="Stream links: grabs the chat replay and prefers moments where chat went wild">
      <input type="checkbox" class="smart" data-k="chat" checked> 💬 Stream chat highlights</label>
    <label class="toggle" title="Prefers funny lines and strong opinions; marks down filler and rambling">
      <input type="checkbox" class="smart" data-k="words" checked> 🤣 Funny lines &amp; opinions, skip boring bits</label>
    <label class="toggle" title="Cuts silent pauses and um/uh out of each clip - tighter clips keep viewers watching. Laughs and reactions are kept.">
      <input type="checkbox" class="smart" data-k="cut_pauses" id="cutp" checked> ✂️ Cut pauses &amp; ums</label>
    <label class="toggle" title="Never end on a question or halfway through someone's answer">
      <input type="checkbox" class="smart" data-k="qa" checked> 🎙️ Interview-aware endings</label>
   </div></details>
  <label class="toggle"><input type="checkbox" id="caps" checked> Burn in captions</label>
  <details id="capdet" class="capdet"><summary>Caption style: <b id="capsum">✨ Auto</b></summary>
   <div id="capbody">
   <div class="looks" id="looks"></div>
   <div id="capnote" class="autonote"></div>
   <div class="capwrap">
    <div class="capctl">
      <label class="toggle" style="margin-top:0"><input type="checkbox" id="cs_match">
        Match colours to the video</label>
      <div class="field" style="margin-top:10px"><label>Font</label><select id="cs_font"></select></div>
      <div class="row cols">
        <div><label>Text</label><input type="color" id="cs_text"></div>
        <div><label>Highlight <input type="checkbox" id="cs_hlon" title="highlight the spoken word"></label>
             <input type="color" id="cs_hl"></div>
        <div><label id="cs_outlbl">Outline</label><input type="color" id="cs_out"></div>
      </div>
      <div class="field" style="margin-top:10px"><label>Size: <b id="cs_sizev"></b></label>
        <input type="range" id="cs_size" min="40" max="160"></div>
      <div class="field" style="margin-top:6px"><label>Words at a time: <b id="cs_wordsv"></b></label>
        <input type="range" id="cs_words" min="1" max="6"></div>
      <div class="field" style="margin-top:6px"><label>Position</label>
        <div class="seg" id="cs_pos">
          <button type="button" data-v="top">Top</button><button type="button" data-v="middle">Middle</button>
          <button type="button" data-v="lower">Lower</button><button type="button" data-v="bottom">Bottom</button></div></div>
      <div class="row" style="margin-top:4px">
        <label class="toggle"><input type="checkbox" id="cs_upper"> ALL CAPS</label>
        <label class="toggle"><input type="checkbox" id="cs_pop"> Pop-in</label></div>
      <button type="button" class="small" id="cs_reset" style="margin-top:10px">Reset to look's defaults</button>
    </div>
    <div class="prev" id="prev"><div class="pv" id="pv"></div><div class="autotag">✨ picked<br>per clip</div><div class="pvmatch" id="pvmatch">colours from video</div></div>
   </div>
   </div>
  </details>
  <div class="row">
    <div class="field"><label>Background music</label>
      <select id="music"><option value="">No music</option></select></div>
    <div class="field" id="volbox" style="display:none"><label>Music volume: <b id="mvv">15</b>%</label>
      <input type="range" id="mvol" min="5" max="50" value="15"></div>
  </div>
  <details><summary>Use a specific Ollama model for this run (optional)</summary>
    <div style="margin-top:8px">
      <input type="text" id="ollama" placeholder="model name, e.g. llama3.1 (leave empty to skip)">
    </div></details>
  <button class="go" id="go">Find the best clips</button>
</form>

<dialog id="rsdlg">
  <div class="rshead"><div>🎨 Restyle <b id="rsname"></b></div><button type="button" class="small" id="rsx">✕</button></div>
  <div class="rsnow">Now: <b id="rsnow"></b> — pick a new look or change anything, then re-render.</div>
  <div id="rsslot"></div>
  <button type="button" class="go" id="rsapply">Re-render this clip</button>
  <div class="rsnow" style="text-align:center;margin-top:8px">The old version stays until the new one is ready.</div>
</dialog>

<dialog id="cdlg" class="cdlg">
 <div class="cdhead"><b id="cdtitle">New campaign</b><button type="button" class="small" id="cdclose">✕</button></div>
 <div id="cdstep1">
  <p class="s" style="color:var(--dim);font-size:13px;margin:6px 0 8px">Copy the whole campaign page from Whop (rules, captions, requirements…) and paste it here.</p>
  <textarea id="cdbrief" rows="12" placeholder="Paste the campaign rules here…"></textarea>
  <button type="button" class="go" id="cdread">Read the rules</button>
 </div>
 <div id="cdstep2" style="display:none">
  <div id="cdvia" class="s" style="font-size:12px;color:var(--acc2);margin:6px 0"></div>
  <div class="cdgrid">
   <label>Campaign name<input type="text" id="cf_name"></label>
   <label>Agency (who runs it)<input type="text" id="cf_agency"></label>
   <label class="wide">Platforms
     <span class="chips" id="cf_platforms">
       <label><input type="checkbox" value="TikTok"> TikTok</label><label><input type="checkbox" value="Instagram Reels"> Instagram</label>
       <label><input type="checkbox" value="YouTube Shorts"> YouTube</label><label><input type="checkbox" value="X"> X</label></span></label>
   <label class="wide">Footage link / note<input type="text" id="cf_source_note"></label>
   <label class="chk"><input type="checkbox" id="cf_music"> Background music allowed</label>
   <label>Minimum length (seconds)<input type="text" id="cf_min_len"></label>
   <label>Pay per 1,000 views ($)<input type="text" id="cf_rate" placeholder="e.g. 2.50 (best platform)"></label>
   <label>Clip length range<input type="text" id="cf_len" placeholder="20-60"></label>
   <label>Tag in caption<input type="text" id="cf_mention" placeholder="@brand"></label>
   <div class="wide s" style="font-size:12px;color:var(--dim);margin-top:2px">Different tag on some apps? (leave empty = same tag everywhere)</div>
   <label>TikTok tag<input type="text" class="cf_tag" data-p="TikTok" placeholder="same"></label>
   <label>Instagram tag<input type="text" class="cf_tag" data-p="Instagram Reels" placeholder="same"></label>
   <label>YouTube tag<input type="text" class="cf_tag" data-p="YouTube Shorts" placeholder="same"></label>
   <label>X tag<input type="text" class="cf_tag" data-p="X" placeholder="same"></label>
   <label>Disclosure hashtag<input type="text" id="cf_disclosure" placeholder="#Ad"></label>
   <label class="wide">On-screen text (burned into every clip)<input type="text" id="cf_on_screen"></label>
   <label>Logo watermark file (in the campaign folder)<input type="text" id="cf_logo" placeholder="e.g. logo.png - leave empty for none"></label>
   <label>…or choose a logo image<input type="file" id="cf_logofile" accept="image/png,image/jpeg,image/webp">
     <span id="cf_logomsg" class="s" style="font-size:11.5px;color:var(--ok)"></span></label>
   <label class="wide">Keywords clips should mention (comma separated)<input type="text" id="cf_keywords"></label>
   <label class="wide">Extra hashtags (comma separated, max 3)<input type="text" id="cf_hashtags"></label>
   <label class="wide">Lines every caption must include (one per line)<textarea id="cf_required_lines" rows="2"></textarea></label>
   <label class="chk wide"><input type="checkbox" id="cf_exact"> Captions must be copied <b>word for word</b> from the list below (nothing added)</label>
   <label class="wide">Example / approved captions (one per line)<textarea id="cf_example_captions" rows="4"></textarea></label>
   <label class="wide">Caption style for the AI<textarea id="cf_caption_rules" rows="2"></textarea></label>
   <label class="wide">Things to check by eye in Review (one per line)<textarea id="cf_manual_checks" rows="3"
     placeholder="e.g. No children using the product"></textarea></label>
   <label class="wide">Posting checklist (one per line)<textarea id="cf_checklist" rows="6"></textarea></label>
  </div>
  <div class="dlbox"><div class="dlhead"><b>📥 Footage in your Downloads</b>
    <span class="s" style="font-size:12px;color:var(--dim)">ticked files get moved into this campaign</span></div>
    <div id="cf_files" class="dllist"></div></div>
  <div class="row" style="margin-top:10px"><button type="button" class="small" id="cdback">← Paste again</button>
   <button type="button" class="small" id="cdsave">Publish only</button>
   <button type="button" class="go" id="cdgo" style="margin:0;flex:1">🚀 Publish &amp; clip everything</button></div>
 </div>
</dialog>

<div class="tabs"><button type="button" class="tab on" data-t="jobs">Jobs</button>
  <button type="button" class="tab" data-t="history">History</button>
  <button type="button" class="tab" data-t="results">📈 Results</button>
  <button type="button" class="tab" data-t="projects">📁 Projects</button></div>
<div id="results" style="display:none"></div>
<div id="projects" style="display:none"></div>
<dialog id="trdlg" class="cdlg">
 <div class="cdhead"><b>✂️ Trim <span id="trname"></span></b><button type="button" class="small" onclick="trClose()">✕</button></div>
 <video id="trvid" controls playsinline style="width:100%;max-height:46vh;background:#000;border-radius:10px"></video>
 <div class="s" style="font-size:12.5px;color:var(--dim);margin:6px 0" id="trinfo"></div>
 <div class="trrow"><b>Start</b><button type="button" class="small" data-t="s" data-d="-1">−1s</button><button type="button" class="small" data-t="s" data-d="-0.25">−¼</button>
   <code id="trs"></code><button type="button" class="small" data-t="s" data-d="0.25">+¼</button><button type="button" class="small" data-t="s" data-d="1">+1s</button></div>
 <div class="trwords" id="trsw"></div>
 <div class="trrow"><b>End</b><button type="button" class="small" data-t="e" data-d="-1">−1s</button><button type="button" class="small" data-t="e" data-d="-0.25">−¼</button>
   <code id="tre"></code><button type="button" class="small" data-t="e" data-d="0.25">+¼</button><button type="button" class="small" data-t="e" data-d="1">+1s</button></div>
 <div class="trwords" id="trew"></div>
 <div class="row" style="margin-top:10px;gap:8px"><span id="trmsg" class="s" style="font-size:12.5px;flex:1;align-self:center"></span>
   <button type="button" class="go" id="trgo" style="margin:0;width:auto;padding:10px 16px">Re-make clip</button></div>
</dialog>
<dialog id="phdlg" class="cdlg">
 <div class="cdhead"><b>📱 Use clipper on your phone</b><button type="button" class="small" onclick="$('#phdlg').close()">✕</button></div>
 <div id="phbody" style="font-size:13.5px"></div>
</dialog>
<dialog id="fpdlg" class="cdlg">
 <div class="cdhead"><b>Choose your projects folder</b><button type="button" class="small" onclick="$('#fpdlg').close()">✕</button></div>
 <div class="s" style="font-size:12.5px;color:var(--dim);margin:6px 0 8px">All your campaigns, footage and clips will live in this folder.
   Pick an empty folder (or make a new one), e.g. <i>Clipping</i> in your Documents.</div>
 <div id="fpdrives" class="row" style="gap:6px;flex-wrap:wrap"></div>
 <div class="row" style="gap:6px;align-items:center;margin:6px 0"><button type="button" class="small" id="fpup">⬆ Up</button>
   <button type="button" class="small" id="fphome">🏠 Home</button>
   <code id="fppath" style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1"></code></div>
 <div id="fplist" class="fplist"></div>
 <div class="row" style="gap:6px;margin-top:8px"><input id="fpnew" placeholder="New folder name" style="flex:1">
   <button type="button" class="small" id="fpmk">+ Make folder</button></div>
 <label class="toggle" style="margin-top:8px"><input type="checkbox" id="fpmove"> Move my existing campaigns &amp; clips into it</label>
 <div class="row" style="margin-top:10px;gap:8px"><span id="fpmsg" class="s" style="font-size:12.5px;align-self:center;flex:1"></span>
   <button type="button" class="go" id="fpuse" style="margin:0;width:auto;padding:10px 16px">Use this folder</button></div>
</dialog>
<dialog id="pldlg" class="cdlg">
 <div class="cdhead"><b id="pltitle">Posting plan</b><button type="button" class="small" onclick="$('#pldlg').close()">✕</button></div>
 <details class="plset"><summary>When you're free &amp; your accounts</summary>
  <div class="cdgrid" style="margin-top:8px">
   <label class="wide">Times you can post and submit within 30 min (e.g. 12:00-13:00, 17:30-22:00)<input type="text" id="pl_win"></label>
   <label>Posts per day per platform<input type="text" id="pl_day"></label>
   <label>Hours between posts (same platform)<input type="text" id="pl_gap"></label>
   <label class="wide">Your accounts<span class="chips" id="pl_mine">
     <label><input type="checkbox" value="TikTok"> TikTok</label><label><input type="checkbox" value="Instagram Reels"> Instagram</label>
     <label><input type="checkbox" value="YouTube Shorts"> YouTube</label><label><input type="checkbox" value="X"> X</label></span></label>
  </div><button type="button" class="small" id="plsave" style="margin-top:8px">Save &amp; re-plan</button></details>
 <div id="plinfo" class="s" style="font-size:12.5px;color:var(--dim);margin:8px 0"></div>
 <div id="pllist" class="pllist"></div>
 <div class="row" style="margin-top:10px"><button type="button" class="small" id="plcopy">Copy plan as text</button>
  <button type="button" class="go" id="plics" style="margin:0;flex:1">📅 Add to calendar (with reminders)</button></div>
</dialog>
<dialog id="pdlg" class="cdlg">
 <div class="cdhead"><b id="pdtitle">Log a post</b><button type="button" class="small" onclick="$('#pdlg').close()">✕</button></div>
 <div id="pdlist" style="margin-top:8px"></div>
 <div class="cdgrid" style="margin-top:10px">
   <label>Platform<select id="pd_plat"><option>TikTok</option><option>Instagram Reels</option><option>YouTube Shorts</option><option>X</option></select></label>
   <label>Views so far<input type="text" id="pd_views" placeholder="0"></label>
   <label class="wide">Link to the post<input type="text" id="pd_url" placeholder="https://www.tiktok.com/@…/video/…"></label>
   <label>Likes<input type="text" id="pd_likes" placeholder="optional"></label>
 </div>
 <button type="button" class="go" id="pdadd">＋ Log this post</button>
 <div class="s" style="font-size:12px;color:var(--dim);margin-top:8px">Come back after a few days and update the views in 📈 Results - clipper learns which clips get views.</div>
</dialog>
<div id="jobs"><div class="empty">Nothing running</div></div>
<div id="history" style="display:none"></div>

<dialog id="rv" class="rv">
 <div class="rvtop"><div><b id="rvgroup"></b><div class="s" id="rvpos" style="color:var(--dim);font-size:12px"></div></div>
   <button type="button" class="small" id="rvclose">✕</button></div>
 <div class="rvmain">
   <button type="button" class="rvnav" id="rvprev">‹</button>
   <div class="rvvid"><video id="rvvideo" controls playsinline autoplay></video><div id="rvstamp" class="rvstamp"></div></div>
   <button type="button" class="rvnav" id="rvnext">›</button>
 </div>
 <div class="rvcap" id="rvcap"></div>
 <label class="rvman" id="rvman" style="display:none"><input type="checkbox" id="rvmanok">
   <span><b>Check by eye before keeping:</b><span id="rvmanlist"></span></span></label>
 <div class="rvbtns"><button type="button" class="rvdrop" id="rvdrop">✕ Drop <small>D</small></button>
   <button type="button" class="rvkeep" id="rvkeep">⭐ Keep <small>K</small></button></div>
 <div class="rvstrip" id="rvstrip"></div>
 <button type="button" class="go" id="rvdone">Done</button>
</dialog>

<h2>Your clips</h2>
<div id="clips"><div class="empty">Loading…</div></div>

<script>
const $=s=>document.querySelector(s);
const enc=p=>p.split('/').map(encodeURIComponent).join('/');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(p,body){
  const r=await fetch(p,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  return r.json();
}
const opt={len:'20-60',mode:'blur',model:'small.en'};
function trackShow(){const z=document.querySelector('.seg[data-name=mode] button.on');
  $('#trackbox').style.display=(z&&z.dataset.v==='crop')?'':'none'}
document.querySelectorAll('.seg[data-name=mode] button').forEach(b=>b.addEventListener('click',()=>setTimeout(trackShow,0)));
try{const t=localStorage.getItem('clipper_track'); if(t!==null)$('#track').checked=t==='1'}catch(e){}
$('#track').onchange=()=>{try{localStorage.setItem('clipper_track',$('#track').checked?'1':'0')}catch(e){}};
function smartOpts(){const o={};document.querySelectorAll('.smart').forEach(b=>o[b.dataset.k]=b.checked);return o}
function smartSum(){const o=smartOpts(),off=Object.values(o).filter(v=>!v).length;
  $('#smartsum').textContent=off?`${Object.keys(o).length-off} of ${Object.keys(o).length} on`:'all on'}
try{const t=JSON.parse(localStorage.getItem('clipper_smart')||'{}');
  document.querySelectorAll('.smart').forEach(b=>{if(b.dataset.k in t)b.checked=!!t[b.dataset.k]})}catch(e){}
document.querySelectorAll('.smart').forEach(b=>b.onchange=()=>{try{localStorage.setItem('clipper_smart',JSON.stringify(smartOpts()))}catch(e){};smartSum()});
smartSum();
document.querySelectorAll('.seg').forEach(seg=>seg.addEventListener('click',e=>{
  const b=e.target.closest('button'); if(!b) return;
  seg.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
  opt[seg.dataset.name]=b.dataset.v;
}));
$('#k').oninput=e=>$('#kv').textContent=e.target.value;
$('#mvol').oninput=e=>$('#mvv').textContent=e.target.value;
$('#music').onchange=e=>$('#volbox').style.display=e.target.value?'':'none';
async function loadMusic(){
  const r=await api('/api/music'), sel=$('#music'), cur=sel.value;
  sel.innerHTML='<option value="">No music</option>'+
    (r.songs.length?'<option value="__random__">🎲 Random song each clip</option>':'')+
    r.songs.map(n=>`<option value="${esc(n)}">${esc(n.replace(/\.[^.]+$/,''))}</option>`).join('');
  sel.value=[...sel.options].some(o=>o.value===cur)?cur:'';
  if(!r.songs.length) sel.options[0].text='No music (add songs to '+r.folder+')';
}
loadMusic();

/* ---------- caption style ---------- */
// cs = your own style (always complete); cs.auto = let Clipper pick per clip instead
let PRESETS={}, cs={auto:true};
const CS_KEY='clipper_capstyle';
let restyleTarget=null, mainCs=null;
function csLoadSaved(){try{return JSON.parse(localStorage.getItem(CS_KEY)||'null')}catch(e){return null}}
function csSave(){if(restyleTarget)return;try{localStorage.setItem(CS_KEY,JSON.stringify(cs))}catch(e){}}
function csFromPreset(name){
  const p=PRESETS[name]; cs={auto:false,preset:name,match:cs.match||false,font:p.font,size:p.size,text:p.text,
    hl:p.highlight||'#FFE14D',hlon:!!p.highlight,out:p.outline,pos:p.pos,words:p.words,upper:p.upper,pop:p.pop};
}
function csLabel(){return cs.auto?'✨ Auto':((PRESETS[cs.preset]||{}).label||cs.preset)+(cs.match?' · matched':'')}
function capPayload(){return {font:cs.font,size:cs.size,text:cs.text,highlight:cs.hlon?cs.hl:'',outline:cs.out,
  pos:cs.pos,upper:cs.upper,pop:cs.pop}}
function csChrome(){            // look buttons, summary, explanation
  document.querySelectorAll('#looks button').forEach(b=>b.classList.toggle('on',cs.auto?b.dataset.v==='auto':b.dataset.v===cs.preset));
  $('#capsum').textContent=csLabel();
  $('.capwrap').classList.toggle('auto',cs.auto);
  $('#capnote').innerHTML=cs.auto
    ?'✨ <b>Auto</b> picks the look from the colours, brightness and how fast people talk. '+
     '<b>Tap a look above, or change anything below</b>, to use your own style instead.'
    :`Using your own style: <b>${esc(csLabel())}</b> — exactly as shown. <a href="#" id="toauto">Switch back to ✨ Auto</a>`;
  const ta=$('#toauto'); if(ta) ta.onclick=e=>{e.preventDefault();cs.auto=true;csChrome();csSave()};
}
function csToUI(){
  if(![...$('#cs_font').options].some(o=>o.value===cs.font)) $('#cs_font').add(new Option(cs.font,cs.font));
  $('#cs_font').value=cs.font; $('#cs_text').value=cs.text; $('#cs_hl').value=cs.hl; $('#cs_hlon').checked=cs.hlon;
  $('#cs_out').value=cs.out; $('#cs_size').value=cs.size; $('#cs_words').value=cs.words; $('#cs_match').checked=cs.match;
  $('#cs_upper').checked=cs.upper; $('#cs_pop').checked=cs.pop;
  document.querySelectorAll('#cs_pos button').forEach(b=>b.classList.toggle('on',b.dataset.v===cs.pos));
  $('#cs_outlbl').textContent=(PRESETS[cs.preset]||{}).box?'Box':((PRESETS[cs.preset]||{}).glow?'Glow':'Outline');
  csChrome(); csPreview();
}
function csFromUI(){             // touching any control = use your own style
  cs.auto=false; cs.match=$('#cs_match').checked; cs.font=$('#cs_font').value;
  cs.text=$('#cs_text').value; cs.hl=$('#cs_hl').value; cs.hlon=$('#cs_hlon').checked; cs.out=$('#cs_out').value;
  cs.size=+$('#cs_size').value; cs.words=+$('#cs_words').value; cs.upper=$('#cs_upper').checked; cs.pop=$('#cs_pop').checked;
  csChrome(); csPreview(); csSave();
}
function csPreview(){
  const p=PRESETS[cs.preset]||{}, pv=$('#pv'), k=288/1920;
  $('#cs_sizev').textContent=cs.size; $('#cs_wordsv').textContent=cs.words;
  $('#pvmatch').style.display=cs.match&&!cs.auto?'':'none';
  const sample=['the','new','site','is','called','X01'].slice(0,cs.words);
  pv.innerHTML=sample.map((w,i)=>{let t=cs.upper?w.toUpperCase():w;
    return (cs.hlon&&i===Math.min(1,sample.length-1))?`<span style="color:${cs.hl}">${t}</span>`:t}).join(' ');
  pv.style.fontFamily=`"${cs.font}", sans-serif`; pv.style.fontSize=(cs.size*k*1.05)+'px'; pv.style.color=cs.text;
  const o=Math.max(1,(p.outline_w||4)*k*0.9);
  if(p.box){pv.style.textShadow='none';pv.innerHTML=`<span style="background:${cs.out}cc;padding:2px 5px;
    box-decoration-break:clone;-webkit-box-decoration-break:clone">${pv.innerHTML}</span>`}
  else if(p.glow){pv.style.textShadow=`0 0 ${o*2}px ${cs.out},0 0 ${o*4}px ${cs.out},0 0 1px ${cs.out}`}
  else{const sh=[];for(let a=0;a<16;a++){sh.push(`${(Math.cos(a/8*Math.PI)*o).toFixed(1)}px ${(Math.sin(a/8*Math.PI)*o).toFixed(1)}px 0 ${cs.out}`)}
    if(p.shadow) sh.push(`${o}px ${o*1.2}px ${o}px #0008`); pv.style.textShadow=sh.join(',')}
  const pos={top:['8%',null],middle:['50%',null],lower:[null,'27%'],bottom:[null,'12%']}[cs.pos];
  pv.style.top=pos[0]||''; pv.style.bottom=pos[1]||''; pv.style.transform=cs.pos==='middle'?'translateY(-50%)':'';
}
async function csInit(){
  const [styles,fonts]=await Promise.all([api('/api/styles'),api('/api/fonts')]);
  PRESETS=styles;
  $('#looks').innerHTML='<button type="button" data-v="auto">✨ Auto</button>'+
    Object.entries(PRESETS).map(([k,p])=>`<button type="button" data-v="${k}">${esc(p.label)}</button>`).join('');
  $('#cs_font').innerHTML=fonts.map(f=>`<option value="${esc(f)}">${esc(f)}</option>`).join('');
  const saved=csLoadSaved();
  if(saved&&PRESETS[saved.preset]&&saved.font){cs=saved; cs.auto=saved.auto!==false}
  else{csFromPreset('highlight'); cs.auto=true}
  csToUI();
}
$('#looks').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
  const v=b.dataset.v;
  if(v==='auto') cs.auto=true;
  else if(v===cs.preset) cs.auto=false;               // back to your tweaks of this look
  else{const m=cs.match;csFromPreset(v);cs.match=m}
  csToUI();csSave()});
async function restyle(path,name){
  const st=await api('/api/clipstyle?path='+encodeURIComponent(path));
  if(st.error){alert(st.error);return}
  mainCs=JSON.parse(JSON.stringify(cs)); restyleTarget=path; $('#rsname').textContent=name;
  cs={auto:false,preset:PRESETS[st.preset]?st.preset:'highlight',match:false,font:st.font,size:st.size,text:st.text,
      hl:st.highlight||'#FFE14D',hlon:!!st.highlight,out:st.outline,pos:st.pos,words:st.words,upper:st.upper,pop:st.pop};
  $('#rsnow').textContent=(PRESETS[cs.preset]||{}).label||cs.preset;
  $('#rsslot').appendChild($('#capbody')); csToUI(); $('#rsdlg').showModal();
}
$('#rsdlg').addEventListener('close',()=>{       // put the panel back, restore your normal style
  $('#capdet').appendChild($('#capbody')); restyleTarget=null;
  if(mainCs){cs=mainCs;mainCs=null} csToUI();
});
$('#rsx').onclick=()=>$('#rsdlg').close();
$('#rsapply').onclick=async()=>{
  const path=restyleTarget;
  const r=await api('/api/restyle',{path,style:cs.auto?'auto':cs.preset,match:cs.match,words:cs.words,cap:capPayload()});
  if(r.error){alert(r.error);return}
  $('#rsdlg').close(); await poll();
  const el=clipEl(path); if(el) el.scrollIntoView({behavior:'smooth',block:'center'});
};
$('#cs_reset').onclick=()=>{const m=cs.match;csFromPreset(cs.preset);cs.match=m;csToUI();csSave()};
['cs_match','cs_font','cs_text','cs_hl','cs_hlon','cs_out','cs_size','cs_words','cs_upper','cs_pop'].forEach(id=>{
  $('#'+id).addEventListener('input',csFromUI); $('#'+id).addEventListener('change',csFromUI)});
$('#cs_pos').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
  cs.pos=b.dataset.v;cs.auto=false;csToUI();csSave()});
$('#caps').addEventListener('change',e=>$('#capdet').style.display=e.target.checked?'':'none');
csInit();
let camps={};
async function loadCamps(){
  const r=await api('/api/campaigns'); camps={};
  const byAg={}; r.forEach(c=>{camps[c.id]=c;(byAg[c.agency]=byAg[c.agency]||[]).push(c)});
  $('#camp').innerHTML='<option value="">No campaign (free clipping)</option>'+
    Object.entries(byAg).map(([ag,cs])=>`<optgroup label="${esc(ag)}">`+
      cs.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')+'</optgroup>').join('');
}
function segSet(name,v){const seg=document.querySelector(`.seg[data-name=${name}]`); if(!seg)return false;
  const b=seg.querySelector(`button[data-v="${v}"]`); if(b){b.click();return true}
  seg.querySelectorAll('button').forEach(x=>x.classList.remove('on')); opt[name]=v; return false}
function bestFit(c,quiet){
  const b=c&&c.best; if(!b)return;
  if(b.len){ if(!segSet('len',b.len)){        // brief's own length: light up the nearest preset
      const [lo,hi]=b.len.split('-').map(Number), mid=(lo+hi)/2; let best=null,bd=1e9;
      document.querySelectorAll('.seg[data-name=len] button').forEach(x=>{const [a,z]=x.dataset.v.split('-').map(Number);
        const d=Math.abs((a+z)/2-mid); if(d<bd){bd=d;best=x}});
      if(best)best.classList.add('on'); opt.len=b.len}}
  segSet('mode',b.mode); setTimeout(trackShow,0);
  $('#track').checked=b.track; $('#caps').checked=b.captions; $('#caps').dispatchEvent(new Event('change'));
  document.querySelectorAll('.smart').forEach(x=>{if(x.dataset.k in b.smart)x.checked=b.smart[x.dataset.k]});
  try{localStorage.setItem('clipper_smart',JSON.stringify(smartOpts()))}catch(e){}; smartSum();
  if(b.cap_auto&&!cs.auto){cs.auto=true;try{csChrome();csSave()}catch(e){}}
  if(!b.music){$('#music').value='';$('#volbox').style.display='none'}
  $('#campbestmsg').textContent='🎯 Set up for this brief: '+b.why.join(' · ');
}
$('#campbest').onclick=()=>bestFit(camps[$('#camp').value]);
$('#camp').onchange=e=>{
  const c=camps[e.target.value];
  $('#campedit').style.display=e.target.value?'':'none';
  $('#campbest').style.display=e.target.value?'':'none'; $('#campbestmsg').textContent='';
  if(c)bestFit(c,true);
  campCheck();
  const noMusic=c&&c.music===false;
  $('#music').disabled=noMusic; if(noMusic){$('#music').value='';$('#volbox').style.display='none'}
  $('#campnote').textContent=c?('Rules applied automatically'+(noMusic?' · music not allowed':'')+
    (c.on_screen?' · on-screen: "'+c.on_screen+'"':'')):'';
  loadSources();
};
const base=p=>p.split('/').pop();
async function loadSources(){
  const cid=$('#camp').value;
  if(!cid){$('#srcbox').style.display='none';return}
  const r=await api('/api/sources?campaign='+encodeURIComponent(cid));
  let h='<option value="">— pick a video, or paste a path/link above —</option>';
  const len=p=>r.dur&&r.dur[p]?' · '+fmtDur(r.dur[p]):'';
  if(r.source.length) h+='<optgroup label="Already in this campaign">'+
    r.source.map(p=>`<option value="${esc(p)}">${esc(base(p))}${len(p)}</option>`).join('')+'</optgroup>';
  if(r.downloads.length) h+='<optgroup label="New in Downloads (will be moved in)">'+
    r.downloads.map(p=>`<option value="${esc(p)}">📥 ${esc(base(p))}${len(p)}</option>`).join('')+'</optgroup>';
  $('#src').innerHTML=h; $('#srcbox').style.display='';
  const tot=r.source.reduce((a,p)=>a+((r.dur||{})[p]||0),0);
  const dlTot=r.downloads.reduce((a,p)=>a+((r.dur||{})[p]||0),0);
  $('#bulkbox').style.display=(r.source.length||r.downloads.length)?'':'none';
  $('#mvdlbox').style.display=r.downloads.length?'':'none';
  $('#mvdltxt').innerHTML=`Move the <b>${r.downloads.length}</b> 📥 video${r.downloads.length>1?'s':''} from Downloads (${fmtDur(dlTot)}) into this campaign first`;
  $('#bulkinfo').innerHTML=`${r.source.length} video${r.source.length!==1?'s':''} · ${fmtDur(tot)} already in ${esc(r.folder)}.<br>`+
    (r.unscanned?`${r.unscanned} not scanned yet - the first scan of ~${fmtDur(r.unscanned_dur)} of footage takes a while. Re-scrapes are quick.`
                :`All scanned - a re-scrape only takes a moment.`);
  const b=$('#batchgo');
  if(r.pending){b.style.display='';b.textContent=`or clip each new video separately (${r.pending})`;}
  else b.style.display='none';
  $('#batchnote').textContent=r.source.length?'':`Put the campaign's videos in ${r.folder} to scrape them all at once.`;
}
function fmtDur(s){s=Math.round(s||0);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
  return h?`${h}h ${String(m).padStart(2,'0')}m`:`${m}:${String(x).padStart(2,'0')}`}
$('#bulkk').oninput=e=>$('#bulkkv').textContent=+e.target.value?e.target.value:'Auto';
$('#bulkgo').onclick=async()=>{
  const b=$('#bulkgo'); b.disabled=true;
  const r=await api('/api/jobs',{bulk:true,move_dl:$('#mvdlbox').style.display!=='none'&&$('#mvdl').checked,url:'',k:+$('#bulkk').value,len:opt.len,mode:opt.mode,model:opt.model,
    captions:$('#caps').checked,track:$('#track').checked,cut_pauses:$('#cutp').checked,smart:smartOpts(),campaign:$('#camp').value,style:cs.preset,match:cs.match,words:cs.words,
    cap:{font:cs.font,size:cs.size,text:cs.text,highlight:cs.hlon?cs.hl:'',outline:cs.out,pos:cs.pos,upper:cs.upper,pop:cs.pop},
    music:$('#music').value,music_vol:+$('#mvol').value/100,ollama:$('#ollama').value.trim()});
  b.disabled=false;
  if(r.error){alert(r.error);return}
  poll(); window.scrollTo({top:document.querySelector('#jobs').offsetTop-20,behavior:'smooth'});
};
$('#batchgo').onclick=async()=>{
  const b=$('#batchgo'); b.disabled=true;
  const r=await api('/api/jobs',{batch:true,url:'',k:0,len:opt.len,mode:opt.mode,model:opt.model,
    captions:$('#caps').checked,track:$('#track').checked,cut_pauses:$('#cutp').checked,smart:smartOpts(),campaign:$('#camp').value,style:cs.preset,match:cs.match,words:cs.words,
    cap:{font:cs.font,size:cs.size,text:cs.text,highlight:cs.hlon?cs.hl:'',outline:cs.out,pos:cs.pos,upper:cs.upper,pop:cs.pop},
    music:$('#music').value,music_vol:+$('#mvol').value/100,ollama:$('#ollama').value.trim()});
  b.disabled=false;
  if(r.error){alert(r.error);return}
  poll(); setTimeout(loadSources,3000);
};
$('#src').onchange=e=>{ if(e.target.value) $('#url').value=e.target.value; };
const campsReady=loadCamps();
async function campCheck(){
  const id=$('#camp').value, el=$('#campchk'); if(!id){el.innerHTML='';return}
  const r=await api('/api/campaigncheck?id='+encodeURIComponent(id));
  const probs=r.filter(x=>x[0]!=='info');
  el.innerHTML=(probs.length?'':'<div class="ok">✅ Campaign settings match the brief</div>')+
    r.map(([l,m])=>`<div class="${l}">${l==='info'?'ℹ️':'⚠'} ${esc(m)}</div>`).join('');
}
/* ---------- update ---------- */
async function checkVersion(fetch){
  const v=await api('/api/version'+(fetch?'?fetch=1':'')); const b=$('#updbtn');
  b.title=`Version ${v.version}`+(v.behind?` - ${v.behind} update(s) available`:'');
  b.classList.toggle('has',v.behind>0); b.textContent=v.behind?`⟳ Update (${v.behind} new)`:'⟳ Update';
}
$('#updbtn').onclick=async()=>{
  const b=$('#updbtn'); b.disabled=true; b.textContent='Updating…';
  const r=await api('/api/update',{});
  if(!r.ok){alert(r.msg);b.disabled=false;checkVersion();return}
  b.textContent='Restarting…';
  for(let i=0;i<30;i++){await new Promise(x=>setTimeout(x,1000));try{await api('/api/version');location.reload();return}catch(e){}}
  alert('Clipper didn\'t come back - restart it from the terminal');
};
setTimeout(()=>checkVersion(true),1500); setInterval(()=>checkVersion(true),30*60*1000);

/* ---------- AI settings ---------- */
let aiProv='';
function aiShow(){document.querySelectorAll('#aiprov button').forEach(b=>b.classList.toggle('on',b.dataset.v===aiProv));
  $('#ai_ollama').style.display=aiProv==='ollama'?'':'none'; $('#ai_claude').style.display=aiProv==='claude'?'':'none'}
$('#aiprov').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;aiProv=b.dataset.v;aiShow()});
$('#aibtn').onclick=async()=>{const s=await api('/api/settings');aiProv=s.ai||'';$('#ai_om').value=s.ollama_model||'';
  $('#ai_cm').value=s.claude_model||'claude-haiku-5-5';$('#ai_key').value='';$('#ai_haskey').textContent=s.has_key?'✓ key saved':'';
  $('#aimsg').textContent='';aiShow();$('#aidlg').showModal(); gpuCheck()};
async function gpuCheck(){$('#gpumsg').textContent='Checking your graphics card…';const g=await api('/api/gpu');
  $('#gpumsg').textContent=g.msg; $('#gpusetup').style.display=g.driver&&!g.libs?'':'none'}
$('#gpusetup').onclick=async()=>{await api('/api/gpusetup',{});$('#gpumsg').textContent='Installing - watch Jobs below. Press ⚙️ AI again afterwards to re-check.';
  $('#gpusetup').style.display='none';poll()};
$('#aiclose').onclick=()=>$('#aidlg').close();
async function aiSave(){return api('/api/settings',{ai:aiProv,ollama_model:$('#ai_om').value.trim(),claude_model:$('#ai_cm').value,claude_key:$('#ai_key').value.trim()})}
$('#aisave').onclick=async()=>{const r=await aiSave();$('#aimsg').textContent=r.ai?'Using '+r.ai:'AI off';setTimeout(()=>$('#aidlg').close(),700)};
$('#aitest').onclick=async()=>{await aiSave();$('#aimsg').textContent='Testing…';const r=await api('/api/aitest',{});
  $('#aimsg').textContent=r.msg;$('#aimsg').style.color=r.ok?'var(--ok)':'var(--acc)'};

/* ---------- campaign builder ---------- */
let cdEditId='';
const LISTF=['keywords','hashtags'], LINEF=['required_lines','example_captions','checklist','manual_checks'],
      TXTF=['name','agency','source_note','min_len','len','mention','disclosure','on_screen','caption_rules','rate','logo'];
const STOP=new Set('the and for with from this that clip clips clipping campaign official new video videos interview launch your'.split(' '));
async function cdFiles(){
  const box=$('#cf_files');
  box.innerHTML='<div class="s" style="color:var(--dim);font-size:13px;padding:6px">🔎 Looking in Downloads…</div>';
  let list;
  try{list=await api('/api/downloads')}catch(e){
    box.innerHTML='<div class="s" style="color:var(--acc);font-size:13px;padding:6px">Couldn\'t read Downloads - restart clipper and try again.</div>';return}
  const words=[$('#cf_name').value,$('#cf_keywords').value,$('#cf_agency').value].join(' ').toLowerCase()
     .split(/[^a-z0-9]+/).filter(w=>w.length>=3&&!STOP.has(w));
  if(!list.length){box.innerHTML='<div class="s" style="color:var(--dim);font-size:13px;padding:6px">No videos or zips downloaded in the last 30 days.</div>';return}
  const now=Date.now()/1000;
  const ago=t=>{const m=Math.round((now-t)/60);return m<1?'just now':m<60?m+' min ago':m<1440?Math.round(m/60)+' h ago':Math.round(m/1440)+' d ago'};
  box.innerHTML=list.map(f=>{const n=f.name.toLowerCase(), hit=words.filter(w=>n.includes(w));
    const fresh=now-f.mtime<600;                       // downloaded in the last 10 minutes
    const mb=(f.size/1048576).toFixed(0)+' MB', d=f.zip?'zip':fmtDur(f.dur);
    const tag=fresh?'<span class="match">just downloaded</span>':hit.length?`<span class="match">matches "${esc(hit[0])}"</span>`:'';
    return `<label><input type="checkbox" value="${esc(f.path)}" ${fresh||hit.length?'checked':''}>
      <span>${f.zip?'🗜️':'🎬'} ${esc(f.name)} ${tag}</span>
      <span class="m">${d} · ${mb} · ${ago(f.mtime)}</span></label>`}).join('');
}
let cdLogo=null;
function cdFill(c){
  TXTF.forEach(k=>$('#cf_'+k).value=c[k]??'');
  LISTF.forEach(k=>$('#cf_'+k).value=(c[k]||[]).join(', '));
  LINEF.forEach(k=>$('#cf_'+k).value=(c[k]||[]).join('\n'));
  $('#cf_music').checked=!!c.music; $('#cf_exact').checked=!!c.exact_captions;
  document.querySelectorAll('.cf_tag').forEach(i=>i.value=(c.mentions||{})[i.dataset.p]||'');
  cdLogo=null; $('#cf_logofile').value=''; $('#cf_logomsg').textContent='';
  document.querySelectorAll('#cf_platforms input').forEach(i=>i.checked=(c.platforms||[]).includes(i.value));
  $('#cdstep1').style.display='none'; $('#cdstep2').style.display='';
  cdFiles();
}
function cdRead(){
  const c={music:$('#cf_music').checked,exact_captions:$('#cf_exact').checked,captions:true,mode:'blur',
    platforms:[...document.querySelectorAll('#cf_platforms input:checked')].map(i=>i.value)};
  TXTF.forEach(k=>c[k]=$('#cf_'+k).value.trim());
  c.mentions={}; document.querySelectorAll('.cf_tag').forEach(i=>{if(i.value.trim())c.mentions[i.dataset.p]=i.value.trim()});
  LISTF.forEach(k=>c[k]=$('#cf_'+k).value.split(',').map(x=>x.trim()).filter(Boolean));
  LINEF.forEach(k=>c[k]=$('#cf_'+k).value.split('\n').map(x=>x.trim()).filter(Boolean));
  return c;
}
$('#campnew').onclick=()=>{cdEditId='';$('#cdtitle').textContent='New campaign';$('#cdbrief').value='';
  $('#cdstep1').style.display='';$('#cdstep2').style.display='none';$('#cdlg').showModal()};
$('#campedit').onclick=async()=>{const id=$('#camp').value;if(!id)return;
  const c=await api('/api/campaign?id='+encodeURIComponent(id)); if(c.error){alert(c.error);return}
  cdEditId=id;$('#cdtitle').textContent='Edit campaign';$('#cdvia').textContent='';cdFill(c);$('#cdlg').showModal()};
$('#cdclose').onclick=()=>$('#cdlg').close();
$('#cdback').onclick=()=>{$('#cdstep1').style.display='';$('#cdstep2').style.display='none'};
$('#cdread').onclick=async()=>{
  const b=$('#cdread');b.disabled=true;b.textContent='Reading…';
  const r=await api('/api/brief',{text:$('#cdbrief').value,ollama:$('#ollama').value.trim()});
  b.disabled=false;b.textContent='Read the rules';
  if(r.error){alert(r.error);return}
  $('#cdvia').textContent=r._via&&r._via!=='patterns'?`✨ Read by ${r._via} - check it over, then publish.`
    :'Filled in automatically - check every box (especially name, agency, tag and on-screen text), fix anything, then save.';
  cdFill(r);
};
async function cdPublish(andClip){
  const files=[...document.querySelectorAll('#cf_files input:checked')].map(i=>i.value);
  const b=andClip?$('#cdgo'):$('#cdsave'), old=b.textContent; b.disabled=true;
  b.textContent=files.length?`Moving ${files.length} file(s)…`:'Saving…';
  const r=await api('/api/savecampaign',{campaign:cdRead(),id:cdEditId,brief:cdEditId?'':$('#cdbrief').value,files,logo:cdLogo});
  b.disabled=false; b.textContent=old;
  if(r.error){alert(r.error);return}
  $('#cdlg').close(); await loadCamps(); $('#camp').value=r.id; $('#camp').onchange({target:$('#camp')});
  loadClips();
  if(andClip){                                   // straight into the bulk scrape
    const j=await api('/api/jobs',{bulk:true,url:'',k:0,len:opt.len,mode:opt.mode,model:opt.model,captions:true,
      campaign:r.id,style:'auto',match:false,words:3,cap:{},music:'',music_vol:.15,ollama:''});
    if(j.error){alert(j.error);return}
    try{if('Notification' in window&&Notification.permission==='default')Notification.requestPermission()}catch(e){}
    poll(); window.scrollTo({top:document.querySelector('#jobs').offsetTop-20,behavior:'smooth'});
  }
}
$('#cf_logofile').onchange=e=>{const f=e.target.files[0]; cdLogo=null; $('#cf_logomsg').textContent='';
  if(!f)return; if(f.size>8e6){alert('That image is too big (max 8 MB)');e.target.value='';return}
  const rd=new FileReader(); rd.onload=()=>{cdLogo={name:f.name,data:rd.result}; $('#cf_logo').value='logo.png';
    $('#cf_logomsg').textContent='✓ '+f.name+' will be saved as logo.png'}; rd.readAsDataURL(f)};
$('#cdsave').onclick=()=>cdPublish(false);
$('#cdgo').onclick=()=>cdPublish(true);
function copyText(t,btn){
  const ta=document.createElement('textarea'); ta.value=t; document.body.appendChild(ta);
  ta.select(); let ok=false; try{ok=document.execCommand('copy')}catch(e){}
  ta.remove();
  if(!ok&&navigator.clipboard) navigator.clipboard.writeText(t);
  const o=btn.textContent; btn.textContent='Copied ✓'; setTimeout(()=>btn.textContent=o,1500);
}
const capStore={};

$('#f').onsubmit=async e=>{
  e.preventDefault();
  $('#go').disabled=true;
  const r=await api('/api/jobs',{url:$('#url').value.trim(),k:+$('#k').value,len:opt.len,mode:opt.mode,
    model:opt.model,captions:$('#caps').checked,track:$('#track').checked,cut_pauses:$('#cutp').checked,smart:smartOpts(),campaign:$('#camp').value,style:cs.auto?'auto':cs.preset,match:cs.match,words:cs.words,cap:capPayload(),music:$('#music').value,music_vol:+$('#mvol').value/100,ollama:$('#ollama').value.trim()});
  $('#go').disabled=false;
  if(r.error){alert(r.error);return}
  $('#url').value='';
  poll(); setTimeout(loadSources,3000);
};

let finished=new Set(), first=true;
async function poll(){
  const jobs=await api('/api/jobs');
  const el=$('#jobs');
  if(!jobs.length){el.innerHTML='<div class="empty">Nothing running</div>';}
  else el.innerHTML=jobs.map(j=>`
    <div class="card job ${j.status}">
      <div class="top"><div style="min-width:0">
        <div class="t">${esc(j.title||j.url)}</div>
        <div class="s">${esc(j.status==='error'?j.error:j.stage)}</div></div>
        ${j.status==='running'||j.status==='queued'
          ?`<button class="small" onclick="cancelJob('${j.id}')">Cancel</button>`
          :`<button class="small" onclick="clearJob('${j.id}')">✕</button>`}
      </div>
      <div class="bar"><i style="width:${j.progress}%"></i></div>
      <details ${openLogs.has(j.id)?'open':''} ontoggle="logToggle('${j.id}',this.open)">
        <summary>Log</summary><pre>${esc(j.log.join('\n'))}</pre></details>
    </div>`).join('');
  busy={}; for(const j of jobs) if(j.clip&&(j.status==='running'||j.status==='queued')) busy[j.clip]=j;
  let refresh=false; const fresh=[];
  let ready=null;
  for(const j of jobs) if(j.status==='done'&&!finished.has(j.id)){finished.add(j.id);
    if(!first){refresh=true;if(j.clip)fresh.push(j.clip);if(j.kind!=='restyle'&&j.made)ready=j}}
  first=false;
  if(refresh) await loadClips(); else markBusy();
  if(ready){
    $('#readytxt').textContent=`✅ ${ready.made} clip${ready.made>1?'s':''} ready to review`+(ready.campaign?` · ${ready.campaign.split('/').pop().replace(/-/g,' ')}`:'');
    $('#readybar').style.display=''; $('#readygo').onclick=()=>{$('#readybar').style.display='none';review(0)};
    try{if(document.hidden&&'Notification' in window&&Notification.permission==='granted')new Notification('Clipper',{body:$('#readytxt').textContent})}catch(e){}
  }
  fresh.forEach(p=>{const el=clipEl(p);if(!el)return;el.classList.add('fresh');
    el.scrollIntoView({behavior:'smooth',block:'center'});setTimeout(()=>el.classList.remove('fresh'),8000)});
}
let busy={};
const clipEl=p=>[...document.querySelectorAll('.clip')].find(e=>e.dataset.path===p);
function markBusy(){document.querySelectorAll('.clip').forEach(el=>{const j=busy[el.dataset.path];
  el.classList.toggle('busy',!!j); el.querySelector('.ov').textContent=j?(j.status==='queued'?'🎨 Waiting to restyle…':'🎨 '+j.stage):''})}
const openLogs=new Set();
function logToggle(id,o){o?openLogs.add(id):openLogs.delete(id)}
async function cancelJob(id){await api('/api/cancel',{id});poll()}
async function clearJob(id){await api('/api/clear',{id});poll()}

async function loadClips(){
  const groups=await api('/api/clips');
  const el=$('#clips');
  if(!groups.length){el.innerHTML='<div class="empty">No clips yet — paste a link above</div>';return}
  groups.forEach(g=>g.clips.forEach(c=>{if(c.caption)capStore[c.path]=c.caption}));
  lastGroups=groups;
  await campsReady; if(!plSet.plan_windows) plSet=await api('/api/settings');
  dueAt={}; groups.forEach(g=>{const pl=computePlan(g); if(pl.rows) pl.rows.forEach(r=>dueAt[r.c.path+'|'+r.p]=r.t)});
  el.innerHTML=groups.map((g,gi)=>`
    <div class="group"><div class="gname"><span>${esc(g.name)}</span>
      <span style="display:flex;gap:8px;align-items:center">${(()=>{const n=g.clips.filter(c=>(c.checks||[]).length).length;return n?`<span class="gwarn">⚠ ${n} need a look</span>`:''})()}
      <span class="s" style="color:var(--dim);font-size:12px">${g.clips.length} clip${g.clips.length==1?"":"s"}</span>
      <button class="gbtn" onclick="review(${gi})">🎬 Review</button>
      <button class="gbtn" onclick="planGroup(${gi})">🗓 Plan</button></span></div>
    <div class="grid">${g.clips.map(c=>`
      <div class="clip" data-path="${esc(c.path)}">
        <div class="vw"><video controls playsinline preload="none" poster="/thumb/${enc(c.path)}?v=${c.v}" src="/clips/${enc(c.path)}?v=${c.v}"></video>
          <div class="ov"></div><div class="badge">✓ Restyled</div></div>
        <div class="stars" title="${esc(c.why||'')}">${starStr(c.stars)} <b>${(c.stars||0).toFixed(1)}</b></div>
        <div class="why">${esc(c.why||'')}</div>
        <div class="n" title="${esc(c.name)}">${esc(c.name)}</div>
        ${platRow(c)}
        ${(c.checks||[]).length?`<div class="warns">${c.checks.map(w=>`<div class="warn">⚠ ${esc(w)}</div>`).join('')}</div>`:''}
        ${capButtons(c)}
        <div class="a"><a href="/clips/${enc(c.path)}?dl=1" download title="Download to this device (phone: it lands in Downloads / your gallery)">📲 Save</a>
          ${CAN_SHARE?`<button title="Send straight to TikTok, Instagram, your gallery…" onclick="shareClip(this,'${esc(c.path).replace(/'/g,"\\'")}')">↗</button>`:''}
          ${c.styled?`<button title="Make 3 different versions: shorter, longer, other look + hook" onclick="variations('${esc(c.path).replace(/'/g,"\\'")}')">🎲</button>`:''}
          ${c.styled?`<button title="Move the start / end" onclick="trimDlg('${esc(c.path).replace(/'/g,"\\'")}','${esc(c.name).replace(/'/g,"\\'")}')">✂️</button>`:''}
          ${c.styled?`<button class="rs" title="Change the caption style" onclick="restyle('${esc(c.path).replace(/'/g,"\\'")}','${esc(c.name).replace(/'/g,"\\'")}')">🎨</button>`:''}
          <button title="Log where you posted it" onclick="logPost('${esc(c.path).replace(/'/g,"\\'")}','${esc(c.name).replace(/'/g,"\\'")}')">📊</button>
          <button onclick="del('${esc(c.path).replace(/'/g,"\\'")}')">Delete</button></div>
      </div>`).join('')}</div></div>`).join('');
  markBusy(); tickTimers();
}
function starStr(n){n=n||0;let s='';for(let i=1;i<=5;i++)s+=n>=i?'★':n>=i-.5?'⯪':'☆';return s}
/* ---------- review: watch, keep or drop, delete the rest ---------- */
let lastGroups=[], rvList=[], rvI=0, rvMark={}, rvName='', rvOk={};
function review(gi){
  const g=lastGroups[gi]; if(!g)return;
  rvList=g.clips; rvI=0; rvMark={}; rvOk={}; rvName=g.name; $('#rvgroup').textContent=g.name; rvShow(); $('#rv').showModal();
}
function rvShow(){
  const c=rvList[rvI]; if(!c)return;
  const v=$('#rvvideo'); v.src=`/clips/${enc(c.path)}?v=${c.v}`; v.play().catch(()=>{});
  $('#rvcap').innerHTML=`<div style="color:#ffcf3d">${starStr(c.stars)} <b style="color:var(--text)">${(c.stars||0).toFixed(1)}</b> · ${esc(c.why||'')}</div>`+esc(c.caption||c.name)+((c.checks||[]).length?'<div style="color:#ffb03d;margin-top:4px">'+c.checks.map(w=>'⚠ '+esc(w)).join('<br>')+'</div>':'');
  const kept=Object.values(rvMark).filter(x=>x==='keep').length, dropped=Object.values(rvMark).filter(x=>x==='drop').length;
  $('#rvpos').textContent=`Clip ${rvI+1} of ${rvList.length} · ⭐ ${kept} kept · ✕ ${dropped} dropped`;
  const man=c.manual||[]; $('#rvman').style.display=man.length?'':'none';
  $('#rvmanlist').innerHTML=man.map(x=>'<br>• '+esc(x)).join(''); $('#rvmanok').checked=!!rvOk[c.path];
  const st=$('#rvstamp'), m=rvMark[c.path]; st.className='rvstamp'+(m?' '+m:''); st.textContent=m==='keep'?'⭐ Keeping':m==='drop'?'✕ Dropping':'';
  $('#rvstrip').innerHTML=rvList.map((x,i)=>`<img src="/thumb/${enc(x.path)}?v=${x.v}" class="${i===rvI?'cur ':''}${rvMark[x.path]||''}" onclick="rvI=${i};rvShow()">`).join('');
  const unmarked=rvList.filter(x=>!rvMark[x.path]).length;
  $('#rvdone').textContent=kept?`Done — keep ${kept}, delete ${rvList.length-kept}`+(unmarked?` (incl. ${unmarked} not marked)`:'')
    :dropped?`Done — delete ${dropped}`+(unmarked?`, leave ${unmarked} not marked`:''):'Done';
}
function rvSet(m){const c=rvList[rvI];
  if(m==='keep'&&(c.manual||[]).length&&!rvOk[c.path]){const b=$('#rvman');b.classList.remove('shake');void b.offsetWidth;b.classList.add('shake');return}
  rvMark[c.path]=m; if(rvI<rvList.length-1)rvI++; rvShow()}
$('#rvmanok').onchange=e=>{rvOk[rvList[rvI].path]=e.target.checked};
$('#rvkeep').onclick=()=>rvSet('keep'); $('#rvdrop').onclick=()=>rvSet('drop');
$('#rvprev').onclick=()=>{if(rvI>0){rvI--;rvShow()}}; $('#rvnext').onclick=()=>{if(rvI<rvList.length-1){rvI++;rvShow()}};
$('#rvclose').onclick=()=>{$('#rvvideo').pause();$('#rv').close()};
$('#rv').addEventListener('close',()=>$('#rvvideo').pause());
document.addEventListener('keydown',e=>{ if(!$('#rv').open)return;
  if(e.key==='k'||e.key==='K')rvSet('keep'); else if(e.key==='d'||e.key==='D')rvSet('drop');
  else if(e.key==='ArrowRight')$('#rvnext').click(); else if(e.key==='ArrowLeft')$('#rvprev').click();});
$('#rvdone').onclick=async()=>{
  const keep=rvList.filter(x=>rvMark[x.path]==='keep');
  // with something kept, everything else goes; with nothing kept, only the ones marked Drop go
  const gone=keep.length?rvList.filter(x=>rvMark[x.path]!=='keep'):rvList.filter(x=>rvMark[x.path]==='drop');
  if(!gone.length){$('#rv').close();return}
  const msg=keep.length?`Keep ${keep.length} clip(s) and permanently delete the other ${gone.length}?`
    :gone.length===rvList.length?`Delete all ${gone.length} clips in this batch?`:`Delete the ${gone.length} clip(s) marked Drop?`;
  if(!confirm(msg))return;
  $('#rvvideo').pause(); $('#rvvideo').removeAttribute('src');
  await api('/api/deletemany',{paths:gone.map(x=>x.path)}); $('#rv').close(); loadClips();
};

/* ---------- tabs + history ---------- */
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x===b));
  ['jobs','history','results','projects'].forEach(t=>$('#'+t).style.display=b.dataset.t===t?'':'none');
  if(b.dataset.t==='history')loadHistory(); if(b.dataset.t==='results')loadResults();
  if(b.dataset.t==='projects')loadProjects();
});
async function loadHistory(){
  const h=await api('/api/history'), el=$('#history');
  if(!h.length){el.innerHTML='<div class="empty">Nothing in the history yet</div>';return}
  const when=t=>new Date(t*1000).toLocaleString([], {weekday:'short',hour:'2-digit',minute:'2-digit',day:'numeric',month:'short'});
  el.innerHTML=h.map(x=>`<div class="hist">
    <div class="top"><b style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.title||x.kind)}</b>
      <span class="st ${x.status}">${x.status==='done'?'✓ Done':x.status==='error'?'✕ Failed':'Cancelled'}</span></div>
    <div class="meta">${esc(x.kind)}${x.campaign?' · '+esc(x.campaign):''} · ${when(x.started)} · took ${fmtDur(x.secs)}${x.made?` · ${x.made} clip${x.made>1?'s':''}`:''}</div>
    ${x.error?`<div class="meta" style="color:var(--acc)">${esc(x.error)}</div>`:''}
    <details><summary>Log</summary><pre>${esc((x.log||[]).join('\n'))}</pre></details></div>`).join('')+
    '<button type="button" class="small" onclick="clearHistory()">Clear history</button>';
}
async function clearHistory(){if(!confirm('Clear the whole history?'))return;await api('/api/history/clear',{});loadHistory()}

/* ---------- results ---------- */
let pdClip='';
const fmtN=n=>n==null?'–':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);
async function logPost(path,name){
  pdClip=path; $('#pdtitle').textContent='Log a post · '+name; $('#pd_url').value='';$('#pd_views').value='';$('#pd_likes').value='';
  const r=await api('/api/results'), mine=r.posts.filter(p=>p.clip===path);
  $('#pdlist').innerHTML=mine.length?'<div class="s" style="font-size:12px;color:var(--dim)">Already logged:</div>'+
    mine.map(p=>`<div style="font-size:13px">${esc(p.platform)} · ${fmtN(p.views)} views ${p.url?`· <a href="${esc(p.url)}" target="_blank">link</a>`:''}</div>`).join('')
    +(mine.length>=4?`<div style="color:#ffb03d;font-size:12px">⚠ ${mine.length} posts - the limit is usually 5</div>`:''):'';
  $('#pdlg').showModal();
}
$('#pdadd').onclick=async()=>{
  const r=await api('/api/results/add',{clip:pdClip,platform:$('#pd_plat').value,url:$('#pd_url').value,views:$('#pd_views').value,likes:$('#pd_likes').value});
  if(r.error){alert(r.error);return} $('#pdlg').close(); loadClips(); if($('#results').style.display!=='none')loadResults();
};
async function loadResults(){
  const r=await api('/api/results'), el=$('#results'), t=r.total;
  if(!t.posts){el.innerHTML='<div class="empty">No posts logged yet.<br>After posting a clip, press 📊 on it to log the link - then update the views here every few days.</div>';return}
  el.innerHTML=`<div class="row" style="margin-bottom:10px;align-items:center;gap:10px">
      <button type="button" class="small" id="rrefresh" onclick="refreshViews()">⟳ Refresh views</button>
      <span id="rrmsg" style="font-size:12px;color:var(--dim)">${rrMsg||'Reads views and likes from your post links'}</span></div>
    <div class="rstats"><div class="rstat"><b>${t.posts}</b><span>posts</span></div>
    <div class="rstat"><b>${fmtN(t.views)}</b><span>views</span></div>
    <div class="rstat"><b>$${t.earn.toFixed(2)}</b><span>rough earnings*</span></div></div>
    ${t.stale?`<div style="color:#ffb03d;font-size:13px;margin-bottom:8px">⚠ ${t.stale} post${t.stale>1?'s haven\'t':' hasn\'t'} had views updated for 3+ days</div>`:''}
    <div class="rsec">What's working</div>
    ${r.learning?r.insights.map(i=>`<div style="font-size:13px">${i.vs>=1?'📈':'📉'} ${esc(i.what)}: ${fmtN(i.avg)} views on average (${i.n} posts, ${i.vs}× your average)</div>`).join('')
      :`<div style="font-size:13px;color:var(--dim)">Log views on ${r.need} more post${r.need===1?'':'s'} and clipper starts learning what gets views.</div>`}
    <div class="rsec">Campaigns</div>
    <table class="rtable"><tr><th>Campaign</th><th>Posts</th><th>Views</th><th>Est.</th></tr>
    ${r.campaigns.map(c=>`<tr><td>${esc(c.name)}</td><td>${c.posts}</td><td>${fmtN(c.views)}</td><td>${c.rate?'$'+c.earn.toFixed(2):'<span style="color:var(--dim)">set pay rate</span>'}</td></tr>`).join('')}</table>
    <div class="rsec">Posts</div>
    <table class="rtable"><tr><th>Clip</th><th>Where</th><th>Views</th><th>Likes</th><th></th></tr>
    ${r.posts.map(p=>{const stale=Date.now()/1000-p.updated>3*86400;
      return `<tr class="${stale?'stale':''}"><td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(p.clip_name)}">${p.url?`<a href="${esc(p.url)}" target="_blank">${esc(p.clip_name)}</a>`:esc(p.clip_name)}</td>
      <td>${esc(p.platform.split(' ')[0])}</td>
      <td><input value="${p.views??''}" data-id="${p.id}" data-k="views" onchange="updPost(this)"></td>
      <td><input value="${p.likes??''}" data-id="${p.id}" data-k="likes" onchange="updPost(this)" style="width:60px"></td>
      <td><button class="small" onclick="delPost('${p.id}')">✕</button></td></tr>`}).join('')}</table>
    <div style="font-size:11px;color:var(--dim);margin-top:8px">* views ÷ 1,000 × the campaign's pay rate. Ignores minimum payouts, caps and fees, so it's a rough guide.</div>`;
}
/* ---------- projects folder ---------- */
const fmtSize=b=>b>=1e9?(b/1e9).toFixed(1)+' GB':b>=1e6?Math.round(b/1e6)+' MB':b>=1e3?Math.round(b/1e3)+' KB':(b||0)+' B';
let projPaths=[];
async function loadProjects(){
  const r=await api('/api/projects'), el=$('#projects');
  projPaths=[r.root, r.free, ...r.campaigns.map(c=>c.path)];
  const tot=r.campaigns.reduce((a,c)=>a+c.source_size+c.clips_size,0)+r.free_size;
  el.innerHTML=`<div class="projroot"><div class="s" style="font-size:12px;color:var(--dim)">Your projects folder${r.custom?'':' (default)'} · ${fmtSize(tot)} in total</div>
      <code>${esc(r.root)}</code>
      <div class="row" style="gap:8px;margin-top:8px"><button type="button" class="small" onclick="openDir(0)">📂 Open</button>
      <button type="button" class="small" onclick="pickFolder()">Change folder…</button>
      ${r.custom?`<button type="button" class="small" onclick="resetFolder()">Back to default</button>`:''}</div></div>
    <table class="rtable"><tr><th>Campaign</th><th>Footage</th><th>Clips</th><th></th></tr>
    ${r.campaigns.map((c,i)=>`<tr><td><b>${esc(c.name)}</b><div style="font-size:11.5px;color:var(--dim)">${esc(c.agency)}</div></td>
      <td>${c.sources} · ${fmtSize(c.source_size)}</td><td>${c.clips} · ${fmtSize(c.clips_size)}</td>
      <td><button type="button" class="small" onclick="openDir(${i+2})">📂</button></td></tr>`).join('')
      ||'<tr><td colspan="4" style="color:var(--dim)">No campaigns yet - press + New above</td></tr>'}
    <tr><td><b>Free clips</b><div style="font-size:11.5px;color:var(--dim)">pasted links, no campaign</div></td><td>–</td>
      <td>${r.free_clips} · ${fmtSize(r.free_size)}</td><td><button type="button" class="small" onclick="openDir(1)">📂</button></td></tr></table>
    <div style="font-size:11.5px;color:var(--dim);margin-top:8px">📂 opens the folder on the PC clipper runs on.</div>`;
}
async function openDir(i){const r=await api('/api/open',{path:projPaths[i]}); if(!r.ok)alert(r.msg||"Couldn't open it")}
let fpCur='';
async function fpGo(path){
  const r=await api('/api/dirs?path='+encodeURIComponent(path||'')); fpCur=r.path;
  $('#fppath').textContent=r.path; $('#fpup').disabled=!r.parent; $('#fpup').onclick=()=>fpGo(r.parent);
  $('#fphome').onclick=()=>fpGo(r.home);
  $('#fpdrives').innerHTML=r.drives.map((d,i)=>`<button type="button" class="small" data-i="${i}">${esc(d)}</button>`).join('');
  $('#fpdrives').querySelectorAll('button').forEach(b=>b.onclick=()=>fpGo(r.drives[+b.dataset.i]));
  const sep=r.path.includes('\\')?'\\':'/';
  $('#fplist').innerHTML=r.dirs.map((n,i)=>`<div data-i="${i}">📁 ${esc(n)}</div>`).join('')||'<div style="color:var(--dim);cursor:default">(no folders in here)</div>';
  $('#fplist').querySelectorAll('div[data-i]').forEach(x=>x.onclick=()=>fpGo((r.path.endsWith(sep)?r.path:r.path+sep)+r.dirs[+x.dataset.i]));
}
async function pickFolder(){$('#fpmsg').textContent='';$('#fpmove').checked=false;$('#fpnew').value='';await fpGo('');$('#fpdlg').showModal()}
$('#fpmk').onclick=async()=>{const r=await api('/api/mkdir',{path:fpCur,name:$('#fpnew').value});
  if(!r.ok){$('#fpmsg').textContent=r.msg;return} $('#fpnew').value=''; fpGo(r.path)};
async function setFolder(body){
  $('#fpmsg').textContent='Saving…';
  const r=await api('/api/projects/set',body);
  if(!r.ok){$('#fpmsg').textContent=r.msg;alert(r.msg);return}
  $('#fpmsg').textContent='Restarting clipper…'; setTimeout(()=>location.reload(),3000);
}
$('#fpuse').onclick=()=>setFolder({path:fpCur,move:$('#fpmove').checked});
async function resetFolder(){
  if(!confirm('Go back to the default folder (next to clipper)? Your campaigns and clips will be moved back too.'))return;
  setFolder({reset:true,move:true});
}
let rrMsg='';
async function refreshViews(){
  const b=$('#rrefresh'); b.disabled=true; b.textContent='⟳ Checking your posts…';
  $('#rrmsg').textContent='This takes a few seconds per post';
  const r=await api('/api/results/refresh',{});
  const f=r.failed||[], ig=f.filter(p=>p.startsWith('Instagram')).length;
  rrMsg=`✓ Updated ${r.refreshed} post${r.refreshed===1?'':'s'}`+(f.length?` · couldn't read ${f.length}`+
    (ig?` (Instagram usually needs you logged in to Instagram in Firefox - or type those views in)`:''):'');
  await loadResults(); loadClips();
}
async function updPost(inp){await api('/api/results/update',{id:inp.dataset.id,[inp.dataset.k]:inp.value});inp.style.borderColor='var(--ok)'}
async function delPost(id){if(!confirm('Remove this post from your results?'))return;await api('/api/results/delete',{id});loadResults();loadClips()}

/* ---------- posting planner ---------- */
let plGroup=null, plRows=[], plSet={};
const PL_SHORT={'TikTok':'TikTok','Instagram Reels':'Instagram','YouTube Shorts':'YouTube','X':'X'};
async function planGroup(gi){
  plGroup=lastGroups[gi]; if(!plGroup)return;
  plSet=await api('/api/settings');
  $('#pl_win').value=plSet.plan_windows; $('#pl_day').value=plSet.plan_per_day; $('#pl_gap').value=plSet.plan_gap;
  document.querySelectorAll('#pl_mine input').forEach(i=>i.checked=(plSet.my_platforms||[]).includes(i.value));
  $('#pltitle').textContent='Posting plan · '+plGroup.name; buildPlan(); $('#pldlg').showModal();
}
function parseWins(t){return t.split(',').map(x=>x.trim().match(/^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/))
  .filter(Boolean).map(m=>[+m[1]*60+ +m[2], +m[3]*60+ +m[4]])}
const groupCamp=g=>{const key=g.clips[0]?g.clips[0].path.split('/')[0]:'_'; return camps[key.replace(/~/g,'/')]||{}};
function computePlan(g){
  const wins=parseWins(plSet.plan_windows||''), gap=(+plSet.plan_gap||2)*60, perDay=+plSet.plan_per_day||3;
  const camp=groupCamp(g);
  let plats=(camp.platforms&&camp.platforms.length?camp.platforms:['TikTok','Instagram Reels','YouTube Shorts','X'])
    .filter(p=>(plSet.my_platforms||[]).includes(p));
  if(!wins.length||!plats.length) return {camp,plats,wins};
  // what to post: best clips first, each platform it isn't on yet, max 5 posts per clip
  const all=[...g.clips].sort((a,b)=>(b.stars||0)-(a.stars||0));
  const clips=all.filter(c=>(c.stars||0)>=2), low=all.length-clips.length;
  const items=[]; const maxRound=plats.length;
  for(let r=0;r<maxRound;r++) for(const c of clips){
    const done=(c.posted||[]); const todo=plats.filter(p=>!done.includes(p));
    const n=done.length+items.filter(i=>i.c===c).length;
    if(todo[r]&&n<5) items.push({c,p:todo[r]});
  }
  // free slots for the next 7 days (the current half-hour counts as "now")
  const now=new Date(), start=new Date(now.getTime()-29*60000), slots=[];
  for(let d=0;d<7;d++){const day=new Date(now.getFullYear(),now.getMonth(),now.getDate()+d);
    for(const [a,b] of wins) for(let m=a;m<=b;m+=30){const t=new Date(day.getTime()+m*60000); if(t>start)slots.push(t)}}
  slots.sort((x,y)=>x-y);
  // real posts so far: keep the gap after the last one on each platform, count today's
  const last={}, perDayCount={}, rows=[];
  lastGroups.forEach(gg=>gg.clips.forEach(c=>(c.posts||[]).forEach(p=>{const t=new Date(p.posted*1000);
    if(!last[p.platform]||t>last[p.platform])last[p.platform]=t;
    const dk=t.toDateString()+p.platform; perDayCount[dk]=(perDayCount[dk]||0)+1})));
  for(const it of items){
    const s=slots.find(t=>{const dk=t.toDateString()+it.p;
      return (perDayCount[dk]||0)<perDay && (!last[it.p]||t-last[it.p]>=gap*60000)});
    if(!s)continue;
    last[it.p]=s; const dk=s.toDateString()+it.p; perDayCount[dk]=(perDayCount[dk]||0)+1;
    rows.push({t:s<now?now:s,p:it.p,c:it.c});
  }
  rows.sort((a,b)=>a.t-b.t);
  return {camp,plats,wins,rows,low,perDay,skipped:items.length-rows.length};
}
function buildPlan(){
  const pl=computePlan(plGroup), camp=pl.camp, plats=pl.plats, perDay=pl.perDay, low=pl.low, skipped=pl.skipped;
  if(!pl.wins.length){$('#pllist').innerHTML='<div class="empty">Add the times you\'re free above.</div>';return}
  if(!plats.length){$('#pllist').innerHTML='<div class="empty">None of your accounts match this campaign\'s platforms ('+
    (camp.platforms||[]).map(p=>PL_SHORT[p]).join(', ')+'). Tick your accounts above.</div>';return}
  plRows=pl.rows;
  $('#plinfo').innerHTML=`${plRows.length} post${plRows.length===1?'':'s'} planned on ${plats.map(p=>PL_SHORT[p]).join(' & ')}, best clips first, `+
    `max ${perDay} a day per platform, ${plSet.plan_gap}h apart.`+(skipped?` ${skipped} didn't fit in the next 7 days.`:'')+
    (low?` ${low} clip${low>1?'s':''} rated under 2★ left out.`:'')+
    (camp.platforms&&camp.platforms.some(p=>!(plSet.my_platforms||[]).includes(p))?
      `<br>This campaign also pays on ${camp.platforms.filter(p=>!(plSet.my_platforms||[]).includes(p)).map(p=>PL_SHORT[p]).join(', ')} - tick it above if you have that account.`:'');
  let html='',lastDay='';
  const dayName=t=>{const d=Math.round((new Date(t.toDateString())-new Date(new Date().toDateString()))/864e5);
    return d===0?'Today':d===1?'Tomorrow':t.toLocaleDateString([], {weekday:'long',day:'numeric',month:'short'})};
  plRows.forEach((r,i)=>{const dn=dayName(r.t); if(dn!==lastDay){html+=`<div class="plday">${dn}</div>`;lastDay=dn}
    html+=`<div class="plrow" id="plr${i}"><span class="tm">${r.t.toTimeString().slice(0,5)}</span><span class="pf">${PL_SHORT[r.p]}</span>
      <img src="/thumb/${enc(r.c.path)}?v=${r.c.v}"><span class="nm" title="${esc(r.c.name)}">${starStr(r.c.stars)} ${esc(r.c.name)}</span>
      ${r.c.caption?`<button class="small" onclick="copyText(capFor(plRows[${i}].c.path,plRows[${i}].p),this)">Caption</button>`:''}
      <button class="small" onclick="plPosted(${i})">✓ Posted</button></div>`});
  $('#pllist').innerHTML=html||'<div class="empty">Nothing left to post from this batch 🎉</div>';
}
/* ---------- phones: share a clip straight to TikTok / Instagram / gallery (needs the https link) ---------- */
const CAN_SHARE=!!(navigator.canShare&&window.isSecureContext&&/Android|iPhone|iPad/i.test(navigator.userAgent));
const blobCache={};
async function shareClip(btn,path){
  const name=path.split('/').pop();
  try{
    if(!blobCache[path]){btn.textContent='⏳';
      const b=await (await fetch('/clips/'+enc(path))).blob(); blobCache[path]=new File([b],name,{type:'video/mp4'})}
    btn.textContent='↗';
    const f=blobCache[path];
    if(!navigator.canShare({files:[f]})){location.href='/clips/'+enc(path)+'?dl=1';return}
    const cap=capStore[path]; if(cap){try{await navigator.clipboard.writeText(cap)}catch(e){}}
    await navigator.share({files:[f]});
  }catch(e){
    if(e.name==='NotAllowedError')btn.textContent='↗ tap again';   // download took a while: next tap shares at once
    else btn.textContent='↗';
  }
}
async function variations(path){
  const r=await api('/api/variations',{path}); if(r.error){alert(r.error);return} poll();
}
/* ---------- trim: move a clip's start/end and re-make it ---------- */
let tr=null;
const fmtT=x=>{x=Math.max(0,x);const m=Math.floor(x/60),s=(x%60);return m+':'+(s<10?'0':'')+s.toFixed(2)};
function trWordsNear(t,before){
  const w=tr.words; if(!w.length)return '';
  const i=w.findIndex(x=>x[0]>=t-0.02);
  const a=w.slice(Math.max(0,(i<0?w.length:i)-4),i<0?w.length:i).map(x=>esc(x[2])).join(' ');
  const b=(i<0?[]:w.slice(i,i+4)).map(x=>esc(x[2])).join(' ');
  return `…${a} <span style="color:var(--acc)">|</span> <i>${b}</i>…`;
}
function trShow(){
  $('#trs').textContent=fmtT(tr.s); $('#tre').textContent=fmtT(tr.e);
  $('#trsw').innerHTML=trWordsNear(tr.s); $('#trew').innerHTML=trWordsNear(tr.e);
  const ch=(tr.s!==tr.s0||tr.e!==tr.e0);
  $('#trinfo').textContent=`New length ${(tr.e-tr.s).toFixed(1)}s (was ${(tr.e0-tr.s0).toFixed(1)}s). The | shows where it cuts in the speech.`;
  $('#trgo').disabled=!ch;
}
async function trimDlg(path,name){
  const r=await api('/api/clipinfo',{path}); if(r.error){alert("This clip can't be trimmed (it was made before trimming existed)");return}
  if(!r.source_ok){alert("The original video isn't on the PC any more, so this clip can't be trimmed.");return}
  tr={path,s:r.start,e:r.end,s0:r.start,e0:r.end,max:r.source_len||1e9,words:r.words||[]};
  $('#trname').textContent=name; $('#trvid').src='/clips/'+enc(path)+'?t='+Date.now(); $('#trmsg').textContent='';
  trShow(); $('#trdlg').showModal();
}
function trClose(){$('#trvid').pause();$('#trdlg').close()}
document.querySelectorAll('#trdlg [data-t]').forEach(b=>b.onclick=()=>{
  const d=+b.dataset.d; if(b.dataset.t==='s')tr.s=Math.max(0,Math.min(tr.e-1,+(tr.s+d).toFixed(2)));
  else tr.e=Math.min(tr.max,Math.max(tr.s+1,+(tr.e+d).toFixed(2))); trShow()});
$('#trgo').onclick=async()=>{const r=await api('/api/trim',{path:tr.path,start:tr.s,end:tr.e});
  if(r.error){$('#trmsg').textContent=r.error;return} trClose(); poll()};
/* ---------- phone dialog ---------- */
async function phoneDlg(){
  const r=await api('/api/phone'), here=location.origin+'/';
  $('#phbody').innerHTML=(r.urls.length?r.urls.map(u=>`<div class="phurl"><code>${esc(u.url)}</code><span>${esc(u.what)}</span>
      <button type="button" class="small" onclick="copyText('${esc(u.url)}',this)">Copy</button></div>`).join(''):'<p>No network address found.</p>')+
    (r.tailscale?'':'<p>⚠ Tailscale isn\'t running on this PC - only the Home Wi-Fi link works, and only at home.</p>')+
    (r.https?'':'<p>Tip: run the phone setup once (see the README) to get the <b>secure https link</b> - that lets you add Clipper to your home screen and send clips straight to your gallery, TikTok or Instagram.</p>')+
    `<p style="color:var(--dim)">You're on: <code>${esc(here)}</code>${window.isSecureContext?' 🔒':''}</p>`+
    `<p style="color:var(--dim)">On the phone: open the link in Chrome → ⋮ → <b>Add to Home screen</b>. Then on any clip: <b>↗</b> sends it straight to TikTok, Instagram or your gallery (the caption gets copied too), <b>📲 Save</b> downloads it.</p>`;
  $('#phdlg').showModal();
}
if('serviceWorker' in navigator&&window.isSecureContext){try{navigator.serviceWorker.register('/sw.js')}catch(e){}}
function campOf(path){const key=path.split('/')[0]; return camps[key.replace(/~/g,'/')]||{}}
function capFor(path,plat){
  let t=capStore[path]||''; const camp=campOf(path), tags=camp.mentions||{}, main=camp.mention||'', want=tags[plat];
  if(t&&main&&want&&want.toLowerCase()!==main.toLowerCase()) t=t.split(new RegExp(main.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi')).join(want);
  return t;
}
function capButtons(c){
  if(!c.caption) return '';
  const camp=campOf(c.path), tags=camp.mentions||{}, q=esc(c.path).replace(/'/g,"\\'");
  const plats=(camp.platforms||[]).filter(p=>tags[p]&&tags[p].toLowerCase()!==(camp.mention||'').toLowerCase());
  if(!Object.keys(tags).length||!(camp.platforms||[]).length)
    return `<button class="capbtn" onclick="copyText(capStore['${q}'],this)">Copy caption</button>`;
  return `<div class="capsplit">${(camp.platforms||[]).map(p=>`<button class="capbtn" title="Caption with the ${esc(PL_SHORT[p]||p)} tag"
    onclick="copyText(capFor('${q}','${p}'),this)">Copy · ${esc(PL_SHORT[p]||p)}</button>`).join('')}</div>`;
}
/* ---------- platform chips under each clip: when to post, then a timer to submit the link ---------- */
const SUBMIT_MIN=30;
let dueAt={};
function whenTxt(t){const d=Math.round((new Date(t.toDateString())-new Date(new Date().toDateString()))/864e5);
  const hm=t.toTimeString().slice(0,5);
  return d===0?hm:d===1?'tmrw '+hm:t.toLocaleDateString([], {weekday:'short'})+' '+hm}
function platRow(c){
  const g=lastGroups.find(g=>g.clips.includes(c)); const camp=g?groupCamp(g):{};
  const plats=camp.platforms||[]; if(!plats.length) return '';
  const q=esc(c.path).replace(/'/g,"\\'");
  return '<div class="plats">'+plats.map(p=>{
    const name=PL_SHORT[p]||p, post=(c.posts||[]).find(x=>x.platform===p);
    if(post&&post.url) return `<button class="pt done" title="Posted and link saved"><b>✓ ${name}</b>done</button>`;
    if(post){const until=(post.posted+SUBMIT_MIN*60)*1000;
      return `<button class="pt timer" data-until="${until}" onclick="addLink('${post.id}','${name}')" title="Submit the link on Whop, then tap to save it here"><b>${name}</b><span>submit link</span></button>`}
    if(!(plSet.my_platforms||[]).includes(p))
      return `<button class="pt off" title="This campaign pays on ${name}, but you haven't ticked it as one of your accounts (🗓 Plan)"><b>${name}</b>no account</button>`;
    const t=dueAt[c.path+'|'+p], soon=t&&t-Date.now()<30*60000;
    return `<button class="pt ${soon?'now':''}" onclick="markPosted('${q}','${p}')" title="Tap after you've posted it on ${name} - starts the ${SUBMIT_MIN}-minute timer"><b>${name}</b>${t?(soon?'post now':whenTxt(t)):'any time'}</button>`
  }).join('')+'</div>';
}
async function markPosted(path,plat){
  if(!confirm(`Posted this clip on ${PL_SHORT[plat]}?\n\nThis starts a ${SUBMIT_MIN}-minute timer to submit the link on Whop.`))return;
  const r=await api('/api/results/add',{clip:path,platform:plat,url:''}); if(r.error){alert(r.error);return}
  loadClips();
}
async function addLink(id,name){
  const url=prompt(`Paste the ${name} post link (and submit it on Whop too):`); if(!url||!url.trim())return;
  await api('/api/results/update',{id,url:url.trim()}); loadClips();
}
function tickTimers(){document.querySelectorAll('.pt[data-until]').forEach(el=>{
  const left=Math.round((+el.dataset.until-Date.now())/1000), sp=el.querySelector('span');
  if(left>0){sp.textContent=`⏱ ${Math.floor(left/60)}:${String(left%60).padStart(2,'0')} to submit`;el.classList.remove('late')}
  else{sp.textContent='⚠ link overdue';el.classList.add('late')}})}
setInterval(tickTimers,1000);
function plPosted(i){const r=plRows[i]; logPost(r.c.path,r.c.name); $('#pd_plat').value=r.p;
  $('#plr'+i).classList.add('done')}
$('#plsave').onclick=async()=>{
  plSet.plan_windows=$('#pl_win').value; plSet.plan_per_day=$('#pl_day').value; plSet.plan_gap=$('#pl_gap').value;
  plSet.my_platforms=[...document.querySelectorAll('#pl_mine input:checked')].map(i=>i.value);
  await api('/api/settings',{plan_windows:plSet.plan_windows,plan_per_day:plSet.plan_per_day,plan_gap:plSet.plan_gap,my_platforms:plSet.my_platforms});
  plSet=await api('/api/settings'); buildPlan();
};
$('#plcopy').onclick=()=>copyText(plRows.map(r=>`${r.t.toLocaleString([], {weekday:'short',hour:'2-digit',minute:'2-digit'})}  ${PL_SHORT[r.p]}  ${r.c.name}`).join('\n'),$('#plcopy'));
$('#plics').onclick=()=>{
  const z=n=>String(n).padStart(2,'0'), f=t=>`${t.getFullYear()}${z(t.getMonth()+1)}${z(t.getDate())}T${z(t.getHours())}${z(t.getMinutes())}00`;
  const escI=t=>String(t||'').replace(/\\/g,'\\\\').replace(/\n/g,'\\n').replace(/[,;]/g,m=>'\\'+m);
  let ics='BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//snxffclip//planner//EN\r\n';
  plRows.forEach((r,i)=>{const end=new Date(r.t.getTime()+30*60000);
    ics+=`BEGIN:VEVENT\r\nUID:clip-${f(r.t)}-${i}@snxffclip\r\nDTSTAMP:${f(new Date())}\r\nDTSTART:${f(r.t)}\r\nDTEND:${f(end)}\r\n`+
      `SUMMARY:${escI('Post '+r.c.name+' on '+PL_SHORT[r.p])}\r\nDESCRIPTION:${escI((r.c.caption||'')+'\n\nSubmit the link in Whop within 30 minutes!')}\r\n`+
      `BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Time to post a clip\r\nTRIGGER:-PT5M\r\nEND:VALARM\r\nEND:VEVENT\r\n`});
  ics+='END:VCALENDAR\r\n';
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([ics],{type:'text/calendar'}));
  a.download='clip-posting-plan.ics'; document.body.appendChild(a); a.click(); a.remove();
};

async function del(p){
  if(!confirm('Delete this clip?'))return;
  await api('/api/delete',{path:p}); loadClips();
}
(()=>{const tp=$('#themepick'); tp.value=document.documentElement.dataset.theme||'';
  tp.onchange=()=>{const v=tp.value; if(v)document.documentElement.dataset.theme=v; else delete document.documentElement.dataset.theme;
    try{localStorage.setItem('clipper_theme',v)}catch(e){}}})();
poll(); loadClips(); setInterval(poll,1500);
// quietly refresh post views when the page opens (at most every 6 hours)
setTimeout(async()=>{try{const k='clipper_views_at', last=+(localStorage.getItem(k)||0);
  if(Date.now()-last<6*3600e3)return; localStorage.setItem(k,Date.now());
  await api('/api/results/refresh',{}); if($('#results').style.display!=='none')loadResults()}catch(e){}},8000);
</script></body></html>"""


def serve_ui(args):
    import os
    import signal
    import socket
    import threading
    import uuid
    from collections import deque
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from queue import Queue
    from urllib.parse import unquote, urlparse, parse_qs

    out_root = args.out_dir.resolve()
    music_dir = Path(__file__).resolve().parent / "background-songs"
    music_dir.mkdir(parents=True, exist_ok=True)

    _fonts = []

    def fonts_cache():
        if not _fonts:
            _fonts.extend(list_fonts() or ["DejaVu Sans"])
        return _fonts

    def clean_cap(c):
        if not isinstance(c, dict):
            return {}
        hexok = lambda v: isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v)
        out = {}
        if isinstance(c.get("font"), str) and re.fullmatch(r"[\w .\-']{1,60}", c["font"]):
            out["font"] = c["font"]
        try:
            out["size"] = max(30, min(180, int(c.get("size"))))
        except (TypeError, ValueError):
            pass
        for k in ("text", "outline"):
            if hexok(c.get(k)):
                out[k] = c[k]
        if c.get("highlight") == "" or hexok(c.get("highlight")):
            out["highlight"] = c["highlight"]
        if c.get("pos") in POSITIONS:
            out["pos"] = c["pos"]
        for k in ("upper", "pop"):
            if isinstance(c.get(k), bool):
                out[k] = c[k]
        return out

    def songs():
        return sorted(f.name for f in music_dir.iterdir()
                      if f.is_file() and f.suffix.lower() in AUDIO_EXT)

    thumbs = CACHE / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    jobs, order, q = {}, [], Queue()
    lock = threading.Lock()

    # ---------- job runner ----------
    def P(job, v):
        """map a single-video progress value onto the whole batch"""
        if job.get("render"):
            return 60 + max(0, v - 62) / 38 * 40
        if job.get("scan"):
            i, n = job["scan"]
            return ((i - 1) + min(v, 62) / 62) / n * 60
        if job.get("bi"):
            i, n = job["bi"]
            return ((i - 1) + v / 100) / n * 100
        return v

    def parse_line(job, line):
        line = line.rstrip()
        if m := re.match(r"=== scan (\d+)/(\d+): (.*) ===", line):
            job["scan"] = (int(m[1]), int(m[2]))
            job["title"] = f"Bulk scrape · scanning {m[1]} of {m[2]}: {m[3]}"
            job["progress"] = P(job, 0)
            job["log"].append(line)
            return
        if line.startswith("=== rendering ==="):
            job["render"], job["mv"] = True, 0
            job["title"] = "Bulk scrape · making the best clips"
            job["log"].append(line)
            return
        if m := re.match(r"=== video (\d+)/(\d+): (.*) ===", line):
            job["bi"] = (int(m[1]), int(m[2]))
            job["title"] = f"{m[3]}  ({m[1]} of {m[2]})"
            job["mv"], job["kv"] = 0, 1
            job["progress"] = P(job, 0)
            job["log"].append(line)
            return
        if job.get("clip") and (m := re.search(r"time=(\d+):(\d+):([\d.]+)", line)):
            t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            pct = min(t / job["dur"], 1) if job.get("dur") else 0
            job["progress"] = max(job["progress"], 5 + 93 * pct)
            job["stage"] = f"Re-rendering… {pct * 100:.0f}%"
            return
        if not line or line.lstrip().startswith("frame="):
            return
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            job["progress"] = max(job["progress"], P(job, min(10, float(m[1]) / 10)))
            job["stage"] = f"Downloading {float(m[1]):.0f}%"
            return                                  # too chatty for the log
        job["log"].append(line)
        if m := re.match(r"> (?:downloading|using cached download): (.*)", line):
            job["title"] = m[1]
            job["stage"] = "Downloading"
            if "cached" in line:
                job["progress"] = P(job, 10)
        elif line.startswith("> analysing"):
            job["stage"], job["progress"] = "Listening to the video…", P(job, 10)
        elif m := re.match(r"\s*([\d:]+) / ([\d:]+)$", line):
            done, total = to_seconds(m[1]), to_seconds(m[2]) or 1
            job["progress"] = P(job, 10 + 50 * min(done / total, 1))
            job["stage"] = f"Transcribing {m[1]} / {m[2]}"
        elif "asking AI" in line:
            job["stage"], job["progress"] = "AI is picking the best bits…", P(job, 61)
        elif m := re.search(r"top (\d+) moments", line):
            job["kv"] = int(m[1])
            if not job.get("bi"):
                job["k"] = int(m[1])
            job["stage"], job["progress"] = "Picked the moments", P(job, 62)
        elif line.startswith("> ") and re.match(r"> \d\d-", line) and not job.get("clip"):
            n = job.get("mv", job["made"]) + 1
            job["stage"] = f"Making clip {n} of {job.get('kv') or job['k']}"
        elif line.strip().startswith("saved "):
            job["made"] += 1
            job["mv"] = job.get("mv", 0) + 1
            per = job["mv"] / max(1, job.get("kv") or job["k"])
            job["progress"] = P(job, 62 + 38 * min(per, 1))
        elif line.startswith("error:"):
            job["error"] = line[6:].strip()

    def worker():
        while True:
            job = q.get()
            if job["status"] == "cancelled":
                continue
            p = job["params"]
            if p.get("setup") == "gpu":
                cmd = [sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "--progress-bar", "off",
                       *CUDA_PKGS]
                run_job(job, cmd)
                continue
            if p.get("restyle"):
                cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--restyle", p["restyle"],
                       "--style", p["style"], "--words", str(p["words"]), "--cap-json", json.dumps(p["cap"])]
                if p.get("trim"):
                    cmd += ["--trim-start", f"{p['trim'][0]:.2f}", "--trim-end", f"{p['trim'][1]:.2f}"]
                if p.get("variations"):
                    cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--variations", p["restyle"]]
                if p["match"]:
                    cmd.append("--match-colors")
                run_job(job, cmd)
                continue
            if p.get("bulk"):
                cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--bulk", "-k", str(p["k"]),
                       "--len", p["len"], "-m", p["mode"], "--model", p["model"]]
                job["k"] = 1
            elif p.get("batch"):
                cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--batch",
                       "--len", p["len"], "-m", p["mode"], "--model", p["model"]]
                job["k"] = 1
            else:
                cmd = [sys.executable, "-u", str(Path(__file__).resolve()), p["url"], "--auto",
                       "-k", str(p["k"]), "--len", p["len"], "-m", p["mode"],
                       "--model", p["model"]]
            if not p["campaign"]:
                cmd += ["-o", str(out_root)]
            if not p["captions"]:
                cmd.append("--no-captions")
            if not p.get("track", True):
                cmd.append("--no-track")
            if not p.get("cut_pauses", True):
                cmd.append("--keep-pauses")
            for k, flag in (("laughs", "--no-laughs"), ("laugh_tail", "--no-laugh-tail"), ("chat", "--no-chat"),
                            ("words", "--no-smart-words"), ("qa", "--no-interview-endings")):
                if not (p.get("smart") or {}).get(k, True):
                    cmd.append(flag)
            if p["ollama"]:
                cmd += ["--ollama", p["ollama"]]
            if p["music"]:
                cmd += ["--music", p["music"], "--music-vol", str(p["music_vol"])]
            if p["campaign"]:
                cmd += ["--campaign", p["campaign"]]
            cmd += ["--style", p["style"], "--words", str(p["words"]), "--cap-json", json.dumps(p["cap"])]
            if p["match"]:
                cmd.append("--match-colors")
            run_job(job, cmd)

    hist_file = CACHE / "history.json"
    hist_lock = threading.Lock()

    def add_history(job):
        p = job.get("params", {})
        kind = ("GPU setup" if p.get("setup") else "Restyle" if p.get("restyle") else "Bulk folder scrape" if p.get("bulk")
                else "Clip each video" if p.get("batch") else "Clip a video")
        url = p.get("url") or ""
        camp_name = (p.get("campaign") or "").split("/")[-1].replace("-", " ")
        if p.get("restyle"):
            title = Path(p["restyle"]).stem
        elif p.get("batch") or p.get("bulk"):
            title = camp_name or "folder"
        else:
            title = url if is_url(url) else Path(url).name if url else (job.get("title") or "")
        entry = {"title": title, "kind": kind, "status": job["status"],
                 "campaign": p.get("campaign", ""), "started": job.get("started") or time.time(),
                 "secs": time.time() - (job.get("started") or time.time()), "made": job.get("made", 0),
                 "error": job.get("error", "") if job["status"] == "error" else "",
                 "log": list(job.get("log", []))[-80:]}
        with hist_lock:
            try:
                h = json.loads(hist_file.read_text()) if hist_file.is_file() else []
            except ValueError:
                h = []
            h.insert(0, entry)
            hist_file.write_text(json.dumps(h[:300]))

    def run_job(job, cmd):
            job["started"] = time.time()
            try:
                run_job_inner(job, cmd)
            finally:
                if job["status"] == "running":
                    job["status"] = "error"
                    job["error"] = job.get("error") or "Stopped unexpectedly"
                add_history(job)

    def run_job_inner(job, cmd):
            job["status"], job["stage"] = "running", ("Re-rendering…" if job.get("clip") else "Starting…")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
                                       else {"start_new_session": True}))
            job["proc"] = proc
            buf = b""
            while True:
                chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for part in parts:
                    parse_line(job, part.decode("utf-8", "replace"))
            if buf:
                parse_line(job, buf.decode("utf-8", "replace"))
            rc = proc.wait()
            job["proc"] = None
            if job["status"] == "cancelled":
                job["stage"] = "Cancelled"
            elif rc == 0:
                job["status"], job["progress"] = "done", 100
                job["stage"] = ("Done — graphics card libraries installed. Press ⚙️ AI to check it"
                                if job["params"].get("setup") else "Done — clip updated ✓" if job.get("clip")
                                else f"Done — {job['made']} clips ready")
            else:
                job["status"] = "error"
                job["error"] = job.get("error") or "Something went wrong — open the log"

    threading.Thread(target=worker, daemon=True).start()

    # ---------- helpers ----------
    def learn_data():
        return load_learning()

    res_lock = threading.Lock()

    def results_summary():
        posts = load_results().get("posts", [])
        est = lambda p: (p.get("views") or 0) / 1000 * float(p.get("rate") or 0)
        camps = {}
        for p in posts:
            k = p.get("campaign_name") or p.get("campaign") or "No campaign"
            c = camps.setdefault(k, {"name": k, "posts": 0, "views": 0, "earn": 0.0, "rate": p.get("rate")})
            c["posts"] += 1
            c["views"] += p.get("views") or 0
            c["earn"] += est(p)
        learn = load_learning()
        insights = []
        if learn:
            for key, (avg, n) in sorted(learn["b"].items(), key=lambda kv: -kv[1][0]):
                insights.append({"what": key.split(":")[1] + (" clips" if key.startswith("len") else
                                                              " (product shots)" if key.endswith("visual") else " (talking)"),
                                 "avg": round(avg), "n": n, "vs": round(avg / learn["avg"], 2)})
        return {"posts": sorted(posts, key=lambda p: -p.get("posted", 0)),
                "total": {"posts": len(posts), "views": sum(p.get("views") or 0 for p in posts),
                          "earn": round(sum(est(p) for p in posts), 2),
                          "stale": sum(1 for p in posts if time.time() - p.get("updated", 0) > 3 * 86400)},
                "campaigns": sorted(camps.values(), key=lambda c: -c["views"]),
                "insights": insights, "learning": bool(learn),
                "need": max(0, 6 - sum(1 for p in posts if p.get("views") is not None))}

    def version_info(fetch=False):
        repo = Path(__file__).resolve().parent
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=60)
        try:
            if fetch:
                g("fetch", "-q")
            head = g("log", "-1", "--format=%h · %cd", "--date=format:%d %b %H:%M").stdout.strip()
            behind = g("rev-list", "--count", "HEAD..@{u}").stdout.strip()
            return {"version": head or "unknown", "behind": int(behind) if behind.isdigit() else 0, "git": True}
        except (OSError, subprocess.TimeoutExpired):
            return {"version": "unknown", "behind": 0, "git": False}

    def delete_clip(rel):
        f, root = safe(rel, want_root=True)
        if not f or f.suffix != ".mp4":
            return False
        f.unlink()
        for extra in (f.with_suffix(".txt"), f.with_suffix(".clip.json")):
            if extra.is_file():
                extra.unlink()
        folder = f.parent
        if folder != root and not any(folder.glob("*.mp4")):
            for x in folder.iterdir():
                if x.is_file():
                    x.unlink()
            folder.rmdir()
        return True

    def folder_size(p):
        n, size = 0, 0
        try:
            for f in Path(p).rglob("*"):
                if f.is_file():
                    size += f.stat().st_size
                    if f.suffix.lower() in VIDEO_EXT:
                        n += 1
        except OSError:
            pass
        return n, size

    def projects_info():
        camps = []
        for cid, name, agency in list_campaigns():
            d = campaign_dir(cid)
            sn, ss = folder_size(d / "source")
            cn, cs = folder_size(d / "clips")
            camps.append({"id": cid, "name": name, "agency": agency, "path": str(d),
                          "sources": sn, "source_size": ss, "clips": cn, "clips_size": cs})
        fn, fs = folder_size(out_root)
        return {"root": str(CAMPAIGN_DIR), "custom": bool(PROJECTS), "free": str(out_root),
                "free_clips": fn, "free_size": fs, "campaigns": camps,
                "default": str(Path(__file__).resolve().parent / "campaigns")}

    def allowed_open(p):
        p = Path(p).resolve()
        for base in (CAMPAIGN_DIR.resolve(), out_root, Path(__file__).resolve().parent):
            if p == base or base in p.parents:
                return True
        return False

    def open_folder(p):
        p = Path(p)
        p.mkdir(parents=True, exist_ok=True)
        if WINDOWS:
            os.startfile(str(p))                                   # noqa - Windows only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def list_dirs(path):
        """For the folder picker: sub-folders of path (default: home)."""
        p = Path(path).expanduser() if path else Path.home()
        if not p.is_dir():
            p = Path.home()
        p = p.resolve()
        try:
            dirs = sorted([x.name for x in p.iterdir() if x.is_dir() and not x.name.startswith(".")
                           and not x.name.startswith("$")], key=str.lower)[:400]
        except OSError:
            dirs = []
        drives = []
        if WINDOWS:
            import string
            drives = [f"{c}:\\" for c in string.ascii_uppercase if Path(f"{c}:\\").exists()]
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else "",
                "dirs": dirs, "home": str(Path.home()), "drives": drives}

    def roots():
        """key -> (clips folder, label). '_' is ~/Clips, others are campaigns."""
        r = {"_": (out_root, "")}
        for cid, name, agency in list_campaigns():
            r[cid.replace("/", "~")] = ((campaign_dir(cid) / "clips").resolve(), f"{agency} › {name}")
        return r

    def safe(rel, want_root=False):
        rel = unquote(rel)
        key, _, rest = rel.partition("/")
        r = roots().get(key)
        if not r or not rest:
            return (None, None) if want_root else None
        p = (r[0] / rest).resolve()
        ok = r[0] in p.parents and p.is_file()
        if want_root:
            return (p if ok else None, r[0])
        return p if ok else None

    def clip_groups():
        groups = {}
        posted_count, post_info = {}, {}
        for p in load_results().get("posts", []):
            posted_count.setdefault(p.get("clip"), []).append(p.get("platform"))
            post_info.setdefault(p.get("clip"), []).append({"id": p.get("id"), "platform": p.get("platform"),
                                                            "url": bool(p.get("url")), "posted": p.get("posted", 0)})
        for key, (root, label) in roots().items():
            if not root.is_dir():
                continue
            camp = {}
            if key != "_":
                try:
                    camp = json.loads((campaign_dir(key.replace("~", "/")) / "campaign.json").read_text())
                except (OSError, ValueError):
                    camp = {}
            for f in root.rglob("*.mp4"):
                rel = f.relative_to(root)
                if len(rel.parts) > 2:
                    continue
                vid = rel.parts[0].replace("-", " ") if len(rel.parts) == 2 else "Single clips"
                g = f"{label} › {vid}" if label else vid
                cap = f.with_suffix(".txt")
                groups.setdefault(g, []).append(
                    {"path": f"{key}/{rel.as_posix()}", "name": f.stem, "mtime": f.stat().st_mtime,
                     "v": int(f.stat().st_mtime), "styled": f.with_suffix(".clip.json").is_file(),
                     "checks": clip_checks(f, camp), "_f": f, "_camp": camp,
                     "manual": camp.get("manual_checks") or [],
                     "posted": posted_count.get(f"{key}/{rel.as_posix()}", []),
                     "posts": post_info.get(f"{key}/{rel.as_posix()}", []),
                     "caption": cap.read_text().strip() if cap.is_file() else ""})
        out = []
        for name, clips in groups.items():
            sc = []
            for cl in clips:
                try:
                    sc.append(json.loads(cl["_f"].with_suffix(".clip.json").read_text()).get("meta", {}).get("score"))
                except (OSError, ValueError):
                    sc.append(None)
            known = sorted(x for x in sc if x is not None)
            ld = learn_data()
            for cl, x in zip(clips, sc):
                rank = (known.index(x) / (len(known) - 1) if len(known) > 1 else 0.5) if x is not None else None
                cl["stars"], cl["why"] = rate_clip(cl.pop("_f"), cl.pop("_camp"), cl["checks"], ld, rank)
            clips.sort(key=lambda c: (-c["stars"], c["name"]))
            out.append({"name": name, "clips": clips, "mtime": max(c["mtime"] for c in clips)})
        return sorted(out, key=lambda g: g["mtime"], reverse=True)

    _lens = {}

    def video_len(f):
        try:
            key = (f, Path(f).stat().st_mtime)
        except OSError:
            return 0
        if key not in _lens:
            _lens[key] = duration_of(Path(f))
        return _lens[key]

    def transcript_cached(f: Path):
        st = f.stat()
        for m in ("small.en", "base.en", "medium.en", "small", "base", "medium", "tiny", "tiny.en"):
            key = hashlib.md5(f"{f.resolve()}|{st.st_size}|{st.st_mtime}|{m}".encode()).hexdigest()
            if (CACHE / f"{key}.json").exists():
                return True
        return False

    def sources(cid):
        src = []
        if cid in [c for c, _, _ in list_campaigns()]:
            d = campaign_dir(cid) / "source"
            if d.is_dir():
                src = sorted((str(f) for f in d.iterdir() if f.suffix.lower() in VIDEO_EXT),
                             key=lambda x: Path(x).stat().st_mtime, reverse=True)
        dl, cutoff = [], time.time() - 14 * 86400
        dd = Path.home() / "Downloads"
        if dd.is_dir():
            dl = sorted((str(f) for f in dd.iterdir()
                         if f.suffix.lower() in VIDEO_EXT and f.stat().st_mtime > cutoff),
                        key=lambda x: Path(x).stat().st_mtime, reverse=True)
        pending, folder = 0, ""
        if src:
            cl = campaign_dir(cid) / "clips"
            pending = sum(1 for f in src
                          if not any((cl / slug(Path(f).stem, 50)).glob("*.mp4")))
        if cid:
            folder = str(campaign_dir(cid) / "source").replace(str(Path.home()), "~")
        dur = {f: video_len(f) for f in src + dl}
        unscanned = [f for f in src if not transcript_cached(Path(f))]
        return {"source": src, "downloads": dl, "pending": pending, "folder": folder, "dur": dur,
                "unscanned": len(unscanned), "unscanned_dur": sum(dur.get(f, 0) for f in unscanned)}

    def thumb_for(f: Path):
        st = f.stat()
        t = thumbs / (hashlib.md5(f"{f}|{st.st_mtime}".encode()).hexdigest() + ".jpg")
        if not t.exists():
            at = 1.0                                  # a frame with captions showing, so restyles are visible
            try:
                sd = json.loads(f.with_suffix(".clip.json").read_text())
                if sd.get("captions") and sd.get("words"):
                    w0 = sd["words"][0]
                    at = max(0.3, min(w0[1] - sd["start"] + 0.15, sd["end"] - sd["start"] - 0.2))
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                pass
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(f),
                            "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "5", str(t)])
        return t if t.exists() else None

    def public(job):
        p = job["params"]
        return {k: (list(v) if k == "log" else v) for k, v in job.items()
                if k not in ("proc", "params")} | {"url": p["url"], "campaign": p.get("campaign", ""),
                                                     "kind": "setup" if p.get("setup") else "bulk" if p.get("bulk") else "batch" if p.get("batch")
                                                     else "restyle" if p.get("restyle") else "single"}

    # ---------- http ----------
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def send_file(self, path: Path, ctype, download=False):
            size = path.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
            if m and (m[1] or m[2]):
                if m[1]:
                    start = int(m[1])
                    end = min(int(m[2]), size - 1) if m[2] else size - 1
                else:
                    start = max(0, size - int(m[2]))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(end - start + 1))
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0:
                        data = fh.read(min(65536, left))
                        if not data:
                            break
                        self.wfile.write(data)
                        left -= len(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/manifest.json", "/sw.js", "/icon.svg"):
                body, ctype = {"/manifest.json": (PWA_MANIFEST, "application/manifest+json"),
                               "/sw.js": (PWA_SW, "text/javascript"),
                               "/icon.svg": (PWA_ICON, "image/svg+xml")}[u.path]
                body = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/api/phone":
                return self.send_json(phone_info(args.port))
            if u.path == "/":
                body = UI_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/jobs":
                with lock:
                    self.send_json([public(jobs[i]) for i in reversed(order)])
            elif u.path == "/api/results":
                self.send_json(results_summary())
            elif u.path == "/api/gpu":
                self.send_json(gpu_status())
            elif u.path == "/api/version":
                self.send_json(version_info(fetch="fetch" in parse_qs(u.query)))
            elif u.path == "/api/projects":
                self.send_json(projects_info())
            elif u.path == "/api/dirs":
                self.send_json(list_dirs(parse_qs(u.query).get("path", [""])[0]))
            elif u.path == "/api/settings":
                st = load_settings()
                self.send_json({"ai": st.get("ai", ""), "ollama_model": st.get("ollama_model", ""),
                                "claude_model": st.get("claude_model", "claude-haiku-5-5"),
                                "has_key": bool(st.get("claude_key")),
                                "plan_windows": st.get("plan_windows", "12:00-13:00, 17:30-22:00"),
                                "plan_per_day": st.get("plan_per_day", 3), "plan_gap": st.get("plan_gap", 2),
                                "my_platforms": st.get("my_platforms", ["TikTok", "Instagram Reels"])})
            elif u.path == "/api/campaigncheck":
                cid = parse_qs(u.query).get("id", [""])[0]
                if cid not in [x for x, _, _ in list_campaigns()]:
                    return self.send_json([])
                self.send_json(campaign_checks(cid))
            elif u.path == "/api/history":
                try:
                    self.send_json(json.loads(hist_file.read_text()) if hist_file.is_file() else [])
                except ValueError:
                    self.send_json([])
            elif u.path == "/api/downloads":
                self.send_json(downloads_list())
            elif u.path == "/api/campaign":
                cid = parse_qs(u.query).get("id", [""])[0]
                if cid not in [c for c, _, _ in list_campaigns()]:
                    return self.send_json({"error": "No such campaign"}, 404)
                self.send_json(json.loads((campaign_dir(cid) / "campaign.json").read_text()))
            elif u.path == "/api/campaigns":
                out = []
                for cid, name, agency in list_campaigns():
                    c = json.loads((campaign_dir(cid) / "campaign.json").read_text())
                    out.append({"id": cid, "name": name, "agency": agency,
                                "music": c.get("music", True), "on_screen": c.get("on_screen"),
                                "platforms": c.get("platforms") or [], "rate": c.get("rate"),
                                "mention": c.get("mention") or "", "mentions": c.get("mentions") or {},
                                "best": best_settings(c, campaign_dir(cid))})
                self.send_json(out)
            elif u.path == "/api/sources":
                self.send_json(sources(parse_qs(u.query).get("campaign", [""])[0]))
            elif u.path == "/api/clipstyle":
                f = safe(parse_qs(u.query).get("path", [""])[0])
                sf = f.with_suffix(".clip.json") if f else None
                if not sf or not sf.is_file():
                    self.send_json({"error": "No saved settings for this clip"})
                else:
                    self.send_json(json.loads(sf.read_text()).get("style", {}))
            elif u.path == "/api/styles":
                self.send_json(CAPTION_PRESETS)
            elif u.path == "/api/fonts":
                self.send_json(fonts_cache())
            elif u.path == "/api/music":
                self.send_json({"songs": songs(), "folder": str(music_dir).replace(str(Path.home()), "~")})
            elif u.path == "/api/clips":
                self.send_json(clip_groups())
            elif u.path.startswith("/clips/") and (f := safe(u.path[7:])):
                self.send_file(f, "video/mp4", download="dl" in parse_qs(u.query))
            elif u.path.startswith("/thumb/") and (f := safe(u.path[7:])) and (t := thumb_for(f)):
                self.send_file(t, "image/jpeg")
            else:
                self.send_error(404)

        def do_POST(self):
            u, d = urlparse(self.path), self.body()
            if u.path == "/api/jobs":
                url = str(d.get("url", "")).strip()
                bulk = bool(d.get("bulk"))
                batch = bool(d.get("batch")) or bulk
                if bulk and d.get("move_dl") and d.get("campaign") in [c for c, _, _ in list_campaigns()]:
                    camp_c = {"id": d["campaign"]}
                    for f in sources(d["campaign"])["downloads"]:
                        file_into_campaign(f, camp_c)
                if batch and not d.get("campaign"):
                    return self.send_json({"error": "Pick a campaign first"}, 400)
                if not url and not batch:
                    return self.send_json({"error": "Paste a link first"}, 400)
                if not batch and not is_url(url) and not Path(url).expanduser().is_file():
                    return self.send_json({"error": "That's not a link or a file on this PC"}, 400)
                ollama = str(d.get("ollama") or "")
                camp = str(d.get("campaign") or "")
                if camp and camp not in [c for c, _, _ in list_campaigns()]:
                    return self.send_json({"error": "Unknown campaign"}, 400)
                choice = str(d.get("music") or "")
                music = ""
                if choice == "__random__":
                    music = str(music_dir)
                elif choice:
                    if choice not in songs():
                        return self.send_json({"error": "That song isn't in the folder any more"}, 400)
                    music = str(music_dir / choice)
                try:
                    music_vol = max(0.02, min(0.6, float(d.get("music_vol") or 0.15)))
                except ValueError:
                    music_vol = 0.15
                params = {
                    "url": url,
                    "k": (max(0, min(30, int(d.get("k") or 0))) if bulk else 0) if batch
                         else max(1, min(20, int(d.get("k") or 5))),
                    "batch": batch, "bulk": bulk,
                    "len": d.get("len") if d.get("len") in ("15-30", "20-60", "45-90") else "20-60",
                    "mode": d.get("mode") if d.get("mode") in ("blur", "crop", "none") else "blur",
                    "model": d.get("model") if d.get("model") in ("tiny", "base", "small", "medium", "tiny.en",
                                                              "base.en", "small.en", "medium.en") else "small.en",
                    "captions": bool(d.get("captions", True)),
                    "track": bool(d.get("track", True)),
                    "cut_pauses": bool(d.get("cut_pauses", True)),
                    "smart": {k: bool(v) for k, v in (d.get("smart") or {}).items()
                              if k in ("laughs", "laugh_tail", "chat", "words", "qa")},
                    "music": music, "music_vol": music_vol, "campaign": camp,
                    "style": d.get("style") if d.get("style") in ["auto", *CAPTION_PRESETS] else "auto",
                    "match": bool(d.get("match")),
                    "words": max(1, min(8, int(d.get("words") or 3))),
                    "cap": clean_cap(d.get("cap")),
                    "ollama": ollama if re.fullmatch(r"[\w.:/-]{1,60}", ollama) else "",
                }
                jid = uuid.uuid4().hex[:8]
                job = {"id": jid, "params": params, "status": "queued", "stage": "Waiting in queue",
                       "progress": 0, "title": ("Bulk folder scrape" if bulk else "Clipping all footage") if batch else ("" if is_url(url) else Path(url).name), "error": "", "made": 0, "k": params["k"],
                       "log": deque(maxlen=300), "proc": None}
                with lock:
                    jobs[jid] = job
                    order.append(jid)
                q.put(job)
                self.send_json({"id": jid})
            elif u.path == "/api/settings":
                st = load_settings()
                if d.get("ai") in ("", "ollama", "claude"):
                    st["ai"] = d["ai"]
                m = str(d.get("ollama_model") or "")
                if re.fullmatch(r"[\w.:/-]{0,60}", m):
                    st["ollama_model"] = m
                cm = str(d.get("claude_model") or "")
                if re.fullmatch(r"[\w.:-]{1,60}", cm):
                    st["claude_model"] = cm
                if d.get("claude_key"):
                    st["claude_key"] = str(d["claude_key"]).strip()[:300]
                if d.get("forget_key"):
                    st.pop("claude_key", None)
                if re.fullmatch(r"[\d:, \-]{3,120}", str(d.get("plan_windows") or "")):
                    st["plan_windows"] = d["plan_windows"]
                for k, lo, hi in (("plan_per_day", 1, 12), ("plan_gap", 0.5, 8)):
                    try:
                        if d.get(k) not in (None, ""):
                            st[k] = max(lo, min(hi, float(d[k]) if k == "plan_gap" else int(d[k])))
                    except ValueError:
                        pass
                if isinstance(d.get("my_platforms"), list):
                    st["my_platforms"] = [x for x in d["my_platforms"]
                                          if x in ("TikTok", "Instagram Reels", "YouTube Shorts", "X")]
                save_settings(st)
                self.send_json({"ok": True, "ai": ai_name()})
            elif u.path == "/api/open":
                p = str(d.get("path") or "")
                if not p or not allowed_open(p):
                    return self.send_json({"ok": False, "msg": "Can't open that folder"}, 400)
                try:
                    open_folder(p)
                except OSError as e:
                    return self.send_json({"ok": False, "msg": str(e)})
                self.send_json({"ok": True})
            elif u.path == "/api/mkdir":
                base = Path(str(d.get("path") or ""))
                name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(d.get("name") or "")).strip(" .")[:60]
                if not base.is_dir() or not name:
                    return self.send_json({"ok": False, "msg": "Pick a folder and type a name"})
                try:
                    (base / name).mkdir(exist_ok=True)
                except OSError as e:
                    return self.send_json({"ok": False, "msg": str(e)})
                self.send_json({"ok": True, "path": str((base / name).resolve())})
            elif u.path == "/api/projects/set":
                if any(j["status"] in ("running", "queued") for j in jobs.values()):
                    return self.send_json({"ok": False, "msg": "Wait for the running jobs to finish first"})
                reset = bool(d.get("reset"))
                new = None if reset else Path(str(d.get("path") or "")).expanduser()
                if new is not None:
                    if not new.is_dir():
                        return self.send_json({"ok": False, "msg": "That folder doesn't exist"})
                    try:
                        t = new / ".clipper-test"
                        t.write_text("ok")
                        t.unlink()
                    except OSError:
                        return self.send_json({"ok": False, "msg": "Clipper can't save into that folder"})
                    new = new.resolve()
                moved = 0
                if d.get("move"):                          # bring existing campaigns + free clips along
                    dest_c = new or Path(__file__).resolve().parent / "campaigns"
                    dest_f = (new / "Free clips") if new else Path.home() / "Clips"
                    try:
                        dest_c.mkdir(parents=True, exist_ok=True)
                        if CAMPAIGN_DIR.is_dir() and CAMPAIGN_DIR.resolve() != dest_c.resolve():
                            for ag in list(CAMPAIGN_DIR.iterdir()):
                                if not ag.is_dir() or ag.resolve() == dest_c.resolve():
                                    continue
                                for c in list(ag.iterdir()):
                                    if (c / "campaign.json").is_file() and not (dest_c / ag.name / c.name).exists():
                                        (dest_c / ag.name).mkdir(exist_ok=True)
                                        shutil.move(str(c), str(dest_c / ag.name / c.name))
                                        moved += 1
                        if out_root.is_dir() and out_root.resolve() != dest_f.resolve() \
                                and dest_f.resolve() not in out_root.resolve().parents:
                            dest_f.mkdir(parents=True, exist_ok=True)
                            for x in list(out_root.iterdir()):
                                if not (dest_f / x.name).exists() and x.resolve() != dest_f.resolve():
                                    shutil.move(str(x), str(dest_f / x.name))
                    except OSError as e:
                        return self.send_json({"ok": False, "msg": f"Moving stopped part-way: {e}"})
                st = load_settings()
                if new is None:
                    st.pop("projects_dir", None)
                else:
                    st["projects_dir"] = str(new)
                save_settings(st)
                self.send_json({"ok": True, "moved": moved})

                def restart_p():
                    time.sleep(0.8)
                    srv.server_close()
                    argv, skip = [], False
                    for a in sys.argv:                 # drop -o so the new folder is used
                        if skip:
                            skip = False
                            continue
                        if a in ("-o", "--out-dir"):
                            skip = True
                            continue
                        argv.append(a)
                    if WINDOWS:
                        subprocess.Popen([sys.executable] + argv)
                        os._exit(0)
                    os.execv(sys.executable, [sys.executable] + argv)
                threading.Thread(target=restart_p, daemon=True).start()
            elif u.path == "/api/update":
                if any(j["status"] in ("running", "queued") for j in jobs.values()):
                    return self.send_json({"ok": False, "msg": "Wait for the running jobs to finish first"})
                repo = Path(__file__).resolve().parent
                r = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"], capture_output=True, text=True)
                if r.returncode != 0:
                    return self.send_json({"ok": False, "msg": "git pull failed: " + (r.stderr or r.stdout)[-300:]})
                self.send_json({"ok": True, "msg": (r.stdout.strip().splitlines() or ["Updated"])[-1]})

                def restart():
                    time.sleep(0.8)
                    srv.server_close()
                    if WINDOWS:                      # execv is unreliable on Windows: start a new copy
                        subprocess.Popen([sys.executable] + sys.argv)
                        os._exit(0)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                threading.Thread(target=restart, daemon=True).start()
            elif u.path == "/api/results/refresh":
                with res_lock:
                    ok, failed = refresh_post_stats(d.get("ids") or None)
                out = results_summary()
                out["refreshed"], out["failed"] = ok, failed
                self.send_json(out)
            elif u.path in ("/api/results/add", "/api/results/update", "/api/results/delete"):
                with res_lock:
                    data = load_results()
                    posts = data.setdefault("posts", [])
                    num = lambda v: (int(str(v).replace(",", "").strip()) if str(v or "").replace(",", "").strip().isdigit()
                                     else None)
                    if u.path.endswith("add"):
                        rel = str(d.get("clip") or "")
                        f = safe(rel)
                        side, camp = {}, {}
                        if f:
                            try:
                                side = json.loads(f.with_suffix(".clip.json").read_text())
                            except (OSError, ValueError):
                                side = {}
                        cid = rel.split("/")[0].replace("~", "/") if "/" in rel else ""
                        if cid and cid != "_":
                            try:
                                camp = json.loads((campaign_dir(cid) / "campaign.json").read_text())
                            except (OSError, ValueError):
                                camp = {}
                        plat = d.get("platform") if d.get("platform") in ("TikTok", "Instagram Reels", "YouTube Shorts", "X") else "TikTok"
                        url = str(d.get("url") or "").strip()[:400]
                        same = [x for x in posts if x.get("clip") == rel]
                        # already logged on this platform (e.g. via the platform button)? update that one instead
                        dup = next((x for x in same if x.get("platform") == plat and
                                    (not x.get("url") or not url or x.get("url") == url)), None)
                        if dup:
                            if url:
                                dup["url"] = url
                            for k in ("views", "likes"):
                                if num(d.get(k)) is not None:
                                    dup[k] = num(d.get(k))
                            dup["updated"] = time.time()
                            save_results(data)
                            return self.send_json(results_summary())
                        if len(same) >= 5:
                            return self.send_json({"error": "This clip has already been posted 5 times - campaigns don't allow more"}, 400)
                        meta = dict(side.get("meta") or {})
                        meta["style"] = (side.get("style") or {}).get("preset")
                        posts.append({"id": uuid.uuid4().hex[:10], "clip": rel,
                                      "clip_name": Path(rel).stem, "campaign": cid if cid != "_" else "",
                                      "campaign_name": camp.get("name", ""), "rate": camp.get("rate"),
                                      "platform": plat, "url": url, "posted": time.time(),
                                      "views": num(d.get("views")), "likes": num(d.get("likes")),
                                      "updated": time.time(), "meta": meta})
                    else:
                        x = next((x for x in posts if x.get("id") == d.get("id")), None)
                        if not x:
                            return self.send_json({"error": "Not found"}, 404)
                        if u.path.endswith("delete"):
                            posts.remove(x)
                        else:
                            for k in ("views", "likes"):
                                if str(d.get(k, "")).strip() != "":
                                    x[k] = num(d.get(k))
                            if d.get("url") is not None:
                                x["url"] = str(d["url"]).strip()[:400]
                            x["updated"] = time.time()
                    save_results(data)
                self.send_json(results_summary())
            elif u.path == "/api/gpusetup":
                jid = uuid.uuid4().hex[:8]
                job = {"id": jid, "params": {"url": "", "setup": "gpu"}, "status": "queued", "stage": "Waiting in queue",
                       "progress": 0, "title": "Setting up the graphics card (CUDA libraries)", "error": "",
                       "made": 0, "k": 1, "log": deque(maxlen=300), "proc": None}
                with lock:
                    jobs[jid] = job
                    order.append(jid)
                q.put(job)
                self.send_json({"id": jid})
            elif u.path == "/api/aitest":
                name = ai_name()
                if not name:
                    st = load_settings()
                    msg = ("Paste your API key first, then Save" if st.get("ai") == "claude"
                           else "Type the Ollama model name first, then Save" if st.get("ai") == "ollama"
                           else "AI is switched off")
                    return self.send_json({"ok": False, "msg": msg})
                r = ai_json('Reply with exactly this JSON: {"ok": true}', timeout=120)
                self.send_json({"ok": bool(r and r.get("ok")), "msg": f"{name} answered ✓" if r
                                else f"{name} didn't answer - " + ("check the API key and model, and that you have credit"
                                      if name.startswith("Claude") else "is Ollama running and the model pulled?")})
            elif u.path == "/api/brief":
                text = str(d.get("text") or "")
                if len(text.strip()) < 30:
                    return self.send_json({"error": "Paste the campaign rules first"}, 400)
                model = str(d.get("ollama") or "")
                model = model if re.fullmatch(r"[\w.:/-]{1,60}", model) else None
                self.send_json(read_brief(text[:20000], model))
            elif u.path == "/api/savecampaign":
                c = clean_campaign(d.get("campaign") or {})
                cid = str(d.get("id") or "")
                if cid and cid not in [x for x, _, _ in list_campaigns()]:
                    cid = ""
                new_id = save_campaign(c, str(d.get("brief") or "")[:20000] or None, cid or None)
                lg = d.get("logo") if isinstance(d.get("logo"), dict) else None
                if lg and str(lg.get("data", "")).startswith("data:image/"):
                    import base64
                    try:
                        head, b64 = str(lg["data"]).split(",", 1)
                        ext = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp"}.get(
                            head.split("/")[1].split(";")[0], ".png")
                        cdir = campaign_dir(new_id)
                        tmp_logo = cdir / ("logo-upload" + ext)
                        tmp_logo.write_bytes(base64.b64decode(b64)[:12_000_000])
                        if ext == ".png":
                            tmp_logo.replace(cdir / "logo.png")
                        else:                          # turn jpg/webp into png
                            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(tmp_logo),
                                            str(cdir / "logo.png")], capture_output=True)
                            tmp_logo.unlink(missing_ok=True)
                        if (cdir / "logo.png").is_file():
                            c["logo"] = "logo.png"
                            save_campaign(c, None, new_id)
                    except (ValueError, OSError):
                        pass
                files = [str(x) for x in (d.get("files") or []) if isinstance(x, str)][:200]
                moved = import_downloads(new_id, files) if files else []
                self.send_json({"id": new_id, "moved": moved})
            elif u.path == "/api/trim":
                rel = str(d.get("path", ""))
                f = safe(rel)
                sf = f.with_suffix(".clip.json") if f else None
                if not sf or not sf.is_file():
                    return self.send_json({"error": "Can't trim that clip"}, 400)
                sd = json.loads(sf.read_text())
                try:
                    ns, ne = float(d["start"]), float(d["end"])
                except (KeyError, TypeError, ValueError):
                    return self.send_json({"error": "Bad times"}, 400)
                if ne - ns < 1 or ns < 0:
                    return self.send_json({"error": "The clip must be at least a second long"}, 400)
                with lock:
                    if any(j.get("clip") == rel and j["status"] in ("queued", "running") for j in jobs.values()):
                        return self.send_json({"error": "That clip is already being changed"}, 400)
                params = {"restyle": str(f), "url": f.name, "style": "highlight", "match": False, "words": 3,
                          "cap": {}, "trim": [ns, ne]}
                jid = uuid.uuid4().hex[:8]
                job = {"id": jid, "params": params, "status": "queued", "stage": "Waiting in queue",
                       "progress": 0, "title": f"✂️ Trim: {f.stem}", "error": "", "made": 0, "k": 1,
                       "clip": rel, "dur": ne - ns, "log": deque(maxlen=300), "proc": None}
                with lock:
                    jobs[jid] = job
                    order.append(jid)
                q.put(job)
                self.send_json({"id": jid})
            elif u.path == "/api/variations":
                rel = str(d.get("path", ""))
                f = safe(rel)
                if not f or not f.with_suffix(".clip.json").is_file():
                    return self.send_json({"error": "Can't make variations of that clip"}, 400)
                params = {"restyle": str(f), "url": f.name, "style": "highlight", "match": False, "words": 3,
                          "cap": {}, "variations": True}
                jid = uuid.uuid4().hex[:8]
                job = {"id": jid, "params": params, "status": "queued", "stage": "Waiting in queue",
                       "progress": 0, "title": f"🎲 Variations: {f.stem}", "error": "", "made": 0, "k": 3,
                       "clip": rel, "log": deque(maxlen=300), "proc": None}
                with lock:
                    jobs[jid] = job
                    order.append(jid)
                q.put(job)
                self.send_json({"id": jid})
            elif u.path == "/api/clipinfo":
                f = safe(str(d.get("path", "")))
                sf = f.with_suffix(".clip.json") if f else None
                if not sf or not sf.is_file():
                    return self.send_json({"error": "no info"}, 400)
                sd = json.loads(sf.read_text())
                src = sd.get("source", "")
                dur = None if is_url(src) else (duration_of(Path(src)) if Path(src).is_file() else None)
                self.send_json({"start": sd.get("start"), "end": sd.get("end"), "source_len": dur,
                                "source_ok": is_url(src) or Path(src).is_file(),
                                "words": [w for w in sd.get("words", [])
                                          if sd.get("start", 0) - 15 <= w[0] <= sd.get("end", 0) + 15][:600]})
            elif u.path == "/api/restyle":
                rel = str(d.get("path", ""))
                f = safe(rel)
                if not f or not f.with_suffix(".clip.json").is_file():
                    return self.send_json({"error": "Can't restyle that clip"}, 400)
                with lock:
                    if any(j.get("clip") == rel and j["status"] in ("queued", "running") for j in jobs.values()):
                        return self.send_json({"error": "That clip is already being restyled"}, 400)
                try:
                    sd = json.loads(f.with_suffix(".clip.json").read_text())
                    dur = float(sd["end"]) - float(sd["start"])
                except (ValueError, KeyError, TypeError):
                    dur = 0
                params = {"restyle": str(f), "url": f.name,
                          "style": d.get("style") if d.get("style") in ["auto", *CAPTION_PRESETS] else "highlight",
                          "match": bool(d.get("match")),
                          "words": max(1, min(8, int(d.get("words") or 3))),
                          "cap": clean_cap(d.get("cap"))}
                jid = uuid.uuid4().hex[:8]
                job = {"id": jid, "params": params, "status": "queued", "stage": "Waiting in queue",
                       "progress": 0, "title": f"🎨 Restyle: {f.stem}", "error": "", "made": 0, "k": 1,
                       "clip": rel, "dur": dur, "log": deque(maxlen=300), "proc": None}
                with lock:
                    jobs[jid] = job
                    order.append(jid)
                q.put(job)
                self.send_json({"id": jid})
            elif u.path == "/api/cancel":
                job = jobs.get(d.get("id"))
                if job and job["status"] in ("queued", "running"):
                    was_queued = job["status"] == "queued"
                    job["status"], job["stage"] = "cancelled", "Cancelled"
                    if was_queued:
                        add_history(job)
                    if job["proc"]:
                        try:
                            if WINDOWS:              # stop the job and its ffmpeg children
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(job["proc"].pid)],
                                               capture_output=True)
                            else:
                                os.killpg(job["proc"].pid, signal.SIGTERM)
                        except (ProcessLookupError, OSError):
                            pass
                self.send_json({"ok": True})
            elif u.path == "/api/clear":
                with lock:
                    job = jobs.get(d.get("id"))
                    if job and job["status"] not in ("queued", "running"):
                        jobs.pop(job["id"])
                        order.remove(job["id"])
                self.send_json({"ok": True})
            elif u.path == "/api/delete":
                delete_clip(str(d.get("path", "")))
                self.send_json({"ok": True})
            elif u.path == "/api/deletemany":
                paths = [str(x) for x in (d.get("paths") or []) if isinstance(x, str)][:500]
                n = sum(1 for x in paths if delete_clip(x))
                self.send_json({"ok": True, "deleted": n})
            elif u.path == "/api/history/clear":
                with hist_lock:
                    hist_file.write_text("[]")
                self.send_json({"ok": True})
            else:
                self.send_error(404)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        ip = "localhost"
    ts = ""
    if shutil.which("tailscale"):
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            ts = f"  anywhere:    http://{r.stdout.split()[0]}:{args.port}   (phone on Tailscale)\n"
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), H)
    if getattr(args, "open", False):
        import webbrowser
        threading.Timer(1.0, webbrowser.open, [f"http://localhost:{args.port}"]).start()
    print(f"Clipper is running:\n"
          f"  on this PC:  http://localhost:{args.port}\n"
          f"  home Wi-Fi:  http://{ip}:{args.port}\n{ts}"
          f"  clips go to: {out_root}\n"
          f"Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


def main():
    if WINDOWS and not sys.flags.utf8_mode:      # Windows: run in UTF-8 mode (emoji, captions, briefs)
        os.environ["PYTHONUTF8"] = "1"
        sys.exit(subprocess.call([sys.executable, "-X", "utf8"] + sys.argv))
    p = argparse.ArgumentParser(description="Clip YouTube videos into vertical shorts.")
    p.add_argument("source", nargs="?", help="YouTube URL or local video file")
    p.add_argument("--ui", action="store_true", help="start the web interface")
    p.add_argument("--port", type=int, default=8765, help="web interface port (default 8765)")
    p.add_argument("--open", action="store_true", help="with --ui: open the page in the browser once it's running")
    p.add_argument("--campaign", help="apply a campaign's rules (file name in campaigns/, without .json)")
    p.add_argument("--audio", action="store_true",
                   help="just rip the audio (works with links, playlists and files; -s/-e optional)")
    p.add_argument("--audio-format", choices=["mp3", "wav", "flac", "m4a"], default="mp3")
    p.add_argument("--audio-dir", default="~/Music/rips", help="where ripped audio goes")
    p.add_argument("-a", "--auto", action="store_true", help="find the best moments automatically")
    p.add_argument("-k", type=int, default=None,
                   help="auto: how many clips (default 5; 0 = decide from video length)")
    p.add_argument("--batch", action="store_true",
                   help="clip every video in a campaign's source folder (or in the folder you give)")
    p.add_argument("--redo", action="store_true", help="batch: also redo videos that already have clips")
    p.add_argument("--mix", type=int, default=None,
                   help="bulk: how many clips to mix with silent b-roll (default 2 when music is banned)")
    p.add_argument("--bulk", action="store_true",
                   help="bulk folder scrape: scan every video, keep the best clips overall")
    p.add_argument("--len", default="20-60", help="auto: clip length range in seconds (default 20-60)")
    p.add_argument("--target", type=float, help="auto: preferred clip length (default: middle of --len)")
    p.add_argument("--ollama", metavar="MODEL", help="auto: use a local ollama model to pick + title")
    p.add_argument("--dry-run", action="store_true", help="auto: only list picks, don't render")
    p.add_argument("-s", "--start", help="start time, e.g. 1:23")
    p.add_argument("-e", "--end", help="end time, e.g. 2:05")
    p.add_argument("-n", "--name", default="clip", help="output file name")
    p.add_argument("-l", "--list", help="text file of 'start end name' lines")
    p.add_argument("--no-track", dest="track", action="store_false",
                   help="Zoom style: don't follow faces, just keep the middle (e.g. more than one person in shot)")
    p.add_argument("-m", "--mode", choices=["blur", "crop", "none"], default="blur",
                   help="vertical style (default: blur)")
    p.add_argument("-c", "--captions", action="store_true", help="burn in captions (auto: on by default)")
    p.add_argument("--no-captions", action="store_true", help="auto: turn captions off")
    p.add_argument("--model", default="small", help="whisper model: tiny/base/small/medium (default small)")
    p.add_argument("--words", type=int, default=None, help="words per caption line")
    p.add_argument("--style", choices=["auto"] + list(CAPTION_PRESETS), default="auto",
                   help="caption look (default: auto = best for each clip)")
    p.add_argument("--restyle", metavar="CLIP.mp4", help="re-render a clip with new caption settings")
    p.add_argument("--variations", metavar="CLIP.mp4", help="make 3 different versions of a clip")
    p.add_argument("--no-laughs", dest="laughs", action="store_false", help="don't listen for laughter")
    p.add_argument("--no-laugh-tail", dest="laugh_tail", action="store_false",
                   help="don't run clips on to include a laugh after the punchline")
    p.add_argument("--no-chat", dest="chat", action="store_false", help="ignore stream chat replays")
    p.add_argument("--no-smart-words", dest="smart_words", action="store_false",
                   help="don't score funny lines / opinions / filler")
    p.add_argument("--no-interview-endings", dest="qa", action="store_false",
                   help="allow clips to end on a question or mid-answer")
    p.add_argument("--keep-pauses", dest="cut_pauses", action="store_false",
                   help="don't cut silent pauses and um/uh out of clips")
    p.add_argument("--trim-start", type=float, help="with --restyle: new start (seconds in the source video)")
    p.add_argument("--trim-end", type=float, help="with --restyle: new end (seconds in the source video)")
    p.add_argument("--match-colors", action="store_true",
                   help="match caption colours to the video")
    p.add_argument("--cap-font", help="caption font name (see --fonts)")
    p.add_argument("--cap-size", type=int, help="caption font size (40-160)")
    p.add_argument("--cap-color", help="caption text colour, e.g. #FFFFFF")
    p.add_argument("--cap-highlight", help="active-word colour (empty = no highlight)")
    p.add_argument("--cap-outline", help="outline / glow / box colour")
    p.add_argument("--cap-pos", choices=list(POSITIONS), help="caption position")
    p.add_argument("--cap-caps", choices=["on", "off"], help="ALL CAPS on/off")
    p.add_argument("--cap-pop", choices=["on", "off"], help="pop-in animation on/off")
    p.add_argument("--cap-json", help=argparse.SUPPRESS)
    p.add_argument("--fonts", action="store_true", help="list fonts you can use")
    p.add_argument("--phone-setup", action="store_true",
                   help="run clipper in the background from boot (Linux) and set up the secure Tailscale link")
    p.add_argument("--check", metavar="CAMPAIGN", help="check a campaign's settings against its brief, and its clips")
    p.add_argument("--gpu", action="store_true", help="show whether the graphics card can be used")
    p.add_argument("--speedtest", metavar="VIDEO", help="compare CPU vs graphics card on a video's first minute")
    p.add_argument("--music", help="background music: a file, or a folder (random track per clip)")
    p.add_argument("--music-vol", type=float, default=0.15, help="music volume 0-1 (default 0.15)")
    p.add_argument("--music-start", type=float, default=0.0, help="start the music N seconds in")
    p.add_argument("-o", "--out-dir", type=Path, default=None,
                   help="where clips go (default ~/Clips, or the campaign's clips folder)")
    args = p.parse_args()
    import signal                                     # Cancel in the web page: clean up, keep old clip
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    need("ffmpeg")
    if args.gpu:
        print(gpu_status()["msg"])
        return
    if args.speedtest:
        speed_test(args.speedtest, args.model if args.model.endswith(".en") else "small.en")
        return
    if args.check:
        c = load_campaign(args.check)
        print(f"Campaign: {c.get('name')}  ({c['id']})")
        res = campaign_checks(c["id"])
        for lvl, msg in res:
            print(f"  {'⚠' if lvl != 'info' else 'ℹ'} {msg}")
        if not [r for r in res if r[0] != "info"]:
            print("  ✅ settings match the brief")
        clips = sorted((campaign_dir(c["id"]) / "clips").rglob("*.mp4"))
        for f in clips:
            chk = clip_checks(f, c)
            st, why = rate_clip(f, c, chk)
            print(f"  {st:.1f}★ {f.relative_to(campaign_dir(c['id']) / 'clips')}  - {why}")
            for w in chk:
                print(f"       ⚠ {w}")
        return
    if args.phone_setup:
        return phone_setup(args.port)
    if args.fonts:
        print("\n".join(list_fonts()))
        print(f"\nAdd more: put .ttf/.otf files in {FONTS_DIR}")
        return
    args.cap = {"font": args.cap_font, "size": args.cap_size, "text": args.cap_color,
                "highlight": args.cap_highlight, "outline": args.cap_outline, "pos": args.cap_pos,
                "upper": None if args.cap_caps is None else args.cap_caps == "on",
                "pop": None if args.cap_pop is None else args.cap_pop == "on"}
    if args.cap_json:
        try:
            args.cap.update({k: v for k, v in json.loads(args.cap_json).items() if v is not None})
        except ValueError:
            die("bad --cap-json")
    if args.out_dir:
        args.out_dir = args.out_dir.expanduser()

    if args.restyle:
        return restyle_mode(args)
    if args.variations:
        return variations_mode(args)
    if args.ui:
        args.out_dir = args.out_dir or OUT_DIR
        args.out_dir.mkdir(parents=True, exist_ok=True)
        return serve_ui(args)
    if not args.source and not args.batch and not args.bulk:
        die("give a YouTube link or file (or use --ui, --batch or --fonts)")
    if args.audio:
        return audio_mode(args)
    if args.batch or args.bulk:
        args.auto = True
        if args.k is None:
            args.k = 0                         # decide per video
    if args.k is None:
        args.k = 5
    if args.auto:
        args.captions = not args.no_captions
    args.campaign_data = None
    if args.campaign:
        args.campaign_data = load_campaign(args.campaign)
        apply_campaign(args, args.campaign_data)
    args.out_dir = args.out_dir or OUT_DIR
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.bulk:
        bulk_mode(args)
    elif args.batch:
        batch_mode(args)
    elif args.auto:
        auto_mode(args)
    elif args.list:
        list_mode(args)
    elif args.start and args.end:
        make_clip(args.source, to_seconds(args.start), to_seconds(args.end),
                  args.out_dir / f"{slug(args.name)}.mp4", args)
    else:
        die("use --auto, or -s START -e END, or --list FILE")


if __name__ == "__main__":
    main()
