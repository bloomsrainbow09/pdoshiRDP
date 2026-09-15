"""The unattended entry point: everything the runner needs, under one lease.

Four things have to happen continuously and nothing ran them together. `watcher.run()`
captures; `orchestrator.run()` drains what it captured; `send.run()` turns decisions into
email; `selfreport` says once a day whether any of it worked. This module is the loop that
owns all four, and the clean shutdown that hands the Telegram lease to the next runner.

**Why the sleep at the top is the most important line here.** The EC2 dispatches a fresh
runner at the old one's t = 350 min, but the old one lives to t ≈ 359 — a measured 9.7
minute overlap (#485 started 00:24:09, #484 ended 00:33:49). If two runners connect to the
same Telegram account, Telegram can revoke the auth key, and recovery needs a human with a
phone. That would permanently end the unattended guarantee this whole system exists for.

So: sleep past the overlap, then acquire the lease with a retry loop rather than a single
attempt. `watcher.run()` refuses and returns when another watcher holds the lease, which is
correct behaviour — but refuse-and-exit under a restart policy is a tight crash loop, so
the waiting happens here instead.

    t≈3    container starts, sleeps
    t≈9.7  old runner exits, releases the lease
    t≈14.7 worst case: old runner was HARD-KILLED and never released; its last heartbeat
           was ≤9.7 and the lease TTL is 300 s, so it expires here
    t=15   wake, acquire, replay the gap, go live
    t=345  clean exit, release the lease
    t≈358  runner dies

The 20-minute gap costs no messages: `replay_gap()` runs on every start and closes the
window from the stored cursor to now. The soak verified that against Telegram's own message
ids — 41 channels, zero gaps.

**Spend is metered but not capped.** Every model call is already recorded in
`content.agent_runs`; this logs the running total each cycle so it is visible in the run
log. There is deliberately no ceiling — processing every message is the point.

Usage:
    python -m core.supervisor --seconds 19800          # one runner lifetime
    python -m core.supervisor --seconds 600 --no-sleep # local smoke test
"""

import argparse
import asyncio
import os
import signal
import sys
import time
import uuid
from pathlib import Path

CORE = Path(__file__).resolve().parent
ROOT = CORE.parent
for _p in (str(ROOT), str(CORE), str(CORE / "subagents"), str(CORE / "delivery")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db as adb                                    # noqa: E402
from core import active, registry, resilience       # noqa: E402
from core import orchestrator, selfreport, watcher  # noqa: E402
from core.delivery import send                      # noqa: E402

# Past the 9.7-minute overlap AND past the 300 s lease TTL of a runner that was killed
# rather than exiting cleanly. PROMPTS.md P15 specifies 600 s; it assumed a ~5 minute
# overlap, and the measured one is 9.7.
HANDOFF_SLEEP_S = 720
LEASE_RETRY_S = 30
LEASE_RETRY_FOR_S = 600

DRAIN_EVERY_S = 30
DELIVER_EVERY_S = 60
REPORT_EVERY_S = 900          # the daily report is date-keyed; this only checks whether due

WORKER = os.environ.get("WORKER_ID") or f"sup-{uuid.uuid4().hex[:8]}"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] supervisor: {msg}", flush=True)


async def _run_watcher(vertical: str, account: str, seconds: int) -> dict:
    """Start the watcher, retrying while another runner still holds the lease.

    The retry wraps `watcher.run()` itself rather than probing the lease first. An
    earlier version acquired the lease here, released it, and handed over to
    `watcher.run()` to acquire it again — which left a window, however small, in which
    the overlapping runner could take it between the release and the re-acquire. The
    lease exists to make that impossible, so it must be taken exactly once, by the code
    that holds it.

    `watcher.run()` returns `{"refused": True}` rather than raising when the lease is
    held. That is the signal to wait, not to fail: a runner that cannot get the lease is
    one whose predecessor is still alive, and the right response is to leave it alone.
    """
    deadline = time.time() + LEASE_RETRY_FOR_S
    attempt = 0
    while True:
        stats = await watcher.run(vertical, account, duration_s=seconds)
        if not stats.get("refused"):
            return stats
        attempt += 1
        if time.time() >= deadline:
            log(f"lease for '{account}' still held after {LEASE_RETRY_FOR_S}s "
                f"({attempt} attempts) — the previous runner is still alive. Exiting 0 "
                f"rather than forcing; Docker's restart policy will try again.")
            return {"refused": True, "attempts": attempt}
        log(f"lease held by another worker; retry {attempt} in {LEASE_RETRY_S}s")
        await asyncio.sleep(LEASE_RETRY_S)


async def _every(seconds: int, name: str, fn, stop: asyncio.Event) -> None:
    """Run `fn` on a fixed cadence until `stop`. One loop's failure never ends the run.

    Each iteration is wrapped because these four tasks are peers: a transient Supabase
    blip in the delivery loop must not take the watcher down with it, and vice versa.
    """
    while not stop.is_set():
        try:
            out = fn()
            if asyncio.iscoroutine(out):
                out = await out
            if out:
                log(f"{name}: {out}")
        except Exception as e:
            log(f"{name} FAILED {type(e).__name__}: {str(e)[:160]}")
            try:
                resilience.fail(f"supervisor.{name}", type(e).__name__, str(e)[:400])
            except Exception:
                pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


def _spend_line() -> str:
    try:
        s = adb.spend_since(24)
        return (f"{s['calls']} calls / {s['failures']} failures / "
                f"${float(s['usd']):.4f} in 24h")
    except Exception as e:
        return f"(spend unavailable: {type(e).__name__})"


async def run(seconds: int, vertical: str | None = None,
              account: str | None = None, do_sleep: bool = True) -> dict:
    v = vertical or active.name()
    plugin = registry.current()
    cfg_account = account or watcher.verticals.load(v).get("source", {}).get("account")
    log(f"vertical={v} plugin={plugin.name} account={cfg_account} worker={WORKER}")

    if do_sleep:
        log(f"sleeping {HANDOFF_SLEEP_S}s before touching Telegram — the previous "
            f"runner overlaps this one by ~9.7 min and two clients on one session can "
            f"get the auth key revoked")
        await asyncio.sleep(HANDOFF_SLEEP_S)
        # `--seconds` is the LIVE window, not the container lifetime. Subtracting the
        # sleep from it as well used to end the watcher at t=333 while the keep-alive
        # ran to t=358 — 25 minutes of coverage discarded on every single cycle.


    stop = asyncio.Event()
    # NOT registered here. `watcher.run()` installs its own SIGTERM handler, and
    # `add_signal_handler` REPLACES rather than chains — so whichever registers last
    # wins. It happened to be the watcher, which is the outcome we want (SIGTERM must
    # reach the thing holding the Telegram lease so it releases on the way out), but by
    # accident. Leaving it to the watcher makes that explicit: `docker stop -t 60`
    # sends SIGTERM, the watcher exits cleanly, releases the lease, and the companion
    # loops below are cancelled in the `finally`. If both registered, the winner would
    # depend on start order.

    log(f"going live for {seconds}s ({seconds/60:.0f} min)")
    t0 = time.time()

    async def heartbeat():
        resilience.beat(WORKER, f"supervisor {v}")
        return None

    tasks = [
        asyncio.create_task(_run_watcher(v, cfg_account, seconds), name="watcher"),
        asyncio.create_task(_every(DRAIN_EVERY_S, "drain",
                                   lambda: orchestrator.run(limit=200), stop), name="drain"),
        asyncio.create_task(_every(DELIVER_EVERY_S, "deliver",
                                   lambda: send.run(limit=50), stop), name="deliver"),
        asyncio.create_task(_every(REPORT_EVERY_S, "report", _report, stop), name="report"),
        asyncio.create_task(_every(60, "heartbeat", heartbeat, stop), name="heartbeat"),
    ]

    try:
        # The watcher owns the clock: it exits on its own `duration_s`, and everything
        # else is a companion loop that stops when it does.
        stats = await tasks[0]
    finally:
        stop.set()
        for t in tasks[1:]:
            t.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)

    # One last drain and deliver, so anything captured in the final seconds still goes out
    # rather than waiting for the next runner.
    log("final drain + deliver before exit")
    try:
        await orchestrator.run(limit=500)
        send.run(limit=200)
    except Exception as e:
        log(f"final pass FAILED {type(e).__name__}: {str(e)[:160]}")

    out = {**(stats or {}), "worker": WORKER, "ran_s": int(time.time() - t0),
           "spend": _spend_line()}
    log(f"stopped cleanly — {out}")
    return out


def _report():
    """Daily report and failure escalation. Both are idempotent and self-scheduling."""
    a = selfreport.send_daily(24)
    b = selfreport.escalate_failures()
    bits = []
    if a and not a.get("skipped"):
        bits.append(f"daily report {a}")
    if b and b.get("sent"):
        bits.append(f"escalated {b}")
    bits.append(_spend_line())
    return " | ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=int, default=19800,
                    help="how long to stay live (default 19800 = 330 min)")
    ap.add_argument("--vertical", default=None, help=f"default: {active.name()}")
    ap.add_argument("--account", default=None, help="default: the vertical's own")
    ap.add_argument("--no-sleep", action="store_true",
                    help="skip the handoff sleep — LOCAL TESTING ONLY. On a runner this "
                         "risks two clients on one Telegram session.")
    a = ap.parse_args()

    adb.migrate()
    out = asyncio.run(run(a.seconds, a.vertical, a.account, do_sleep=not a.no_sleep))
    return 1 if out.get("refused") else 0


if __name__ == "__main__":
    raise SystemExit(main())
