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


def slug(name: str, n=60) -> str:
    return (re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:n].strip("-")) or "clip"


def run(cmd):
    if subprocess.run(cmd).returncode != 0:
        die("command failed: " + " ".join(map(str, cmd[:4])) + " ...")


def need(tool):
    if not shutil.which(tool):
        die(f"'{tool}' not found - install it with: sudo pacman -S {tool}")


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
    return path, title


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
        run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
             "-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-c:a", "aac", "-b:a", "192k", str(out)])
    if not out.exists():
        die("couldn't get the video segment")
    return out


# ======================================================================
# transcription
# ======================================================================
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

    def attempt(device):
        import os
        model = WhisperModel(model_size, device=device, compute_type="int8",
                             cpu_threads=os.cpu_count() or 4)
        segs, info = model.transcribe(audio, word_timestamps=True, vad_filter=True,
                                      language="en" if model_size.endswith(".en") else None)
        res = []
        for s in segs:
            res.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                        "words": [(w.start, w.end, w.word.strip()) for w in (s.words or [])]})
            print(f"\r  {short_ts(s.end)} / {short_ts(info.duration)}", end="", flush=True)
        print()
        return res

    try:
        out = attempt("auto")
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


def loudness_curve(path: Path):
    """Loudness (z-scored dB) every HOP seconds, via ffmpeg -> numpy."""
    import numpy as np
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


def build_candidates(segs, min_len, max_len, target):
    """Every sentence-aligned window between min_len and max_len seconds."""
    cands = []
    ends_sentence = lambda t: bool(re.search(r"[.!?…][\"')\]]*\s*$", t.strip()))
    stretch = min(10.0, max_len * 0.25)            # allowed overrun to let a sentence finish
    for i in range(len(segs)):
        best, best_end = None, None
        for j in range(i, len(segs)):
            if j > i and segs[j]["start"] - segs[j - 1]["end"] > 4.0:
                break                              # long silence - don't bridge it
            dur = segs[j]["end"] - segs[i]["start"]
            if dur > max_len + stretch:
                break
            if dur < min_len:
                continue
            if ends_sentence(segs[j]["text"]):
                over = max(0.0, dur - max_len) * 3     # overrunning is allowed but discouraged
                if best_end is None or abs(dur - target) + over < best_end[2]:
                    best_end = (j, dur, abs(dur - target) + over)
            if dur <= max_len and (best is None or abs(dur - target) < abs(best[1] - target)):
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
        end = max(end, e + 0.35)          # whisper ends words a touch early
    return max(0.0, start), end


def score_candidates(cands, segs, loud):
    import numpy as np
    feats = []
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
        ])
    F = np.array(feats)
    Z = (F - F.mean(axis=0)) / (F.std(axis=0) + 1e-6)
    weights = np.array([1.0, 0.8, 1.2, 0.9, 0.5, 0.6, 1.0, -0.8])
    for c, z in zip(cands, Z @ weights):
        c["score"] = float(z)
    return sorted(cands, key=lambda c: c["score"], reverse=True)


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
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                             "format": "json"}).encode(),
            headers={"Content-Type": "application/json"})
        print(f"  asking ollama ({model}) to pick...")
        resp = json.loads(urllib.request.urlopen(req, timeout=600).read())
        picks = json.loads(resp["response"])["picks"]
        out = []
        for p in picks:
            idx = int(p["id"])
            if 0 <= idx < len(pool) and pool[idx] not in out:
                pool[idx]["title"] = str(p.get("title", "")).strip()
                out.append(pool[idx])
        out = pick(out, k, gap=0)               # drop overlaps
        if out:
            return out
        print("  ! ollama gave no usable picks, using built-in scoring")
    except Exception as e:
        print(f"  ! ollama not available ({e}), using built-in scoring")
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
    if FONTS_DIR.is_dir():
        for f in FONTS_DIR.iterdir():
            if f.suffix.lower() in (".ttf", ".otf"):
                r = subprocess.run(["fc-scan", "--format", "%{family}\n", str(f)],
                                   capture_output=True, text=True)
                fams |= {x.split(",")[0].strip() for x in r.stdout.splitlines() if x.strip()}
    return sorted(fams, key=str.lower)


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


def render(raw: Path, out: Path, mode: str, subs, music=None, music_vol=0.15, music_start=0.0):
    if mode == "crop":      # zoom to fill, cut the sides off
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
        fd = f":fontsdir='{FONTS_DIR}'" if FONTS_DIR.is_dir() else ""
        v += f";[v0]subtitles='{subs}'{fd},{vfade}[v]"
    else:
        v += f";[v0]{vfade}[v]"

    inputs = ["-i", str(raw)]
    amap = "0:a?"
    afade = f"afade=t=in:d=0.06,afade=t=out:st={max(d - 0.45, 0):.2f}:d=0.45"
    if not music and has_audio(raw):
        v += f";[0:a]{afade}[a]"
        amap = "[a]"
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
    run(["ffmpeg", "-loglevel", "error", "-stats", "-y", *inputs,
         "-filter_complex", v, "-map", "[v]", "-map", amap,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", "-f", "mp4", str(out)])


def make_clip(src, start, end, out: Path, args, words=None):
    """words given -> reuse transcript; else transcribe the clip if captions wanted."""
    if end <= start:
        die(f"end must be after start ({out.name})")
    print(f"> {out.stem}: {short_ts(start)} -> {short_ts(end)}  ({end - start:.0f}s)")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        raw = fetch_segment(src, start, end, tmp)
        win = []                                  # words in source time
        if words is not None:
            win = [tuple(w) for w in words if w[0] >= start - 0.05 and w[1] <= end + 0.05]
        elif args.captions:
            segs = transcribe(raw, args.model, cache=False)
            win = [(w[0] + start, w[1] + start, w[2]) for s in segs for w in s["words"]]
        need_video = getattr(args, "style", None) == "auto" or getattr(args, "match_colors", False)
        style = resolve_style(args, raw if need_video else None, win, end - start)
        subs = build_ass(win if args.captions else [], start, tmp, style,
                         overlay=getattr(args, "on_screen", None), duration=end - start)
        music = pick_music(getattr(args, "music", None))
        if music:
            print(f"  music: {music.name}")
        # render next to the clip, then swap it in: the old clip stays playable
        # (and safe) until the new one is completely finished
        part = out.with_name(out.name + ".part")
        try:
            render(raw, part, args.mode, subs, music,
                   getattr(args, "music_vol", 0.15), getattr(args, "music_start", 0.0))
            os.replace(part, out)
        finally:
            part.unlink(missing_ok=True)
    side = {"source": str(src), "start": start, "end": end, "words": win, "style": style,
            "mode": args.mode, "captions": bool(args.captions),
            "music": str(music) if music else None, "music_vol": getattr(args, "music_vol", 0.15),
            "music_start": getattr(args, "music_start", 0.0), "on_screen": getattr(args, "on_screen", None)}
    out.with_suffix(".clip.json").write_text(json.dumps(side))
    print(f"  saved {out}")


def restyle_mode(args):
    """Re-render one clip with a new caption style (uses the saved .clip.json)."""
    clip = Path(args.restyle).expanduser()
    side_f = clip.with_suffix(".clip.json")
    if not side_f.is_file():
        die(f"no saved settings for {clip.name} - it was made before Restyle existed, make it again")
    sd = json.loads(side_f.read_text())
    args.mode = sd.get("mode", args.mode)
    args.captions = True
    args.music = sd.get("music")
    args.music_vol = sd.get("music_vol", 0.15)
    args.music_start = sd.get("music_start", 0.0)
    args.on_screen = sd.get("on_screen")
    src = sd["source"]
    if not is_url(src) and not Path(src).is_file():
        die(f"the original video isn't there any more: {src}")
    print(f"> restyling {clip.name}")
    make_clip(src, sd["start"], sd["end"], clip, args, [tuple(w) for w in sd.get("words", [])])


# ======================================================================
# campaigns  (rules for each clipping campaign live in campaigns/*.json)
# ======================================================================
CAMPAIGN_DIR = Path(__file__).resolve().parent / "campaigns"
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


def ollama_caption(c, text, model):
    prompt = (
        f"Write ONE short social media caption (max 10 words) for a TikTok clip.\n"
        f"Rules: {c.get('caption_rules', '')}\n"
        f"Only describe what is actually said or shown in the transcript. "
        f"No made-up facts, stats or dates. No hashtags, no emojis, no clickbait.\n"
        f"Style examples: {' | '.join(c.get('example_captions', []))}\n"
        f'Reply ONLY with JSON: {{"caption":"..."}}\n\nTranscript of the clip:\n{text[:1500]}')
    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                             "format": "json"}).encode(),
            headers={"Content-Type": "application/json"})
        cap = json.loads(json.loads(urllib.request.urlopen(req, timeout=300).read())["response"])["caption"]
        cap = re.sub(r"#\w+", "", str(cap)).strip().strip('"')
        return cap if 3 < len(cap) < 120 else None
    except Exception:
        return None


def make_caption(c, text, n, ollama=None):
    line = ollama_caption(c, text, ollama) if ollama else None
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
                   "hashtags", "max_extra_hashtags", "caption_rules", "example_captions", "checklist"]


def _clean_line(t):
    return re.sub(r"\s+", " ", re.sub(r"^[\s•\-–*✔✅❌⚠️️\d.)]+", "", t)).strip()


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
    c["disclosure"] = "#Ad" if re.search(r"#ad\b|#advertisement|#sponsored|ftc|disclos", low) else ""

    # required line (e.g. "Use one of these lines:" -> next line)
    req = []
    for i, l in enumerate(lines):
        if re.search(r"(use one of these lines|must (include|say|mention) (this|the following)|required line)", l, re.I):
            for nxt in lines[i + 1:i + 4]:
                t = _clean_line(nxt)
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
    chk.append("Submit the post link in Whop within 30 minutes of posting")
    c["checklist"] = chk
    c["hashtags"], c["max_extra_hashtags"] = [], 3
    return c


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
        '"example_captions": example captions without hashtags/mentions, "checklist": short rules to check before posting}\n'
        "Reply ONLY with the JSON.\n\nBRIEF:\n" + text[:8000])
    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"}).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(json.loads(urllib.request.urlopen(req, timeout=300).read())["response"])
    except Exception as e:
        print(f"  ! ollama couldn't read the brief ({e})")
        return None


def read_brief(text, model=None):
    c = parse_brief(text)
    ai = ollama_brief(text, model) if model else None
    if isinstance(ai, dict):
        for k in CAMPAIGN_FIELDS:
            v = ai.get(k)
            if v not in (None, "", []) and (k not in ("music",) or isinstance(v, bool)):
                c[k] = v
        c["_via"] = "ollama"
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
    out["example_captions"] = lst(d.get("example_captions"), 120, 12)
    out["checklist"] = lst(d.get("checklist"), 200, 30)
    return out


DOWNLOADS = Path.home() / "Downloads"


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
        dest = CAMPAIGN_DIR / slug(c["agency"]).lower() / slug(c["name"], 40).lower()
    for sub in ("source", "clips"):
        (dest / sub).mkdir(parents=True, exist_ok=True)
    (dest / "campaign.json").write_text(json.dumps(c, indent=2, ensure_ascii=False))
    if brief:
        (dest / "brief.txt").write_text(brief)
    return dest.relative_to(CAMPAIGN_DIR).as_posix()


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
    fps = 1 / HOP
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-vf",
                        f"fps={fps:g},scale=64:36,format=gray", "-f", "rawvideo", "-"],
                       capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    n = a.size // (64 * 36)
    if n < 2:
        return np.zeros(1), []
    f = a[: n * 64 * 36].reshape(n, 36, 64).astype(float)
    diff = np.concatenate([[0.0], np.abs(np.diff(f, axis=0)).mean(axis=(1, 2)) / 255])
    thr = diff.mean() + 2.5 * diff.std()
    cuts = [i * HOP for i in range(1, n) if diff[i] > max(thr, 0.08)]
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
    segs = [s for s in transcribe(src, args.model) if s["text"]]
    words = sum(len(s["text"].split()) for s in segs)
    if words < 12:
        cands = visual_moments(src, lo, hi, target)
        if not cands:
            die("video too short to use")
        print(f"  no real speech - picking by movement and sound ({len(cands)} options)")
        return segs, cands
    loud = loudness_curve(src)
    cands = build_candidates(segs, lo, hi, target)
    if not cands:
        die(f"no {args.len}s windows found - try a different --len")
    cands = score_candidates(cands, segs, loud)
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
    picks = ollama_pick(cands, k, args.ollama) if args.ollama else None
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
        make_clip(src, c["start"], c["end"], folder / f"{c['name']}.mp4", args, words)
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
    out = args.out_dir / f"bulk-scrape-{_t.strftime('%d%b-%H%M')}"
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# bulk scrape of {folder}", "# best clips across all videos", ""]
    print(f"\n> top {len(picks)} moments:")
    for n, cd in enumerate(picks, 1):
        cd["name"] = f"{n:02d}-{slug(cd['vid'], 18)}-{slug(cd['title'], 28)}"
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
        make_clip(cd["src"], cd["start"], cd["end"], out / f"{cd['name']}.mp4", args, words_of[cd["src"]])
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
UI_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0e0f13">
<title>Clipper</title>
<style>
:root{--bg:#0e0f13;--card:#181a21;--line:#262934;--text:#eceef3;--dim:#8b90a0;--acc:#ff3d6e;--acc2:#ffb03d;--ok:#3ddc97}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
     padding:16px 14px 60px;max-width:760px;margin:auto}
h1{font-size:26px;letter-spacing:-.5px;display:flex;align-items:center;gap:8px;margin-bottom:14px}
h1 span{background:linear-gradient(90deg,var(--acc),var(--acc2));-webkit-background-clip:text;color:transparent}
h2{font-size:13px;text-transform:uppercase;letter-spacing:1.2px;color:var(--dim);margin:26px 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}
input[type=text]{width:100%;background:#0b0c10;border:1px solid var(--line);color:var(--text);
     border-radius:12px;padding:14px;font-size:16px;outline:none}
input[type=text]:focus{border-color:var(--acc)}
.row{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.field{flex:1;min-width:140px}
.field label{display:block;font-size:12px;color:var(--dim);margin-bottom:5px}
.seg{display:flex;background:#0b0c10;border:1px solid var(--line);border-radius:10px;padding:3px}
.seg button{flex:1;background:none;border:0;color:var(--dim);padding:8px 4px;border-radius:8px;font:inherit;font-size:13px}
.seg button.on{background:var(--line);color:var(--text)}
input[type=range]{width:100%;accent-color:var(--acc)}
select{width:100%;background:#0b0c10;border:1px solid var(--line);color:var(--text);border-radius:10px;
       padding:10px;font:inherit;font-size:14px}
.toggle{display:flex;align-items:center;gap:8px;font-size:14px;margin-top:14px}
.toggle input{width:20px;height:20px;accent-color:var(--acc)}
.go{width:100%;margin-top:16px;padding:15px;border:0;border-radius:12px;font:600 17px system-ui;color:#fff;
    background:linear-gradient(90deg,var(--acc),#ff6a3d);cursor:pointer}
.go:active{transform:scale(.98)}
.go:disabled{opacity:.5}
details{margin-top:12px;color:var(--dim);font-size:13px}
summary{cursor:pointer}
.job{margin-bottom:10px}
.job .top{display:flex;justify-content:space-between;gap:10px;align-items:start}
.job .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job .s{color:var(--dim);font-size:13px}
.bar{height:6px;background:#0b0c10;border-radius:4px;margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .6s}
.job.done .bar i{background:var(--ok)}
.job.error .s{color:var(--acc)}
pre{white-space:pre-wrap;font-size:11.5px;background:#0b0c10;padding:10px;border-radius:8px;margin-top:8px;
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
  background:linear-gradient(90deg,var(--acc),#ff6a3d);color:#fff;font-size:12px;font-weight:600}
select:disabled{opacity:.45}
.capdet{margin-top:10px}
.cdlg{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:16px;padding:16px;
  width:min(680px,94vw);max-height:90vh}
.cdlg::backdrop{background:#000b}
.cdhead{display:flex;justify-content:space-between;align-items:center;font-size:17px}
.cdlg textarea{width:100%;background:#0b0c10;border:1px solid var(--line);color:var(--text);border-radius:10px;
  padding:10px;font:inherit;font-size:13px;margin-top:4px;resize:vertical}
.cdgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cdgrid>label{font-size:12px;color:var(--dim);display:block}
.cdgrid>label input[type=text]{margin-top:4px;padding:9px;font-size:14px}
.cdgrid .wide{grid-column:1/-1}
.dlbox{margin-top:14px;background:#0b0c10;border:1px solid var(--line);border-radius:12px;padding:10px}
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
.autonote{font-size:12.5px;color:var(--dim);background:#0b0c10;border-radius:10px;padding:10px;margin-top:10px}
.looks{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.looks button{background:#0b0c10;border:1px solid var(--line);color:var(--dim);border-radius:999px;
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
input[type=color]{width:100%;height:36px;border:1px solid var(--line);border-radius:8px;background:#0b0c10;padding:2px}
.prev{width:162px;height:288px;border-radius:14px;overflow:hidden;position:relative;flex-shrink:0;margin:auto;
  background:linear-gradient(160deg,#3a2f1a,#14161c 45%,#1d2a33);display:flex;justify-content:center;border:1px solid var(--line)}
.pv{position:absolute;left:8px;right:8px;text-align:center;font-weight:800;line-height:1.15}
.pvmatch{position:absolute;top:6px;right:6px;font-size:9px;background:#0009;color:#ffb03d;padding:2px 5px;border-radius:5px;display:none}
</style></head><body>
<h1>✂️ <span>Clipper</span></h1>

<form class="card" id="f">
  <input type="text" id="url" placeholder="Paste a YouTube link (or a file path on the PC)" autocomplete="off" required>
  <div style="margin-top:12px"><label style="font-size:12px;color:var(--dim)">Campaign</label>
    <div style="display:flex;gap:8px;margin-top:5px">
      <select id="camp" style="flex:1"><option value="">No campaign (free clipping)</option></select>
      <button type="button" class="small" id="campedit" title="Edit this campaign" style="display:none">✏️</button>
      <button type="button" class="small" id="campnew" style="white-space:nowrap">+ New</button></div>
    <div id="campnote" class="s" style="color:var(--acc2);font-size:12px;margin-top:5px"></div>
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
        <button type="button" data-v="crop">Zoom</button>
        <button type="button" data-v="none">Original</button></div></div>
    <div class="field"><label>Transcription</label>
      <div class="seg" data-name="model">
        <button type="button" data-v="base.en">Fast</button>
        <button type="button" data-v="small.en" class="on">Good</button>
        <button type="button" data-v="medium.en">Best</button></div></div>
  </div>
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
  <details><summary>AI picking (Ollama)</summary>
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
   <label>Clip length range<input type="text" id="cf_len" placeholder="20-60"></label>
   <label>Tag in caption<input type="text" id="cf_mention" placeholder="@brand"></label>
   <label>Disclosure hashtag<input type="text" id="cf_disclosure" placeholder="#Ad"></label>
   <label class="wide">On-screen text (burned into every clip)<input type="text" id="cf_on_screen"></label>
   <label class="wide">Keywords clips should mention (comma separated)<input type="text" id="cf_keywords"></label>
   <label class="wide">Extra hashtags (comma separated, max 3)<input type="text" id="cf_hashtags"></label>
   <label class="wide">Lines every caption must include (one per line)<textarea id="cf_required_lines" rows="2"></textarea></label>
   <label class="wide">Example captions (one per line)<textarea id="cf_example_captions" rows="4"></textarea></label>
   <label class="wide">Caption style for the AI<textarea id="cf_caption_rules" rows="2"></textarea></label>
   <label class="wide">Posting checklist (one per line)<textarea id="cf_checklist" rows="6"></textarea></label>
  </div>
  <div class="dlbox"><div class="dlhead"><b>📥 Footage in your Downloads</b>
    <span class="s" style="font-size:12px;color:var(--dim)">ticked files get moved into this campaign</span></div>
    <div id="cf_files" class="dllist"></div></div>
  <div class="row" style="margin-top:10px"><button type="button" class="small" id="cdback">← Paste again</button>
   <button type="button" class="go" id="cdsave" style="margin:0;flex:1">🚀 Publish campaign</button></div>
 </div>
</dialog>

<h2>Jobs</h2>
<div id="jobs"><div class="empty">Nothing running</div></div>

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
$('#camp').onchange=e=>{
  const c=camps[e.target.value];
  $('#campedit').style.display=e.target.value?'':'none';
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
    captions:$('#caps').checked,campaign:$('#camp').value,style:cs.preset,match:cs.match,words:cs.words,
    cap:{font:cs.font,size:cs.size,text:cs.text,highlight:cs.hlon?cs.hl:'',outline:cs.out,pos:cs.pos,upper:cs.upper,pop:cs.pop},
    music:$('#music').value,music_vol:+$('#mvol').value/100,ollama:$('#ollama').value.trim()});
  b.disabled=false;
  if(r.error){alert(r.error);return}
  poll(); window.scrollTo({top:document.querySelector('#jobs').offsetTop-20,behavior:'smooth'});
};
$('#batchgo').onclick=async()=>{
  const b=$('#batchgo'); b.disabled=true;
  const r=await api('/api/jobs',{batch:true,url:'',k:0,len:opt.len,mode:opt.mode,model:opt.model,
    captions:$('#caps').checked,campaign:$('#camp').value,style:cs.preset,match:cs.match,words:cs.words,
    cap:{font:cs.font,size:cs.size,text:cs.text,highlight:cs.hlon?cs.hl:'',outline:cs.out,pos:cs.pos,upper:cs.upper,pop:cs.pop},
    music:$('#music').value,music_vol:+$('#mvol').value/100,ollama:$('#ollama').value.trim()});
  b.disabled=false;
  if(r.error){alert(r.error);return}
  poll(); setTimeout(loadSources,3000);
};
$('#src').onchange=e=>{ if(e.target.value) $('#url').value=e.target.value; };
loadCamps();
/* ---------- campaign builder ---------- */
let cdEditId='';
const LISTF=['keywords','hashtags'], LINEF=['required_lines','example_captions','checklist'],
      TXTF=['name','agency','source_note','min_len','len','mention','disclosure','on_screen','caption_rules'];
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
function cdFill(c){
  TXTF.forEach(k=>$('#cf_'+k).value=c[k]??'');
  LISTF.forEach(k=>$('#cf_'+k).value=(c[k]||[]).join(', '));
  LINEF.forEach(k=>$('#cf_'+k).value=(c[k]||[]).join('\n'));
  $('#cf_music').checked=!!c.music;
  document.querySelectorAll('#cf_platforms input').forEach(i=>i.checked=(c.platforms||[]).includes(i.value));
  $('#cdstep1').style.display='none'; $('#cdstep2').style.display='';
  cdFiles();
}
function cdRead(){
  const c={music:$('#cf_music').checked,captions:true,mode:'blur',
    platforms:[...document.querySelectorAll('#cf_platforms input:checked')].map(i=>i.value)};
  TXTF.forEach(k=>c[k]=$('#cf_'+k).value.trim());
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
  $('#cdvia').textContent=r._via==='ollama'?'✨ Read by your local AI - check it over, then save.'
    :'Filled in automatically - check every box (especially name, agency, tag and on-screen text), fix anything, then save.';
  cdFill(r);
};
$('#cdsave').onclick=async()=>{
  const files=[...document.querySelectorAll('#cf_files input:checked')].map(i=>i.value);
  const b=$('#cdsave'); b.disabled=true; b.textContent=files.length?`Moving ${files.length} file(s)…`:'Saving…';
  const r=await api('/api/savecampaign',{campaign:cdRead(),id:cdEditId,brief:cdEditId?'':$('#cdbrief').value,files});
  b.disabled=false; b.textContent='🚀 Publish campaign';
  if(r.error){alert(r.error);return}
  $('#cdlg').close(); await loadCamps(); $('#camp').value=r.id; $('#camp').onchange({target:$('#camp')});
};
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
    model:opt.model,captions:$('#caps').checked,campaign:$('#camp').value,style:cs.auto?'auto':cs.preset,match:cs.match,words:cs.words,cap:capPayload(),music:$('#music').value,music_vol:+$('#mvol').value/100,ollama:$('#ollama').value.trim()});
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
  for(const j of jobs) if(j.status==='done'&&!finished.has(j.id)){finished.add(j.id);
    if(!first){refresh=true;if(j.clip)fresh.push(j.clip)}}
  first=false;
  if(refresh) await loadClips(); else markBusy();
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
  el.innerHTML=groups.map(g=>`
    <div class="group"><div class="gname"><span>${esc(g.name)}</span>
      <span class="s" style="color:var(--dim);font-size:12px">${g.clips.length} clip${g.clips.length==1?"":"s"}</span></div>
    <div class="grid">${g.clips.map(c=>`
      <div class="clip" data-path="${esc(c.path)}">
        <div class="vw"><video controls playsinline preload="none" poster="/thumb/${enc(c.path)}?v=${c.v}" src="/clips/${enc(c.path)}?v=${c.v}"></video>
          <div class="ov"></div><div class="badge">✓ Restyled</div></div>
        <div class="n" title="${esc(c.name)}">${esc(c.name)}</div>
        ${c.caption?`<button class="capbtn" onclick="copyText(capStore['${esc(c.path).replace(/'/g,"\\'")}'],this)">Copy caption</button>`:''}
        <div class="a"><a href="/clips/${enc(c.path)}?dl=1" download>Save</a>
          ${c.styled?`<button class="rs" title="Change the caption style" onclick="restyle('${esc(c.path).replace(/'/g,"\\'")}','${esc(c.name).replace(/'/g,"\\'")}')">🎨</button>`:''}
          <button onclick="del('${esc(c.path).replace(/'/g,"\\'")}')">Delete</button></div>
      </div>`).join('')}</div></div>`).join('');
  markBusy();
}
async function del(p){
  if(!confirm('Delete this clip?'))return;
  await api('/api/delete',{path:p}); loadClips();
}
poll(); loadClips(); setInterval(poll,1500);
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
        elif "asking ollama" in line:
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
            if p.get("restyle"):
                cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--restyle", p["restyle"],
                       "--style", p["style"], "--words", str(p["words"]), "--cap-json", json.dumps(p["cap"])]
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

    def run_job(job, cmd):
            job["status"], job["stage"] = "running", ("Re-rendering…" if job.get("clip") else "Starting…")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
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
                job["stage"] = ("Done — clip updated ✓" if job.get("clip")
                                else f"Done — {job['made']} clips ready")
            else:
                job["status"] = "error"
                job["error"] = job.get("error") or "Something went wrong — open the log"

    threading.Thread(target=worker, daemon=True).start()

    # ---------- helpers ----------
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
        for key, (root, label) in roots().items():
            if not root.is_dir():
                continue
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
                     "caption": cap.read_text().strip() if cap.is_file() else ""})
        out = []
        for name, clips in groups.items():
            clips.sort(key=lambda c: c["name"])
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
        return {k: (list(v) if k == "log" else v) for k, v in job.items()
                if k not in ("proc", "params")} | {"url": job["params"]["url"]}

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
                                "music": c.get("music", True), "on_screen": c.get("on_screen")})
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
                files = [str(x) for x in (d.get("files") or []) if isinstance(x, str)][:200]
                moved = import_downloads(new_id, files) if files else []
                self.send_json({"id": new_id, "moved": moved})
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
                    job["status"], job["stage"] = "cancelled", "Cancelled"
                    if job["proc"]:
                        try:
                            os.killpg(job["proc"].pid, signal.SIGTERM)
                        except ProcessLookupError:
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
                f, root = safe(str(d.get("path", "")), want_root=True)
                if f and f.suffix == ".mp4":
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
    p = argparse.ArgumentParser(description="Clip YouTube videos into vertical shorts.")
    p.add_argument("source", nargs="?", help="YouTube URL or local video file")
    p.add_argument("--ui", action="store_true", help="start the web interface")
    p.add_argument("--port", type=int, default=8765, help="web interface port (default 8765)")
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
    p.add_argument("-m", "--mode", choices=["blur", "crop", "none"], default="blur",
                   help="vertical style (default: blur)")
    p.add_argument("-c", "--captions", action="store_true", help="burn in captions (auto: on by default)")
    p.add_argument("--no-captions", action="store_true", help="auto: turn captions off")
    p.add_argument("--model", default="small", help="whisper model: tiny/base/small/medium (default small)")
    p.add_argument("--words", type=int, default=None, help="words per caption line")
    p.add_argument("--style", choices=["auto"] + list(CAPTION_PRESETS), default="auto",
                   help="caption look (default: auto = best for each clip)")
    p.add_argument("--restyle", metavar="CLIP.mp4", help="re-render a clip with new caption settings")
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
    p.add_argument("--music", help="background music: a file, or a folder (random track per clip)")
    p.add_argument("--music-vol", type=float, default=0.15, help="music volume 0-1 (default 0.15)")
    p.add_argument("--music-start", type=float, default=0.0, help="start the music N seconds in")
    p.add_argument("-o", "--out-dir", type=Path, default=None,
                   help="where clips go (default ~/Clips, or the campaign's clips folder)")
    args = p.parse_args()
    import signal                                     # Cancel in the web page: clean up, keep old clip
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    need("ffmpeg")
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
