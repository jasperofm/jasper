"""Choosing which parts of a long video become clips.

Modes: random, time (evenly spread), scene, highlight, manual, hybrid.
All modes skip black/empty footage, respect a minimum spacing between clips and
can avoid sections that were already exported before.
"""
import random
from dataclasses import dataclass, field

MODES = ["smart", "speech", "hybrid", "highlight", "scene", "random", "time", "manual"]
MODE_LABELS = {
    "smart": "Smart (scored candidates, best first)",
    "speech": "Speech-based (whole sentences / thoughts)",
    "hybrid": "Hybrid (manual + highlights + scenes)",
    "highlight": "Highlights (loud, active moments)",
    "scene": "Scene-based (cuts on scene changes)",
    "random": "Random sections",
    "time": "Evenly spread over time",
    "manual": "Manual (your timeline selections)",
}


@dataclass
class SelectionSettings:
    mode: str = "smart"
    clips_per_video: int = 5
    durations: list = field(default_factory=lambda: [30.0])   # target lengths, cycled for variety
    min_len: float = 10.0
    max_len: float = 90.0
    min_spacing: float = 5.0
    skip_intro: float = 0.0
    skip_outro: float = 0.0
    avoid_silence: bool = True
    avoid_black: bool = True
    avoid_used: bool = True
    speech_only: bool = False
    snap_to_pauses: bool = True
    min_score: float = 0.0
    pre_roll: float = 0.0
    post_roll: float = 0.0
    seed: int = None

    def to_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


def _subtract(regions, cut):
    out = []
    a2, b2 = cut
    for a, b in regions:
        if b2 <= a or a2 >= b:
            out.append((a, b))
            continue
        if a < a2:
            out.append((a, a2))
        if b2 < b:
            out.append((b2, b))
    return out


def usable_regions(duration, analysis, s: SelectionSettings, used=()):
    start, end = max(0.0, s.skip_intro), max(0.0, duration - s.skip_outro)
    regions = [(start, end)] if end - start > 0.5 else []
    an = analysis or {}
    if s.avoid_black:
        for a, b in an.get("black", []):
            regions = _subtract(regions, (a, b))
    if s.avoid_silence or s.speech_only:
        min_gap = 1.0 if s.speech_only else 4.0     # only long silences are "unusable" unless speech-only
        for a, b in an.get("silence", []):
            b = duration if b is None else b
            if b - a >= min_gap:
                regions = _subtract(regions, (a + 0.3, b - 0.3))
    if s.avoid_used:
        for a, b in used:
            regions = _subtract(regions, (a - s.min_spacing, b + s.min_spacing))
    return [(a, b) for a, b in regions if b - a >= max(1.0, s.min_len)]


def _fits(cand, chosen, spacing):
    a, b = cand
    return all(b + spacing <= c[0] or a >= c[1] + spacing for c in chosen)


def _clamp_len(t, s):
    return max(s.min_len, min(s.max_len, t))


def _snap(t, pauses, window=1.5):
    best = None
    for p in pauses:
        if abs(p - t) <= window and (best is None or abs(p - t) < abs(best - t)):
            best = p
    return best if best is not None else t


def _pause_points(analysis):
    pts = []
    for a, b in (analysis or {}).get("silence", []):
        if b is not None:
            pts.append((a + b) / 2)
    return sorted(pts)


def _place(region, start, length):
    a, b = region
    start = max(a, min(start, b - length))
    return (start, min(b, start + length))


def _in_region(regions, a, b):
    for r in regions:
        if a >= r[0] - 0.01 and b <= r[1] + 0.01:
            return r
    return None


def _finish(chosen, s, duration):
    if not (s.pre_roll or s.post_roll):
        return sorted(chosen)
    return sorted((round(max(0.0, a - s.pre_roll), 2), round(min(duration, b + s.post_roll), 2), why)
                  for a, b, why in chosen)


def _sentences(transcript):
    out, cur = [], []
    for w in transcript or []:
        cur.append(w)
        if w[2][-1:] in ".!?":
            out.append((cur[0][0], cur[-1][1]))
            cur = []
    if cur:
        out.append((cur[0][0], cur[-1][1]))
    return out


def _speech_spans(analysis, duration):
    """Stretches between silences, used when there is no transcript."""
    spans, t = [], 0.0
    for a, b in sorted((analysis or {}).get("silence", [])):
        if a - t > 1.0:
            spans.append((t, a))
        t = duration if b is None else b
    if duration - t > 1.0:
        spans.append((t, duration))
    return spans


def _speech_candidates(units, regions, s, targets):
    """Join consecutive sentences/spans into clips close to each target length, never cutting inside one."""
    cands = []
    for L in set(targets):
        for i in range(len(units)):
            a = units[i][0]
            for j in range(i, len(units)):
                b = units[j][1]
                if b - a > s.max_len:
                    break
                if b - a >= s.min_len and (b - a >= L * 0.85 or j == len(units) - 1):
                    if _in_region(regions, a, b):
                        cands.append((a, b))
                    break
    return cands


def _window_candidates(regions, targets, pauses, s):
    cands = []
    for L in set(targets):
        step = max(2.0, L / 3)
        for ra, rb in regions:
            t = ra
            while t + s.min_len <= rb:
                a, b = t, min(rb, t + L)
                if pauses:
                    a2, b2 = _snap(a, pauses, 2.0), _snap(b, pauses, 2.5)
                    if ra <= a2 < b2 <= rb and s.min_len <= b2 - a2 <= s.max_len:
                        a, b = a2, b2
                if b - a >= s.min_len:
                    cands.append((a, b))
                t += step
    return cands


def _pick_scored(cands, analysis, transcript, s, chosen, add, label):
    from . import scoring
    w = scoring.weights()
    scored = []
    for a, b in set((round(a, 2), round(b, 2)) for a, b in cands):
        sc, expl, _ = scoring.score(analysis, a, b, transcript, w)
        if sc >= s.min_score:
            scored.append((sc, a, b, expl))
    scored.sort(reverse=True)
    bonus = scoring.cloud_rerank([(a, b) for _, a, b, _ in scored[:25]], transcript)
    if bonus:
        top = [(sc + 2 * bonus.get(i, (0, ""))[0], a, b, expl) for i, (sc, a, b, expl) in enumerate(scored[:25])]
        scored = sorted(top, reverse=True) + scored[25:]
    for sc, a, b, expl in scored:
        if len(chosen) >= s.clips_per_video:
            break
        add(a, b, f"{label} {sc:.0f}")


def select(duration, analysis, s: SelectionSettings, used=(), manual=(), transcript=None):
    """Return a list of (start, end, reason) for one video."""
    rng = random.Random(s.seed)
    regions = usable_regions(duration, analysis, s, used)
    targets = [_clamp_len(float(t), s) for t in (s.durations or [30.0])]
    chosen = []   # (start, end, reason)

    def want(i):
        return targets[i % len(targets)]

    def add(a, b, why):
        if b - a < s.min_len - 0.01:
            return False
        if not _fits((a, b), chosen, s.min_spacing):
            return False
        chosen.append((round(a, 2), round(b, 2), why))
        return True

    mode = s.mode
    pauses = _pause_points(analysis) if s.snap_to_pauses else []

    if mode in ("manual", "hybrid"):
        for a, b in manual:
            if len(chosen) >= s.clips_per_video:
                break
            if b > a:
                add(max(0, a), min(duration, b), "manual")
        if mode == "manual":
            return _finish(chosen, s, duration)

    if mode in ("smart", "speech"):
        try:
            from . import learning
            pl = learning.preferred_length() if mode == "smart" else None
        except Exception:
            pl = None
        tl = targets + ([_clamp_len(pl, s)] if pl else [])
        if mode == "speech":
            units = _sentences(transcript) or _speech_spans(analysis, duration)
            cands = _speech_candidates(units, regions, s, tl)
        else:
            sent = _sentences(transcript)
            snaps = sorted(set(pauses + [x for ab in sent for x in ab])) if s.snap_to_pauses else []
            cands = _window_candidates(regions, tl, snaps, s)
        _pick_scored(cands, analysis, transcript, s, chosen, add, "score")
        return _finish(chosen, s, duration)

    def from_starts(starts, why, jitter=0.0):
        for st in starts:
            if len(chosen) >= s.clips_per_video:
                return
            length = want(len(chosen))
            if jitter:
                length = _clamp_len(length * rng.uniform(1 - jitter, 1 + jitter), s)
            r = _in_region(regions, st, st + 0.01)
            if r is None or r[1] - r[0] < s.min_len:
                continue
            length = min(length, r[1] - r[0])
            a, b = _place(r, st, length)
            if pauses:
                b2 = _snap(b, pauses)
                if r[0] <= b2 <= r[1] and s.min_len <= b2 - a <= s.max_len:
                    b = b2
            add(a, b, why)

    if mode in ("highlight", "hybrid"):
        from_starts(_highlight_starts(analysis, duration, targets[0]), "highlight")
    if mode in ("scene", "hybrid"):
        scenes = [t for t in (analysis or {}).get("scenes", [])]
        order = scenes[:]
        if mode == "scene":
            rng.shuffle(order)          # different scene starts on every run
        from_starts(order, "scene change")
    if mode == "time":
        total = sum(b - a for a, b in regions)
        if total > 0 and regions:
            n = s.clips_per_video
            starts = []
            for k in range(n):
                pos = total * (k + 0.5) / n - want(k) / 2
                acc = 0
                for a, b in regions:
                    if pos <= acc + (b - a):
                        starts.append(a + max(0, pos - acc))
                        break
                    acc += b - a
            from_starts(starts, "time")
    if mode in ("random", "hybrid", "scene", "highlight", "time") and len(chosen) < s.clips_per_video:
        # fill (or, in random mode, make every clip) from random positions
        tries = 0
        while len(chosen) < s.clips_per_video and tries < 400 and regions:
            tries += 1
            weights = [b - a for a, b in regions]
            r = rng.choices(regions, weights=weights)[0]
            length = min(want(len(chosen)), r[1] - r[0])
            if length < s.min_len:
                continue
            st = rng.uniform(r[0], r[1] - length)
            a, b = st, st + length
            if pauses:
                a2 = _snap(a, pauses)
                if a2 >= r[0] and b - a2 >= s.min_len:
                    a = a2
            add(a, b, "random" if mode == "random" else "fill")
    return _finish(chosen, s, duration)


def _highlight_starts(analysis, duration, length):
    """Starts of windows with the most energy: loud audio plus lots of scene changes."""
    an = analysis or {}
    rms = an.get("rms") or []
    n = int(duration)
    if n <= 0:
        return []
    score = [0.0] * n
    if rms:
        lo = min(rms)
        for i in range(min(n, len(rms))):
            score[i] += (rms[i] - lo) / 10.0
    for t in an.get("scenes", []):
        if 0 <= int(t) < n:
            score[int(t)] += 1.5
    w = max(1, int(length))
    windows = []
    acc = sum(score[:w])
    for i in range(0, max(1, n - w)):
        windows.append((acc, i))
        if i + w < n:
            acc += score[i + w] - score[i]
    windows.sort(reverse=True)
    return [float(i) for _, i in windows[:200]]
