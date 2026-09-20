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

# Which verticals this runner CAPTURES, and which it DRAINS. They are separate on
# purpose and the defaults are deliberately asymmetric.
#
# Capture defaults to "*" — every enabled vertical drawing on this account. It has to be
# all-or-nothing at the client level anyway: only one client may hold a Telegram
# session's lease, so a second vertical on the same account cannot run its own watcher,
# and the choice is "one watcher for all of them" or "the others never get read". Widening
# capture is also cheap and safe: it writes rows to agent_events and sends nothing.
#
# Drain defaults to the ACTIVE vertical alone, because draining is what sends email. A
# niche added today has prompts that have never seen its own corpus — deals' taxonomy is
# marked `provisional: true` for exactly this reason — and letting it email on its first
# cycle would be untuned output to a real inbox. Capture it, accumulate the corpus,
# derive the taxonomy from real messages, then add it here.
#
#     WATCH_VERTICALS=*                 capture everything on this account (default)
#     DRAIN_VERTICALS=trading           email only trading (default: the active vertical)
#     DRAIN_VERTICALS=trading,movies    once a niche's prompts are proven
WATCH_VERTICALS = os.environ.get("WATCH_VERTICALS", "*").strip() or "*"
DRAIN_VERTICALS = [x for x in
                   (os.environ.get("DRAIN_VERTICALS") or "").split(",") if x.strip()]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] supervisor: {msg}", flush=True)


async def _run_watcher(vertical, account: str, seconds: int) -> dict:
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
    home = account or watcher.verticals.load_any(v).get("source", {}).get("account")

    watch = WATCH_VERTICALS if WATCH_VERTICALS != "*" else "*"
    drain = DRAIN_VERTICALS or [v]

    # Which ACCOUNTS to watch, not just which verticals.
    #
    # A lease covers one session, so one watcher covers one account — and a vertical may
    # span several. At least one here does, with two channels on its home account and
    # three on the other holding 98% of its files. Watching only the active vertical's
    # own account left that entire majority uncaptured.
    #
    # Different accounts are independent authorizations, so one client each is safe and
    # concurrent — it is sharing ONE session that is forbidden, not holding two.
    if watch == "*":
        accounts_map = {}
        for acct in sorted(watcher.accounts_with_verticals()):
            names = watcher.verticals_on(acct)
            if names:
                accounts_map[acct] = names
        if not accounts_map:
            accounts_map = {home: [v]}
    else:
        capturing = [x.strip() for x in watch.split(",") if x.strip()]
        accounts_map = {}
        for name in capturing:
            for acct in watcher.accounts_for(name):
                accounts_map.setdefault(acct, []).append(name)

    every = sorted({n for names in accounts_map.values() for n in names})
    log(f"vertical={v} plugin={plugin.name} home={home} worker={WORKER}")
    for acct, names in accounts_map.items():
        n = len(watcher.watched_channels(names, acct))
        log(f"  watching {acct:<8} {n:>3} channels  {names}")
    log(f"draining (emailing) {drain}")
    if set(every) - set(drain):
        log(f"  note: {sorted(set(every) - set(drain))} are captured but NOT "
            f"emailed — their rows accumulate for taxonomy derivation. Add them to "
            f"DRAIN_VERTICALS once their prompts are validated.")

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

    # One watcher per ACCOUNT, run concurrently. Each takes the lease for its own
    # session, so they never contend with each other — only with another process on the
    # same account, which is exactly what the lease is for.
    watchers = [
        asyncio.create_task(_run_watcher(names, acct, seconds), name=f"watch:{acct}")
        for acct, names in accounts_map.items()
    ]
    tasks = [
        *watchers,
        asyncio.create_task(_every(DRAIN_EVERY_S, "drain",
                                   lambda: orchestrator.run(limit=200, verticals=drain), stop), name="drain"),
        asyncio.create_task(_every(DELIVER_EVERY_S, "deliver",
                                   lambda: send.run(limit=50), stop), name="deliver"),
        asyncio.create_task(_every(REPORT_EVERY_S, "report", _report, stop), name="report"),
        # Park what nobody drains, on the same cadence as the report. Without it the
        # undecided count grows by ~2,000 a cycle and stops being a health signal — a
        # genuinely stuck trading row would be invisible among rows undecided by design.
        asyncio.create_task(_every(REPORT_EVERY_S, "park",
                                   lambda: adb.park_undrained(drain), stop), name="park"),
        asyncio.create_task(_every(60, "heartbeat", heartbeat, stop), name="heartbeat"),
    ]

    try:
        # The watchers own the clock: each exits on its own `duration_s`, and everything
        # else is a companion loop that stops when they do. Waiting for ALL of them
        # rather than the first matters now that there is more than one — returning on
        # the first would cancel the others mid-capture and strand their leases.
        done = await asyncio.gather(*watchers, return_exceptions=True)
        stats = {}
        for acct, r in zip(accounts_map, done):
            if isinstance(r, BaseException):
                log(f"watcher for '{acct}' FAILED {type(r).__name__}: {str(r)[:140]}")
                stats.setdefault("failed_accounts", []).append(acct)
                continue
            for k, val in (r or {}).items():
                if isinstance(val, int):
                    stats[k] = stats.get(k, 0) + val
            if r.get("refused"):
                stats.setdefault("refused_accounts", []).append(acct)
        # `refused` for the whole run only if EVERY account refused; one busy session
        # must not make the exit code claim nothing ran.
        stats["refused"] = len(stats.get("refused_accounts", [])) == len(accounts_map)
    finally:
        stop.set()
        companions = [t for t in tasks if t not in watchers]
        for t in companions:
            t.cancel()
        await asyncio.gather(*companions, return_exceptions=True)

    # One last drain and deliver, so anything captured in the final seconds still goes out
    # rather than waiting for the next runner.
    log("final drain + deliver before exit")
    try:
        await orchestrator.run(limit=500, verticals=drain)
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
