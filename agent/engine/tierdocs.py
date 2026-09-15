"""Generate TIER*.md — what is in each tier and why, measured from the messages.

The prose is generated from the archive rather than written by hand, so it cannot
drift from reality: re-run after any download and the numbers are current. The only
hand-written part is the `note` on each placement in tiers.json, which carries the
judgement a measurement cannot ("verified against the RHP", "copies others").
"""

import collections
import json
import re
import statistics as st
from datetime import datetime, timezone

from engine import archive, tiers, verticals

IPO = re.compile(r"\b(ipo|gmp|grey market|listing|allot|subscri)", re.I)
CALL = re.compile(r"\b(buy|sell)\b.{0,40}?\b(above|below|at|near|cmp)\b|\btarget\b|\bsl\b|\bstop\s?loss\b", re.I)
SL = re.compile(r"\b(?:sl|stop\s?loss)\b[^\d]{0,12}\d", re.I)
PROMO = re.compile(r"(join|premium|paid|dm\b|refer|link in bio|subscribe now|whatsapp)", re.I)
GUJ = re.compile(r"[઀-૿]")


def profile(path) -> dict:
    """Measure one channel's file: volume, cadence, what it actually posts."""
    if not path.is_file():
        return {}
    n = media = 0
    texts, dates = [], []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            if r.get("media_type"):
                media += 1
            if r.get("text"):
                texts.append(r["text"])
            if r.get("date"):
                dates.append(datetime.fromisoformat(r["date"]))
    if not n or not dates:
        return {}
    span = max(1, (max(dates) - min(dates)).days)
    t = texts or [""]
    guj = sum(1 for x in t if len(GUJ.findall(x)) > 3)
    return {
        "messages": n,
        "first": min(dates), "last": max(dates),
        "age_days": (datetime.now(timezone.utc) - max(dates)).days,
        "per_day": n / span,
        "media_pct": 100 * media / n,
        "subst_pct": 100 * sum(1 for x in t if len(x) >= 120) / n,
        "median_len": st.median([len(x) for x in t]),
        "ipo_pct": 100 * sum(1 for x in t if IPO.search(x)) / len(t),
        "call_pct": 100 * sum(1 for x in t if CALL.search(x)) / len(t),
        "sl_pct": 100 * sum(1 for x in t if SL.search(x)) / max(1, sum(1 for x in t if CALL.search(x))),
        "promo_pct": 100 * sum(1 for x in t if PROMO.search(x)) / len(t),
        "guj_pct": 100 * guj / len(t),
    }


def describe(p: dict) -> str:
    """One line saying what the channel mainly posts, from the numbers alone."""
    if not p:
        return "no messages downloaded yet"
    bits = []
    if p["ipo_pct"] >= 50:
        bits.append("almost entirely IPO/GMP")
    elif p["ipo_pct"] >= 20:
        bits.append("heavily IPO/GMP")
    elif p["ipo_pct"] >= 8:
        bits.append("some IPO coverage")
    if p["call_pct"] >= 12:
        bits.append(f"gives trade calls ({p['call_pct']:.0f}% of texts, "
                    f"{p['sl_pct']:.0f}% with a stop-loss)")
    elif p["call_pct"] >= 5:
        bits.append(f"occasional calls ({p['call_pct']:.0f}%)")
    if p["guj_pct"] >= 40:
        bits.append("mostly Gujarati")
    elif p["guj_pct"] >= 10:
        bits.append(f"part Gujarati ({p['guj_pct']:.0f}%)")
    if p["media_pct"] >= 45:
        bits.append(f"image-led ({p['media_pct']:.0f}% media)")
    if p["subst_pct"] < 8:
        bits.append(f"very low text substance ({p['subst_pct']:.0f}%)")
    elif p["subst_pct"] >= 30:
        bits.append(f"substantial text ({p['subst_pct']:.0f}%)")
    if p["promo_pct"] >= 8:
        bits.append(f"promotional ({p['promo_pct']:.0f}%)")
    return "; ".join(bits) or "general market chatter"


def write(name: str) -> list:
    man = archive.read_manifest(name)
    root = verticals.DIR / name / "messages"
    data = tiers.load(name)
    labels = data.get("labels", tiers.TIER_LABELS)
    grouped = tiers.by_tier(name)

    # channels present on disk but never placed belong to the default tier
    for cid, c in man.get("channels", {}).items():
        if str(cid) not in data["placements"]:
            grouped.setdefault(tiers.DEFAULT_TIER, {}).setdefault("", []).append(
                {"id": int(cid), "title": c.get("title"), "note": ""})

    written = []
    for tier, cats in grouped.items():
        out = [f"# {labels.get(tier, tier)}", ""]
        total = sum(len(v) for v in cats.values())
        out += [f"{total} channel(s). Generated {datetime.now(timezone.utc):%Y-%m-%d} "
                f"from the archive — re-run `run.py vertical docs {name}` after a download.", ""]
        if tier == tiers.DEFAULT_TIER:
            out += ["> Everything discovered in the Telegram folder lands here until it is",
                    "> categorised by hand. Nothing is promoted automatically.", ""]
        for cat in sorted(cats):
            rows = sorted(cats[cat], key=lambda r: (r.get("title") or "").lower())
            out += [f"## {cat or 'uncategorised'}", ""]
            for r in rows:
                entry = man.get("channels", {}).get(str(r["id"]), {})
                p = profile(root / entry.get("file", "")) if entry.get("file") else {}
                title = r.get("title") or entry.get("title") or str(r["id"])
                uname = entry.get("username")
                out.append(f"### {title}")
                out.append("")
                if uname:
                    out.append(f"`@{uname}` · id `{r['id']}`")
                else:
                    out.append(f"id `{r['id']}` · no public username (private/paid)")
                out.append("")
                if p:
                    state = "live" if p["age_days"] <= 3 else f"{p['age_days']}d silent"
                    out += [
                        "| | |", "|---|---|",
                        f"| messages | {p['messages']:,} |",
                        f"| range | {p['first']:%Y-%m} → {p['last']:%Y-%m} ({state}) |",
                        f"| rate | {p['per_day']:.1f}/day |",
                        f"| media | {p['media_pct']:.0f}% |",
                        f"| substantial text | {p['subst_pct']:.0f}% (median {p['median_len']:.0f} chars) |",
                        "",
                        f"**Posts:** {describe(p)}", "",
                    ]
                else:
                    out += ["_not downloaded yet_", ""]
                if r.get("note"):
                    out += [f"**Why this tier:** {r['note']}", ""]
        path = root / tier / f"{tier.upper().replace('-', '')}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out), encoding="utf-8")
        written.append(path)
    return written
