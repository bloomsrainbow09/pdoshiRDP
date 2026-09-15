"""The soak: run the whole system against live Telegram and watch it for hours.

Six hours is not an arbitrary number. It is the GitHub Actions job limit, so it is
exactly one runner's life — the soak is a rehearsal of the unit of work that will repeat
four times a day forever.

What it watches, and why each one:

  **Nothing missed.** Verified against Telegram ITSELF, not against our own records. A
  system that only checks its own database will happily report perfect coverage of the
  messages it happened to see. At the end, the watcher's cursors are compared with what
  Telegram says the latest message id is, per channel.

  **Memory flat.** The runner has limited RAM. A slow leak does not show up in a
  five-minute test and kills a six-hour one, so RSS is sampled every minute and the
  trend is reported rather than just the peak.

  **File descriptors flat.** An httpx client or a database cursor left open per message
  is invisible until the process hits the limit and every subsequent call fails.

  **No duplicate emails.** Checked against the notifications table, not against intent.

  **Cost within budget.** Extrapolated from the measured window to 24 hours.

Everything is written to a JSONL timeline as it happens, so a soak that dies at hour
five still produces five hours of evidence.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path(__file__).resolve().parent
for p in (AGENT, AGENT.parent, AGENT / "delivery", AGENT / "templates"):
    sys.path.insert(0, str(p))

import db as adb                    # noqa: E402
import orchestrator as orc          # noqa: E402
from core import resilience  # noqa: E402  explicit: a vertical may have one too
from core import samples  # noqa: E402  explicit: a vertical may have one too
from core import selfreport  # noqa: E402  explicit: a vertical may have one too
import send as sender               # noqa: E402
from core import watcher  # noqa: E402  explicit: a vertical may have one too
from engine import db as edb        # noqa: E402

TIMELINE = AGENT / "bench" / "soak_timeline.jsonl"
RESULT = AGENT / "bench" / "soak_result.json"
SAMPLE_S = 60


def _rss_mb() -> float:
    """Resident memory, without requiring psutil."""
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1e6, 1)
    except ImportError:
        pass
    try:                                # Windows
        import ctypes
        import ctypes.wintypes as w

        class PMC(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        c = PMC()
        c.cb = ctypes.sizeof(c)
        ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return round(c.WorkingSetSize / 1e6, 1)
    except Exception:
        return 0.0


def _fds() -> int:
    """Open handles. A per-message leak is invisible until the limit is hit."""
    try:
        import psutil
        return psutil.Process().num_handles() if os.name == "nt" \
            else psutil.Process().num_fds()
    except Exception:
        pass
    try:
        import ctypes
        n = ctypes.c_ulong()
        ctypes.windll.kernel32.GetProcessHandleCount(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(n))
        return int(n.value)
    except Exception:
        return 0


def snapshot(started: float, worker: str) -> dict:
    # Window = the whole run so far, not a fixed hour. Reporting `open_errors` over the
    # last hour meant a six-hour soak ended claiming zero while 60 errors from hours one
    # to four sat unresolved — the gate checks all-time and caught the discrepancy.
    hours = max(1, int((time.time() - started) / 3600) + 1)
    s = resilience.daily_stats(hours)
    return {"at": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": int(time.time() - started),
            "rss_mb": _rss_mb(), "handles": _fds(),
            "seen": s["seen"], "notified": s["notified"], "discarded": s["discarded"],
            "escalated": s["escalated"], "undecided": s["undecided"],
            "emails_sent": s["emails_sent"], "model_calls": s["model_calls"],
            "cost_usd": s["cost_usd"], "open_errors": s["open_errors"],
            "heartbeat": resilience.last_beat(worker).get("age_s")}


async def verify_against_telegram(account: str = "india") -> dict:
    """Ask TELEGRAM what the latest message id is, per channel, and compare.

    This is the check that cannot be faked by our own bookkeeping: a gap between the
    cursor and Telegram's own latest id is a message we never saw.
    """
    try:
        from engine.sources.telegram import client as tgclient
    except Exception as e:
        return {"checked": 0, "error": f"telegram client unavailable: {e}"[:120]}

    chans = watcher.watched_channels()
    gaps, checked = [], 0
    try:
        async with tgclient.connected(account) as (tg, _row):
            for cid, meta in list(chans.items())[:60]:
                try:
                    msgs = await tg.get_messages(int(cid), limit=1)
                except Exception:
                    continue
                if not msgs:
                    continue
                latest = msgs[0].id
                cursor = adb.cursor_for(int(cid))
                checked += 1
                if cursor and latest > cursor:
                    gaps.append({"channel": meta.get("title", str(cid))[:40],
                                 "telegram_latest": latest, "our_cursor": cursor,
                                 "behind_by": latest - cursor})
    except Exception as e:
        return {"checked": checked, "error": f"{type(e).__name__}: {e}"[:140],
                "gaps": gaps}
    return {"checked": checked, "gaps": gaps, "behind": len(gaps)}


def duplicate_emails(hours: float = 12.0) -> dict:
    """Duplicates sent WITHIN THIS RUN.

    A fixed 12-hour window kept re-reporting sends from before a fix, so a clean run
    looked identical to the broken one it was verifying.
    """
    dupes = edb.fetch_all(
        """SELECT subject, count(*) n FROM content.notifications
            WHERE status = 'sent'
              AND sent_at > now() - (%s || ' hours')::interval
            GROUP BY 1 HAVING count(*) > 1""", (max(0.1, hours),))
    return {"duplicate_subjects": len(dupes),
            "detail": [{"subject": d["subject"][:60], "times": d["n"]} for d in dupes[:5]]}


def analyse(samples_: list) -> dict:
    if not samples_:
        return {"error": "no samples"}
    rss = [s["rss_mb"] for s in samples_ if s["rss_mb"]]
    fds = [s["handles"] for s in samples_ if s["handles"]]
    hours = max(1e-6, samples_[-1]["elapsed_s"] / 3600)
    cost = samples_[-1]["cost_usd"]

    def trend(xs):
        if len(xs) < 4:
            return 0.0
        half = len(xs) // 2
        return round(sum(xs[half:]) / len(xs[half:]) - sum(xs[:half]) / len(xs[:half]), 1)

    return {"samples": len(samples_), "hours": round(hours, 2),
            "rss_start_mb": rss[0] if rss else 0, "rss_peak_mb": max(rss) if rss else 0,
            "rss_end_mb": rss[-1] if rss else 0, "rss_drift_mb": trend(rss),
            "handles_start": fds[0] if fds else 0,
            "handles_end": fds[-1] if fds else 0, "handles_drift": trend(fds),
            "messages_seen": samples_[-1]["seen"],
            "emails_sent": samples_[-1]["emails_sent"],
            "model_calls": samples_[-1]["model_calls"],
            "cost_window_usd": cost,
            "cost_24h_projected_usd": round(cost / hours * 24, 3),
            "open_errors": samples_[-1]["open_errors"]}


async def run(hours: float, account: str = "india", drain_every: int = 5) -> dict:
    """Watch, decide, deliver, and sample the process — for as long as asked."""
    adb.migrate()
    worker = f"soak-{os.getpid()}"
    started = time.time()
    deadline = started + hours * 3600
    TIMELINE.write_text("", encoding="utf-8")
    fh = TIMELINE.open("a", encoding="utf-8")
    ticks = 0

    print(f"soak: {hours}h, worker {worker}, sampling every {SAMPLE_S}s", flush=True)
    try:
        while time.time() < deadline:
            ticks += 1
            resilience.beat(worker, f"soak tick {ticks}")
            # Watch for a slice, then drain whatever arrived through the pipeline.
            try:
                await watcher.run(account=account,
                                  duration_s=min(SAMPLE_S, 60))
            except Exception as e:
                resilience.fail("soak:watcher", type(e).__name__, str(e))

            if ticks % drain_every == 0:
                try:
                    await orc.run(limit=60, concurrency=4, production_only=True)
                    sender.run(40)
                except Exception as e:
                    resilience.fail("soak:pipeline", type(e).__name__, str(e))
                try:
                    selfreport.escalate_failures()
                except Exception:
                    pass

            snap = snapshot(started, worker)
            fh.write(json.dumps(snap) + "\n")
            fh.flush()
            if ticks % 5 == 0:
                print(f"  {snap['elapsed_s'] // 60:>4}m  rss {snap['rss_mb']}MB  "
                      f"handles {snap['handles']}  seen {snap['seen']}  "
                      f"emails {snap['emails_sent']}  ${snap['cost_usd']}", flush=True)
    finally:
        fh.close()

    rows = [json.loads(x) for x in TIMELINE.read_text(encoding="utf-8").splitlines() if x]
    out = analyse(rows)
    hours = out.get("hours", 1)
    out["telegram_verification"] = await verify_against_telegram(account)
    out["duplicates"] = duplicate_emails(out.get("hours", 12.0))
    out["open_errors_whole_run"] = edb.fetch_one(
        "SELECT count(*) n FROM content.agent_errors WHERE NOT resolved "
        "AND created_at > now() - (%s || ' hours')::interval",
        (max(1, int(hours) + 1),))["n"]
    out["undecided"] = edb.fetch_one(
        "SELECT count(*) n FROM content.agent_events WHERE decision IS NULL AND "
        + resilience.NOT_TEST)["n"]
    out["worker"] = worker
    RESULT.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m agent.soak")
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--account", default="india")
    a = ap.parse_args()
    r = asyncio.run(run(a.hours, a.account))
    print(json.dumps(r, indent=1, default=str))
