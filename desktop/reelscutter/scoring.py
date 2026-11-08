"""Candidate clip scoring: transparent, weighted signals with a written explanation.

Scores are a way to prioritise moments in *your* footage (clear speech, energy, natural boundaries,
picture quality, your keywords). They are not predictions of views or virality.
"""
import json

from . import db

FACTORS = {
    "speech": "Speech / sound present",
    "energy": "Audio energy",
    "hook": "Strong opening",
    "visual": "Visual activity (scene changes)",
    "boundaries": "Natural start and end",
    "quality": "Technical quality",
    "keywords": "Your keywords",
}
DEFAULT_WEIGHTS = dict(speech=1.0, energy=1.0, hook=0.8, visual=0.6, boundaries=1.0, quality=0.8, keywords=1.5)


def weights():
    try:
        w = json.loads(db.setting("score_weights") or "{}")
    except Exception:
        w = {}
    out = dict(DEFAULT_WEIGHTS, **{k: float(v) for k, v in w.items() if k in DEFAULT_WEIGHTS})
    if db.setting("learning_enabled", "1") == "1":
        from . import learning
        for k, m in learning.weight_multipliers().items():
            out[k] = out[k] * m
    return out


def _words(text):
    return [w.strip().lower() for w in (text or "").split(",") if w.strip()]


def _overlap(a, b, spans):
    tot = 0.0
    for s, e in spans:
        e = b if e is None else e
        tot += max(0.0, min(b, e) - max(a, s))
    return tot


def features(analysis, a, b, transcript=None):
    an = analysis or {}
    L = max(0.1, b - a)
    f = {}
    silence = an.get("silence") or []
    f["speech"] = 1 - min(1.0, _overlap(a, b, silence) / L) if "rms" in an else 0.5
    rms = an.get("rms") or []
    seg = rms[int(a): max(int(a) + 1, int(b))]
    if seg:
        mean = sum(seg) / len(seg)
        f["energy"] = max(0.0, min(1.0, (mean + 60) / 45))
        head = seg[:3]
        f["hook"] = max(0.0, min(1.0, 0.5 + (sum(head) / len(head) - mean) / 12))
    else:
        f["energy"] = f["hook"] = 0.5
    scenes = [t for t in an.get("scenes") or [] if a < t < b]
    f["visual"] = min(1.0, len(scenes) / max(1.0, L / 8))
    pauses = [e for s, e in silence if e is not None] + [s for s, e in silence]
    if transcript:
        ends = [w[1] for w in transcript if w[2][-1:] in ".!?"]
        starts = [transcript[i + 1][0] for i, w in enumerate(transcript[:-1]) if w[2][-1:] in ".!?"]
        pauses += ends + starts
    near = lambda t: any(abs(t - p) < 0.8 for p in pauses)       # noqa: E731
    f["boundaries"] = (0.5 * near(a) + 0.5 * near(b)) if pauses else 0.5
    black = _overlap(a, b, an.get("black") or [])
    q = 1 - min(1.0, black / L)
    qual = an.get("quality")
    if qual:
        q *= 1 - min(0.5, (qual.get("blurry_pct", 0) + qual.get("dark_pct", 0)) / 200)
    f["quality"] = q
    kw = _words(db.setting("score_keywords"))
    text = " ".join(w[2].lower() for w in transcript or [] if a <= w[0] <= b)
    f["keywords"] = min(1.0, sum(text.count(k) for k in kw) / 2) if kw and text else 0.0
    f["_excluded"] = any(x in text for x in _words(db.setting("score_exclusions"))) if text else False
    f["_has_keywords"] = bool(kw and text)
    return f


def score(analysis, a, b, transcript=None, w=None):
    """(score 0-100, explanation, features)."""
    w = w or weights()
    f = features(analysis, a, b, transcript)
    if f["_excluded"]:
        return 0.0, "Contains one of your excluded words", f
    use = {k: v for k, v in w.items() if k != "keywords" or f["_has_keywords"]}
    tot = sum(use.values()) or 1
    s = 100 * sum(f[k] * v for k, v in use.items()) / tot
    ranked = sorted(use, key=lambda k: f[k] * use[k], reverse=True)
    good = [FACTORS[k].lower() for k in ranked[:2] if f[k] >= 0.6]
    weak = [FACTORS[k].lower() for k in ranked[::-1][:2] if f[k] < 0.35]
    expl = ("Strong: " + ", ".join(good) if good else "No standout signals") + \
        ("; weak: " + ", ".join(weak) if weak else "")
    return round(s, 1), expl, {k: round(v, 3) for k, v in f.items() if not k.startswith("_")}


def cloud_rerank(cands, transcript):
    """Optional: ask Claude to rank candidates by how self-contained and clear each excerpt is.

    Only runs when the user enabled cloud AI and gave a key. Sends transcript text only. Returns
    {index: (bonus -10..10, reason)} or {} on any problem (the local scores are then used unchanged).
    """
    if db.setting("cloud_ai_enabled", "0") != "1" or not db.setting("anthropic_api_key") or not transcript:
        return {}
    try:
        import anthropic
        items = []
        for i, (a, b) in enumerate(cands[:25]):
            text = " ".join(w[2] for w in transcript if a <= w[0] <= b)[:1200]
            if text:
                items.append({"id": i, "text": text})
        if not items:
            return {}
        client = anthropic.Anthropic(api_key=db.setting("anthropic_api_key"))
        msg = client.messages.create(
            model="claude-opus-5-5", max_tokens=4000,
            system="You review transcript excerpts from the user's own long videos and rate how well each works "
                   "as a standalone short clip: complete thought, clear start and end, understandable without "
                   "context. Do not predict views or virality. Reply with JSON only: "
                   "[{\"id\": int, \"rating\": -10..10, \"reason\": \"short\"}].",
            messages=[{"role": "user", "content": json.dumps(items)}])
        txt = "".join(getattr(b, "text", "") for b in msg.content)
        data = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
        return {int(d["id"]): (max(-10, min(10, float(d["rating"]))), str(d.get("reason", ""))[:200])
                for d in data}
    except Exception as e:
        db.log("warning", f"Cloud ranking skipped: {e}")
        return {}
