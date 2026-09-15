"""Check what a channel claims against what the market actually says.

A channel posting `BUY X target 500` under "for educational purposes only" is making a
claim about the world. The disclaimer is a legal shield for them; it tells the reader
nothing. Passing the call through with a politely-worded caveat accepts the framing.

So instead of framing it, we check it. Two free, keyless sources that work from a GitHub
runner with no account and no cost:

  **Yahoo Finance** — last price, previous close, day range, 52-week range. Enough to
  answer the questions that actually matter about a call: is the entry already gone, is
  the target beyond anything this share has ever done, and how much is at risk between
  the entry and the stop-loss.

  **Google News RSS** — recent headlines for the company. Enough to answer whether there
  is news behind the call or whether somebody is just posting levels.

What this deliberately does NOT do is form a view. It reports the price, the ranges and
the headlines, and states plainly where the call and the market disagree. "Their target
is above the 52-week high" is a fact. "This is a bad trade" is not ours to say.

Everything degrades to silence rather than to a guess: if the network is down or the
symbol cannot be resolved, the email says the claim could not be checked. It never
blocks delivery — the reader's own message reaches them either way.
"""

import html as _html
import json
import re
import sys
import time
import urllib.parse as up
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT))
sys.path.insert(0, str(AGENT.parent))

import httpx  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 12.0
CACHE_S = 900            # a quote is good for fifteen minutes; news for an hour
NEWS_CACHE_S = 3600

_cache: dict = {}

# Indices are not ".NS" tickers. Channels write them a dozen ways.
INDEX = {
    "NIFTY": "^NSEI", "NIFTY50": "^NSEI", "NIFTY 50": "^NSEI",
    "BANKNIFTY": "^NSEBANK", "BANK NIFTY": "^NSEBANK", "NIFTYBANK": "^NSEBANK",
    "FINNIFTY": "^CNXFIN", "SENSEX": "^BSESN", "MIDCPNIFTY": "^NSEMDCP50",
}

# Words that sit next to a symbol and are not part of it.
_NOISE = re.compile(
    r"(?i)\b(fut|future|futures|cash|eq|equity|spot|ce|pe|call|put|weekly|monthly|"
    r"expiry|lot|qty)\b")


def _cached(key: str, ttl: float):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _store(key: str, value):
    _cache[key] = (time.time(), value)
    return value


def symbol_for(raw: str) -> tuple:
    """(yahoo_ticker, kind) for whatever the channel wrote, or (None, reason).

    Channels write "NATIONALUM", "ASIAN PAINT NOVEMBER FUTURE", "BANKNIFTY 48000 CE".
    Options and futures are deliberately NOT resolved to the underlying: a call on a
    48000 strike is not a call on the index, and quoting the index price beside it would
    be worse than quoting nothing.
    """
    s = (raw or "").strip().upper()
    if not s:
        return None, "no symbol given"
    if re.search(r"(?i)\b\d{3,6}\s*(CE|PE)\b", s) or re.search(r"(?i)\bstrike\b", s):
        return None, "an option contract — its price does not follow the share directly"
    bare = _NOISE.sub(" ", s)
    # Month names go wherever they sit. "Asian Paint November Future" resolved to
    # ASIANPAINTNOVEMBER.NS because the month only matched when a digit preceded it.
    bare = re.sub(r"\b(19|20)\d{2}\b", " ", bare)
    bare = re.sub(r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|"
                  r"SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER|JAN|FEB|MAR|APR|JUN|JUL|"
                  r"AUG|SEPT|SEP|OCT|NOV|DEC)\b", " ", bare)
    bare = re.sub(r"[^A-Z0-9& ]+", " ", bare)
    bare = re.sub(r"\s+", " ", bare).strip()
    if not bare:
        return None, "no symbol given"
    if bare in INDEX:
        return INDEX[bare], "index"
    token = bare.replace(" ", "")
    if len(token) < 2 or len(token) > 20:
        return None, "symbol not recognised"
    return f"{token}.NS", "equity"


def search_ticker(name: str) -> list:
    """Ask Yahoo which listed companies match this name. Never raises.

    Guessing `SYMBOL + ".NS"` works until a company changes its listing. Tata Motors
    demerged and `TATAMOTORS.NS` now 404s, so a call naming it looked unverifiable when
    the real answer is that it became two companies. Searching finds them; deciding
    which one the channel meant is not ours to do.
    """
    n = (name or "").strip()
    if len(n) < 3:
        return []
    hit = _cached(f"s:{n.lower()}", NEWS_CACHE_S)
    if hit is not None:
        return hit
    try:
        r = httpx.get("https://query2.finance.yahoo.com/v1/finance/search",
                      params={"q": n, "quotesCount": 8, "newsCount": 0},
                      headers=UA, timeout=TIMEOUT)
        out, seen = [], set()
        for q in r.json().get("quotes", []):
            if str(q.get("exchange")) != "NSI":       # NSE only; BSE duplicates it
                continue
            sym = q.get("symbol")
            if not sym or sym in seen or sym.endswith("-BL.NS"):
                continue
            seen.add(sym)
            out.append({"ticker": sym,
                        "name": q.get("longname") or q.get("shortname") or ""})
        return _store(f"s:{n.lower()}", out)
    except Exception:
        return _store(f"s:{n.lower()}", [])


def quote(raw_symbol: str, company: str = "") -> dict:
    """Live price and ranges from Yahoo Finance. Never raises."""
    ticker, kind = symbol_for(raw_symbol)
    if not ticker:
        return {"ok": False, "why": kind, "symbol": raw_symbol}
    hit = _cached(f"q:{ticker}", CACHE_S)
    if hit:
        return hit

    for t in ([ticker, ticker.replace(".NS", ".BO")] if ticker.endswith(".NS")
              else [ticker]):
        try:
            r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}",
                          params={"range": "1mo", "interval": "1d"},
                          headers=UA, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            m = (r.json().get("chart", {}).get("result") or [{}])[0].get("meta") or {}
            if not m.get("regularMarketPrice"):
                continue
            out = {"ok": True, "symbol": raw_symbol, "ticker": t, "kind": kind,
                   "price": float(m["regularMarketPrice"]),
                   "prev_close": _f(m.get("chartPreviousClose")),
                   "day_low": _f(m.get("regularMarketDayLow")),
                   "day_high": _f(m.get("regularMarketDayHigh")),
                   "year_low": _f(m.get("fiftyTwoWeekLow")),
                   "year_high": _f(m.get("fiftyTwoWeekHigh")),
                   "currency": m.get("currency") or "INR",
                   "exchange": m.get("fullExchangeName") or "",
                   "as_of": datetime.now(timezone.utc).isoformat(timespec="minutes")}
            if out["prev_close"]:
                out["change_pct"] = round(
                    (out["price"] - out["prev_close"]) / out["prev_close"] * 100, 2)
            return _store(f"q:{ticker}", out)
        except Exception:
            continue
    # The guessed ticker does not exist. Search by name before giving up.
    found = search_ticker(company or raw_symbol)
    if len(found) == 1:
        alt = quote(found[0]["ticker"])
        if alt.get("ok"):
            alt["resolved_from"] = raw_symbol
            return _store(f"q:{ticker}", alt)
    if len(found) > 1:
        names = ", ".join(f"{x['ticker'].replace('.NS', '')}" for x in found[:3])
        return _store(f"q:{ticker}", {
            "ok": False, "symbol": raw_symbol, "ticker": ticker, "split": True,
            "candidates": found[:3],
            "why": f"this name now matches more than one listed company ({names}), "
                   f"and the message does not say which"})
    return _store(f"q:{ticker}",
                  {"ok": False, "why": "no price found for this symbol",
                   "symbol": raw_symbol, "ticker": ticker})


def _f(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


# Pages that rank for a company name but report nothing. Quote tools, screeners and
# evergreen data pages are what a naive news search actually returns: the first three
# results for NATIONALUM were an option-chain widget, a shareholding-pattern table and a
# weekly outlook column. Citing those as "news this fortnight" would be worse than
# citing nothing, because it implies something happened.
_NOT_NEWS = re.compile(
    r"(?i)(option\s*chain|shareholding\s*pattern|share\s*price\s*(live|today)|"
    r"stock\s*price|live\s*(price|updates?)|outlook for the (week|day)|"
    r"price\s*target\s*calculator|technical\s*analysis\s*of|screener|"
    r"balance\s*sheet|financial\s*results\s*table|dividend\s*history|"
    r"\bchart\b.*\blive\b|moneycontrol\.com/india/stockpricequote)")


def headlines(name: str, limit: int = 4) -> list:
    """Recent headlines for a company, newest first. Never raises."""
    n = (name or "").strip()
    if len(n) < 3:
        return []
    hit = _cached(f"n:{n.lower()}", NEWS_CACHE_S)
    if hit is not None:
        return hit[:limit]
    try:
        q = up.quote(f'"{n}" when:14d')
        r = httpx.get(f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN"
                      f"&ceid=IN:en", headers=UA, timeout=TIMEOUT,
                      follow_redirects=True)
        items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
        out = []
        for it in items:
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
            d = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
            src = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
            if not t:
                continue
            title = _html.unescape(re.sub(r"\s+", " ", t.group(1))).strip()
            # Google appends " - Publisher" to every headline.
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
            if _NOT_NEWS.search(title) or len(title) < 18:
                continue
            out.append({"title": title[:130],
                        "source": _html.unescape(src.group(1) if src else "")[:40],
                        "when": (d.group(1) if d else "")[:16]})
        return _store(f"n:{n.lower()}", out)[:limit]
    except Exception:
        return _store(f"n:{n.lower()}", [])[:limit]


def _num(v):
    """First number in a level like "111-112.5" or "abv 1250"."""
    m = re.search(r"\d[\d,]*\.?\d*", str(v or ""))
    return float(m.group(0).replace(",", "")) if m else None


def check_call(fields: dict, company: str = "") -> dict:
    """Verify a trade call against the live market. Returns findings, never a verdict.

    Each finding is a fact the reader can act on: the entry has already gone, the target
    is beyond the 52-week high, the stop-loss risks this many rupees. Whether the trade
    is good is not ours to say — but whether the numbers still describe reality is
    checkable, and that is what the channel's disclaimer is designed to stop you asking.
    """
    sym = fields.get("symbol") or ""
    q = quote(sym, company)
    out = {"symbol": sym, "quote": q, "findings": [], "news": [],
           "checked": bool(q.get("ok"))}
    if not q.get("ok"):
        out["why"] = q.get("why", "could not be checked")
        return out

    price = q["price"]
    cur = "₹" if q["currency"] == "INR" else q["currency"] + " "
    entry, target, stop = (_num(fields.get("entry")), _num(fields.get("target")),
                           _num(fields.get("stop_loss")))
    direction = str(fields.get("direction") or "").lower()

    out["findings"].append(
        f"{sym} is trading at {cur}{price:,.2f} right now"
        + (f", {'up' if q.get('change_pct', 0) >= 0 else 'down'} "
           f"{abs(q['change_pct'])}% today" if q.get("change_pct") is not None else "")
        + ".")

    if entry:
        gap = (price - entry) / entry * 100
        if abs(gap) < 2:
            out["findings"].append(
                f"Their entry of {cur}{entry:,.2f} is about where it is trading now.")
        elif (direction == "buy" and gap > 2) or (direction == "sell" and gap < -2):
            out["findings"].append(
                f"The price has already moved past their entry of {cur}{entry:,.2f} "
                f"by {abs(gap):.0f}%. Acting now is not the trade they described.")
        else:
            out["findings"].append(
                f"Their entry of {cur}{entry:,.2f} is {abs(gap):.0f}% "
                f"{'below' if gap > 0 else 'above'} the current price.")

    # A call whose entry has moved by more than half is not a live call, it is an old
    # message resurfacing. Comparing its target with today's price produces confident
    # nonsense — "their target of Rs116 is -67% above today's price" — so say the one
    # true thing and stop.
    if entry and abs((price - entry) / entry) > 0.5:
        out["stale"] = True
        out["findings"].append(
            f"This looks like an old call. The share has moved "
            f"{abs((price - entry) / entry) * 100:.0f}% away from the entry price it "
            f"names, so the levels in it no longer describe this market.")
        out["news"] = headlines(company or sym)
        return out

    if target and q.get("year_high"):
        if direction != "sell" and target > q["year_high"]:
            out["findings"].append(
                f"Their target of {cur}{target:,.2f} is above the highest price this "
                f"share has reached in a year ({cur}{q['year_high']:,.2f}).")
        elif direction != "sell":
            move = (target - price) / price * 100
            out["findings"].append(
                f"Their target of {cur}{target:,.2f} is {abs(move):.0f}% "
                f"{'above' if move >= 0 else 'BELOW'} today's price. The 12-month "
                f"range is {cur}{q['year_low']:,.2f} to {cur}{q['year_high']:,.2f}.")

    if entry and stop:
        risk = abs(entry - stop)
        out["findings"].append(
            f"Between their entry and their stop-loss there is {cur}{risk:,.2f} a share "
            f"at risk — {risk / entry * 100:.1f}% of what you would put in.")

    name = company or sym
    out["news"] = headlines(name)
    if not out["news"]:
        out["findings"].append(
            f"No news about {name} in the last fortnight, so this looks like a call on "
            f"price movement rather than on anything that happened.")
    return out


def check_ipo(fields: dict) -> dict:
    """Corroborate an IPO against the open web rather than only against other channels.

    The in-folder cross-check (two channels naming the same company) catches a repost.
    It cannot catch six channels repeating one wrong number, which is exactly what
    happened when a GMP of 73 circulated against a tracked high of 50.
    """
    name = (fields.get("company") or "").strip()
    out = {"company": name, "news": [], "findings": [], "checked": bool(name)}
    if not name:
        out["why"] = "no company name to search for"
        return out
    out["news"] = headlines(f"{name} IPO", limit=4)
    if out["news"]:
        out["findings"].append(
            f"{len(out['news'])} news reports mention this IPO in the last fortnight.")
    else:
        out["findings"].append(
            "No news coverage of this IPO in the last fortnight. For a mainboard issue "
            "that is unusual — treat the details as unconfirmed until you see them on "
            "the registrar's or the exchange's own site.")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="python -m agent.market")
    ap.add_argument("symbol")
    ap.add_argument("--entry"), ap.add_argument("--target"), ap.add_argument("--stop")
    ap.add_argument("--direction", default="buy")
    a = ap.parse_args()
    print(json.dumps(check_call(
        {"symbol": a.symbol, "entry": a.entry, "target": a.target,
         "stop_loss": a.stop, "direction": a.direction}), indent=1))
