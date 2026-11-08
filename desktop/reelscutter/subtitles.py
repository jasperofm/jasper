"""Local speech recognition (faster-whisper) and captions: burned-in ASS, SRT and VTT.

Models are never downloaded silently: the user installs them from the AI Analysis page. Only one model is
kept in memory, and it is unloaded after an idle period to free RAM.
"""
import subprocess
import threading
import time
from pathlib import Path

from . import db, paths
from .media import NO_WINDOW

# fontsize, text colour, highlight colour, outline colour, back colour, border style, outline, shadow, marginV(%)
STYLES = {
    "classic": (0.044, "&H00FFFFFF", "&H0000FFFF", "&H00000000", "&H64000000", 1, 6, 2, 0.27),
    "bold": (0.054, "&H00FFFFFF", "&H0000FF00", "&H00000000", "&H00000000", 1, 9, 0, 0.29),
    "boxed": (0.040, "&H00FFFFFF", "&H0000D7FF", "&H00000000", "&H50000000", 3, 14, 0, 0.27),
    "minimal": (0.034, "&H00FFFFFF", "&H00FFFFFF", "&H00000000", "&H80000000", 1, 2, 1, 0.20),
}
POSITIONS = {"bottom": 2, "middle": 5, "top": 8}

# approximate download sizes (MB) of the CTranslate2 int8 models
MODELS = {"tiny": 75, "base": 145, "small": 485, "medium": 1530}

_model = None
_model_name = None
_last_used = 0.0
_lock = threading.Lock()


def available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def models_dir():
    d = paths.data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_installed(name):
    d = models_dir() / f"models--Systran--faster-whisper-{name}"
    return d.exists() and any(d.rglob("model.bin"))


def download_model(name):
    """Explicit, user-approved download (called from the UI after a confirmation)."""
    from faster_whisper import download_model as dl
    return dl(name, cache_dir=str(models_dir()))


def delete_model(name):
    import shutil
    unload()
    shutil.rmtree(models_dir() / f"models--Systran--faster-whisper-{name}", ignore_errors=True)


def current_model_name():
    name = db.setting("whisper_model", "base")
    if model_installed(name):
        return name
    for n in MODELS:
        if model_installed(n):
            return n
    return None


def _whisper(name=None):
    global _model, _model_name, _last_used
    name = name or current_model_name()
    if not name:
        raise RuntimeError("No speech model installed. Install one on the AI Analysis page.")
    if _model is None or _model_name != name:
        from faster_whisper import WhisperModel
        _model = None
        _model = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(models_dir()),
                              local_files_only=True, cpu_threads=max(1, int(db.setting("threads_per_job", "2"))))
        _model_name = name
        threading.Thread(target=_idle_unloader, daemon=True).start()
    _last_used = time.time()
    return _model


def _idle_unloader():
    while _model is not None:
        time.sleep(30)
        idle = float(db.setting("model_idle_minutes", "5")) * 60
        if time.time() - _last_used > idle and not _lock.locked():
            unload()


def unload():
    global _model, _model_name
    _model, _model_name = None, None
    import gc
    gc.collect()


def transcribe_file(src, start, end, model=None, language=None):
    """Words with absolute source timestamps: [(start, end, text)]. Streams audio via FFmpeg (16 kHz mono)."""
    import numpy as np
    pcm = subprocess.run([paths.ffmpeg(), "-v", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
                          "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True, **NO_WINDOW).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    with _lock:
        segs, _info = _whisper(model).transcribe(audio, word_timestamps=True, vad_filter=True, language=language)
        words = [(round(start + w.start, 3), round(start + w.end, 3), w.word.strip())
                 for s in segs for w in (s.words or []) if w.word.strip()]
    global _last_used
    _last_used = time.time()
    return words


def group_lines(words, chunk=3):
    """Split words into short caption lines, breaking on punctuation."""
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= chunk or w[2][-1:] in ".!?,":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def _ts(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _srt_ts(t, sep=","):
    t = max(0.0, t)
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d}{sep}{ms % 1000:03d}"


def write_srt(words, offset, out_path, chunk=6):
    lines = []
    for i, g in enumerate(group_lines(words, chunk), 1):
        lines.append(f"{i}\n{_srt_ts(g[0][0] - offset)} --> {_srt_ts(g[-1][1] - offset)}\n"
                     f"{' '.join(w[2] for w in g)}\n")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_vtt(words, offset, out_path, chunk=6):
    lines = ["WEBVTT\n"]
    for g in group_lines(words, chunk):
        lines.append(f"{_srt_ts(g[0][0] - offset, '.')} --> {_srt_ts(g[-1][1] - offset, '.')}\n"
                     f"{' '.join(w[2] for w in g)}\n")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_ass(words, offset, out_path, size, style="classic", chunk=3, font="Arial", position="bottom",
              uppercase=True, highlight=True):
    """Word-highlighted (karaoke style) captions; timestamps relative to the clip start (offset)."""
    if not words:
        return None
    W, H = size
    fs, col, hi, outc, back, border, outl, shad, mv = STYLES.get(style, STYLES["classic"])
    align = POSITIONS.get(position, 2)
    margin = int(H * mv) if align == 2 else int(H * 0.08)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{int(H * fs)},{col},{hi},{outc},{back},-1,0,0,0,100,100,0,0,{border},{outl},{shad},{align},{W // 14},{W // 14},{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def clean(s):
        s = s.strip().replace("{", "(").replace("}", ")").replace("\\", "")
        return s.upper() if uppercase else s

    groups = group_lines(words, chunk)
    lines = []
    for gi, g in enumerate(groups):
        g_end = g[-1][1]
        nxt = groups[gi + 1][0][0] if gi + 1 < len(groups) else g_end + 0.4
        g_end = min(max(g_end, nxt), g_end + 0.6)
        if not highlight:
            lines.append(f"Dialogue: 0,{_ts(g[0][0] - offset)},{_ts(g_end - offset)},Cap,,0,0,0,,"
                         + " ".join(clean(x[2]) for x in g))
            continue
        for i, w in enumerate(g):
            en = g[i + 1][0] if i + 1 < len(g) else g_end
            txt = " ".join(("{\\c" + hi + "&}" + clean(x[2]) + "{\\c" + col + "&}") if k == i else clean(x[2])
                           for k, x in enumerate(g))
            lines.append(f"Dialogue: 0,{_ts(w[0] - offset)},{_ts(en - offset)},Cap,,0,0,0,,{txt}")
    Path(out_path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def make_ass(src, start, end, out_path, size, style="classic", chunk=3, words=None, **kw):
    """Transcribe src[start:end] (unless words are given) and write captions. Returns out_path or None."""
    if words is None:
        words = transcribe_file(src, start, end)
    words = [w for w in words if w[1] > start and w[0] < end]
    return write_ass(words, start, out_path, size, style, chunk, **kw)
