"""Replay archived messages through the live pipeline. The P6 validation harness.

Reads real messages from the archive, injects them as events on a reserved channel-id
range, and runs the full orchestrator over them. Everything is torn down afterwards so
production data is untouched.
"""

import asyncio, json, random, sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT)); sys.path.insert(0, str(AGENT.parent))

from core import active                      # noqa: E402
import db as adb, orchestrator as orc            # noqa: E402
from engine import db as edb                     # noqa: E402

REPLAY_BASE = -998_000_000        # reserved band; no real channel is in it
# R7: the archive belongs to whichever vertical is running, not to trading by name.
MSGS = active.path() / "messages"


def clean():
    edb.execute("DELETE FROM content.agent_events WHERE channel_id < %s AND channel_id > %s",
                (REPLAY_BASE + 1_000_000, REPLAY_BASE - 1_000_000))


def load(n: int, seed: int = 7) -> list:
    pool = []
    for tier in ("tier1", "tier2", "tier3", "useless"):
        for f in (MSGS / tier).rglob("*.jsonl"):
            for line in f.open(encoding="utf-8"):
                line = line.strip()
                if line:
                    pool.append((tier, line))
                    if len(pool) > 400_000:
                        break
    random.seed(seed)
    out = []
    for tier, line in random.sample(pool, min(len(pool), n * 6)):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append({"tier": tier, "r": r})
        if len(out) >= n:
            break
    return out


def inject(rows: list) -> int:
    made = 0
    for i, row in enumerate(rows):
        r = row["r"]
        if adb.record_event(channel_id=REPLAY_BASE - (i % 500), message_id=1_000_000 + i,
                            channel_title=(r.get("channel") or "replay")[:80],
                            tier=row["tier"], text=r.get("text") or "",
                            urls=r.get("urls") or [], has_media=bool(r.get("media_type")),
                            media_kind=r.get("media_type"),
                            content_hash=r.get("content_hash")):
            made += 1
    return made


async def main(n: int, conc: int):
    adb.migrate(); clean()
    rows = load(n)
    made = inject(rows)
    print(f"injected {made} archived messages; running the live pipeline…", flush=True)
    stats = await orc.run(limit=n + 50, concurrency=conc)
    dist = edb.fetch_all("""SELECT intent, decision, count(*) n FROM content.agent_events
                            WHERE channel_id BETWEEN %s AND %s GROUP BY 1,2 ORDER BY n DESC""",
                         (REPLAY_BASE - 1000, REPLAY_BASE))
    undecided = edb.fetch_one("""SELECT count(*) n FROM content.agent_events
                                 WHERE channel_id BETWEEN %s AND %s AND decision IS NULL""",
                              (REPLAY_BASE - 1000, REPLAY_BASE))["n"]
    noreason = edb.fetch_one("""SELECT count(*) n FROM content.agent_events
                                WHERE channel_id BETWEEN %s AND %s
                                AND decision IS NOT NULL
                                AND coalesce(trim(decision_reason),'')=''""",
                             (REPLAY_BASE - 1000, REPLAY_BASE))["n"]
    spend = adb.spend_since(1)
    out = {**stats, "injected": made, "undecided": undecided,
           "decisions_without_reason": noreason,
           "calls_last_hour": spend["calls"], "failures_last_hour": spend["failures"],
           "usd_last_hour": float(spend["usd"])}
    print(json.dumps(out, indent=2))
    print("\nintent x decision:")
    for d in dist:
        print(f"   {(d['intent'] or '-'):<16}{d['decision']:<10}{d['n']:>5}")
    Path(AGENT / "bench" / "replay_result.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=250)
    ap.add_argument("--concurrency", type=int, default=3)
    a = ap.parse_args()
    asyncio.run(main(a.n, a.concurrency))
