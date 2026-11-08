"""Learns from Approve / Reject decisions in the Clip Review Studio (optional, local, resettable).

What it learns:
  * preferred clip length (median of approved clips) — used as a gentle target in Smart selection
  * which scoring signals approved clips have more of than rejected ones — small weight adjustments (±30%)
  * most-approved selection strategy and preset (shown to the user; not applied automatically)
"""
import json
from collections import Counter

from . import db

MIN_DECISIONS = 6


def _rows():
    out = []
    for r in db.q("SELECT f.decision, f.features, j.score_info FROM feedback f LEFT JOIN jobs j ON j.id=f.job_id"):
        try:
            feats = json.loads(r["features"] or "{}")
            info = json.loads(r["score_info"] or "{}")
            feats["signals"] = info.get("features", {})
            out.append((r["decision"], feats))
        except Exception:
            pass
    return out


def preferred_length():
    rows = [f["length"] for d, f in _rows() if d == "approved" and f.get("length")]
    if len(rows) < MIN_DECISIONS:
        return None
    rows.sort()
    return rows[len(rows) // 2]


def weight_multipliers():
    rows = _rows()
    ok = [f["signals"] for d, f in rows if d == "approved" and f["signals"]]
    bad = [f["signals"] for d, f in rows if d == "rejected" and f["signals"]]
    if len(ok) < 3 or len(bad) < 3:
        return {}
    out = {}
    for k in ok[0]:
        a = sum(x.get(k, 0) for x in ok) / len(ok)
        b = sum(x.get(k, 0) for x in bad) / len(bad)
        out[k] = max(0.7, min(1.3, 1 + (a - b)))
    return out


def describe():
    rows = _rows()
    n_ok = sum(d == "approved" for d, _ in rows)
    n_bad = sum(d == "rejected" for d, _ in rows)
    if not rows:
        return "Nothing learned yet. Approve or reject clips in the Clip Review Studio."
    parts = [f"{n_ok} approved and {n_bad} rejected clips recorded."]
    pl = preferred_length()
    parts.append(f"Preferred length: about {pl:.0f}s." if pl else
                 f"Preferred length is used after {MIN_DECISIONS} approvals.")
    modes = Counter(f.get("mode") for d, f in rows if d == "approved" and f.get("mode"))
    presets = Counter(f.get("preset") for d, f in rows if d == "approved" and f.get("preset"))
    if modes:
        parts.append(f"Most approved strategy: {modes.most_common(1)[0][0]}.")
    if presets:
        parts.append(f"Most approved preset: {presets.most_common(1)[0][0]}.")
    m = weight_multipliers()
    if m:
        from .scoring import FACTORS
        up = [FACTORS[k] for k, v in m.items() if v > 1.08]
        down = [FACTORS[k] for k, v in m.items() if v < 0.92]
        if up:
            parts.append("Scoring now favours: " + ", ".join(up) + ".")
        if down:
            parts.append("Scoring now de-emphasises: " + ", ".join(down) + ".")
    return " ".join(parts)
