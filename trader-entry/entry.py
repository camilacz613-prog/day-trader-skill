#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches a setup bar by bar and says the
moment to get in. Standard library only. Long or short side, stock hours or a 24h market.
It never places an order.

    python3 entry.py check TSLA                          # opening-range breakout, 5-minute bars
    python3 entry.py check TSLA --now "2026-10-19 10:15" --offline TSLA.json
    python3 entry.py check TSLA --setup level --level 371.20    # first close above a price
    python3 entry.py check TSLA --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch TSLA --every 30 --until 15:30  # one line per closed bar
    python3 entry.py watchlist                            # every hit in trader/watchlist.json

Setups:
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 9:30, 9:35 and 9:40 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars (the 9:30 bar carries the opening
         auction, so 1.0 is already a high bar).

A trigger's time is the trigger bar's CLOSE time (the 10:10 bar closes at
10:15 AM) and its price is that close, to the cent. Only bars on the interval
grid count (Yahoo's live tick, e.g. 10:49:45, is dropped) and a bar counts only
once it has closed: now >= its start + the interval.

Statuses: triggered; waiting (not open yet, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default 15:30;
on a 1:00 p.m. early close the cutoff moves up three hours with the close);
no-data (the market is closed today or Yahoo sent no bars).

trader/config.json (optional): "no_entries_before" (09:45) and
"no_entries_after" (15:30), New York time.

Short side: --side sell (or short) mirrors every rule. An orb triggers on the first
completed bar that CLOSES BELOW the range low; a level setup on the first close below
--level. The result and entry.json carry "side": "sell". A short needs a broker that
lends the shares (and margin): trader-risk blocks it unless "allow_short" is true.

Named sessions for gold: --session london and --session ny are the two cash-market opens that
move gold, each with its own 30-minute opening range and its own flat-by:
  london   08:00 to 14:30 London time  = 09:00 to 15:30 Berlin (always, London and Berlin switch
           to summer time on the same day)
  ny       09:30 to 16:00 New York time = 15:30 to 22:00 Berlin, except in the weeks when the US
           and Europe are on different clocks (about 3 weeks in March and 1 in late October),
           when the New York open falls at 14:30 Berlin
The session is looked up for each date with its own time zone, so the result carries both the
New York and the Berlin times. --tz berlin|london|newyork with --anchor/--close sets any other
window in that zone, for example --tz berlin --anchor 15:30 --close 22:00 pins the Berlin clock.
--or-minutes defaults to 30 with --session.

24h markets: --market 24h trades a symbol that has no stock-market open (gold futures
GC=F, forex EURUSD=X, crypto BTC-USD). There is no 9:30 open, so YOU name the session:
--anchor HH:MM is where the opening range starts (New York time, default 08:20, the
COMEX gold open; 03:00 is the London open, 18:00 the CME Globex open) and --close HH:MM
is where the session ends (default 17:00, the CME daily break; a close at or before the
anchor means the next day). Entries stop 30 minutes before the close unless config sets
"no_entries_after_24h" (and "no_entries_before_24h"). --weekends keeps Saturday and Sunday
(crypto); without it the week runs Sunday 18:00 to Friday 17:00 New York time, like
futures and forex. There is no holiday calendar for these markets: a holiday shows up as
missing bars, and the result says so.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query2.finance.yahoo.com/v8/finance/chart/TSLA?range=1d&interval=5m
  falls back to query1 on HTTP 429 or 5xx; a 429 gets one retry after a pause.
  --offline PATH reads a saved chart JSON (.json or .json.gz) instead, or
  PATH/<TICKER>.json when PATH is a folder. Without --now, an offline file is
  read as of its own time (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:45", "no_entries_after": "15:30",
            "no_entries_before_24h": None, "no_entries_after_24h": None}
DEFAULT_ANCHOR = "08:20"
DEFAULT_CLOSE_24H = "17:00"
LAST_ENTRY_BEFORE_CLOSE = 30      # minutes: 15:30 before a 16:00 close, and the 24h default
EPOCH = datetime.datetime(1970, 1, 1)

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
CALENDAR_YEARS = (2026, 2027)
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


ZONES = {"newyork": "America/New_York", "london": "Europe/London", "berlin": "Europe/Berlin"}
SESSIONS = {
    "london": {"tz": "london", "open": "08:00", "close": "14:30", "label": "London"},
    "ny": {"tz": "newyork", "open": "09:30", "close": "16:00", "label": "New York"},
}


def _last_sunday(year, month):
    d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1) if month < 12 else datetime.date(year, 12, 31)
    return d - datetime.timedelta(days=(d.weekday() + 1) % 7)


def zone_offset(utc, zone):
    """Hours from UTC for newyork, london or berlin at this naive UTC time. Europe switches at
    01:00 UTC on the last Sundays of March and October (rule since 1996); the US at 2:00 local on
    the second Sunday of March and the first Sunday of November."""
    if zone == "newyork":
        return eastern_offset(utc)
    start = datetime.datetime.combine(_last_sunday(utc.year, 3), datetime.time(1, 0))
    end = datetime.datetime.combine(_last_sunday(utc.year, 10), datetime.time(1, 0))
    summer = start <= utc.replace(tzinfo=None) < end
    return (1 if summer else 0) + (1 if zone == "berlin" else 0)


def zone_to_utc(local, zone):
    """A naive wall-clock time in that zone -> naive UTC (a time that does not exist when the clocks
    jump forward is read with the winter offset)."""
    for off in (-5, -4, 0, 1, 2):
        utc = local - datetime.timedelta(hours=off)
        if zone_offset(utc, zone) == off:
            return utc
    return local - datetime.timedelta(hours=zone_offset(local, zone))


def from_zone(local, zone):
    """A wall-clock time in `zone` -> New York wall-clock time."""
    return to_eastern(zone_to_utc(local, zone))


def in_zone(et, zone):
    """A New York wall-clock time -> the wall-clock time in `zone`."""
    utc = eastern_to_utc(et)
    return utc + datetime.timedelta(hours=zone_offset(utc, zone))


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-10-19 10:15" (or ...T10:15, or just 10:15 for
    today) is New York time; an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" ET"):
        t = t[:-3].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_eastern(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise EntryError('--now wants a New York time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants a New York time like 15:30, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """10:10, 09:45: the contract's wall-clock form."""
    return "%02d:%02d" % (t.hour, t.minute)


def short(t):
    """9:45, 10:10: for sentences."""
    return "%d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def make_market(kind="stock", anchor=None, close=None, weekends=False, tz=None, session=None):
    """The market the rules run on. "stock" is the NYSE session; "24h" is a session you name
    for futures, forex and crypto: an anchor and a close, in New York time or in the zone
    `tz` (newyork, london, berlin), or one of the named sessions (london, ny)."""
    if kind not in ("stock", "24h"):
        raise EntryError("--market is stock or 24h")
    if session is not None and session not in SESSIONS:
        raise EntryError("--session is london or ny")
    if tz is not None and tz not in ZONES:
        raise EntryError("--tz is newyork, london or berlin")
    if session is not None:
        kind = "24h"
    if kind == "stock":
        return {"kind": "stock"}
    base = SESSIONS.get(session) or {}
    zone = tz or base.get("tz") or "newyork"
    if session is not None and tz is not None and tz != base["tz"]:
        raise EntryError("--session %s is a %s time session: drop --tz, or use --tz with --anchor and --close"
                         % (session, ZONES[base["tz"]]))
    m = {"kind": "24h", "anchor": parse_hhmm(anchor or base.get("open") or DEFAULT_ANCHOR, "--anchor"),
         "close": parse_hhmm(close or base.get("close") or DEFAULT_CLOSE_24H, "--close"),
         "weekends": bool(weekends), "tz": zone}
    if session is not None:
        m["name"] = session
    return m


STOCK = {"kind": "stock"}


def is_24h(market):
    return bool(market) and market.get("kind") == "24h"


def _in_market_week(t):
    """Futures and forex: Sunday 18:00 to Friday 17:00 New York time."""
    wd = t.weekday()
    if wd == 5:
        return False
    if wd == 6 and t.time() < datetime.time(18, 0):
        return False
    if wd == 4 and t.time() > datetime.time(17, 0):
        return False
    return True


def session_window(d, market=None):
    """(open_dt, close_dt) as New York datetimes for the session that starts on date d (the date
    in the session's own zone)."""
    if is_24h(market):
        open_dt = datetime.datetime.combine(d, market["anchor"])
        close_dt = datetime.datetime.combine(d, market["close"])
        if close_dt <= open_dt:
            close_dt += datetime.timedelta(days=1)
        zone = market.get("tz") or "newyork"
        if zone != "newyork":
            open_dt, close_dt = from_zone(open_dt, zone), from_zone(close_dt, zone)
        return open_dt, close_dt
    return (datetime.datetime.combine(d, OPEN),
            datetime.datetime.combine(d, EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE))


def trading_day(d, market=None):
    """(is_trading_day, reason). Stocks: weekends and the NYSE holiday list. 24h: the
    market week, or every day with --weekends; no holiday list."""
    if is_24h(market):
        if market["weekends"]:
            return True, ""
        open_dt, close_dt = session_window(d, market)
        if not (_in_market_week(open_dt) and _in_market_week(close_dt - datetime.timedelta(seconds=1))):
            return False, "outside the futures and forex week (Sunday 18:00 to Friday 17:00 New York time)"
        return True, ""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def session(d, market=None):
    """(open_time, close_time) in New York time, or None if closed all day. For a 24h
    market close_time may be on the next day: use session_window for the datetimes."""
    ok, _ = trading_day(d, market)
    if not ok:
        return None
    if is_24h(market):
        o, c = session_window(d, market)
        return o.time(), c.time()
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


def session_day(local, market=None):
    """The date of the session that `local` falls in: today's, or yesterday's when a 24h
    session that began yesterday evening is still running."""
    d = local.date()
    if is_24h(market):
        for cand in (d - datetime.timedelta(days=1), d, d + datetime.timedelta(days=1)):
            if session(cand, market):
                o, c = session_window(cand, market)
                if o <= local < c:
                    return cand
        y = d - datetime.timedelta(days=1)
        if session(y, market) and session_window(y, market)[1] > local:
            return y
    return d


def next_open(d, market=None):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd, market):
            return "%s %s" % (nd.isoformat(), hm(session(nd, market)[0]))
        nd += datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(sym):
    return sym.upper().replace(".", "-")


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_symbol(sym), rng, interval)


def chart_range(day, today, interval, market=None):
    """The smallest Yahoo range that still holds the session of `day`. A 24h session
    can begin the evening before, so today's is read from the last 5 days."""
    back = (today - day).days
    if back <= 0:
        return "5d" if is_24h(market) else "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """GET a Yahoo path: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise EntryError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def parse_chart(text, sym):
    """Yahoo chart JSON v8 -> {symbol, meta, bars, prev_close, data_time}.
    Bars with a null close are dropped here; off-grid ticks later."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": str(meta.get("symbol") or sym).upper(), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        for name in (sym + ".json", sym + ".json.gz", yahoo_symbol(sym) + ".json", yahoo_symbol(sym) + ".json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def get_chart(sym, interval, day=None, offline=None, today=None, market=None):
    """Bars for one ticker, from Yahoo (range=1d for today) or a saved file."""
    sym = sym.upper()
    if offline:
        f = offline_file(offline, sym)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
    else:
        today = today or to_eastern(utc_now()).date()
        text = yahoo_get(chart_path(sym, chart_range(day or today, today, interval, market), interval))
    ch = parse_chart(text, sym)
    if ch["symbol"] != yahoo_symbol(sym):
        raise EntryError("the chart file holds %s, not %s" % (ch["symbol"], sym))
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
    for k in DEFAULTS:
        if cfg[k] is not None:
            parse_hhmm(cfg[k], k)
    return cfg


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def early_cutoff(day, t, close_dt):
    """A cutoff on an early-close day moves up with the close: 15:30 (30 minutes
    before a 16:00 close) becomes 12:30 before a 1:00 p.m. close. A cutoff that
    is already before the early close stays."""
    at = datetime.datetime.combine(day, t)
    if at < close_dt:
        return at
    moved = at - (datetime.datetime.combine(day, CLOSE) - close_dt)
    return moved if moved.time() > OPEN else close_dt


def _wall_after(open_dt, t):
    """The first moment at or after open_dt whose wall-clock time is t."""
    at = datetime.datetime.combine(open_dt.date(), t)
    return at if at >= open_dt else at + datetime.timedelta(days=1)


def session_block(market, open_dt, close_dt):
    """The session as trader/entry.json carries it: New York times for this date, plus the zone it was
    set in and the Berlin clock when it is not a New York time session."""
    if not is_24h(market):
        return {"kind": "stock"}
    b = {"kind": "24h", "open": hm(open_dt.time()), "close": hm(close_dt.time()),
         "weekends": bool(market["weekends"])}
    zone = market.get("tz") or "newyork"
    if zone != "newyork" or market.get("name"):
        b.update(tz=zone, local_open=hm(market["anchor"]), local_close=hm(market["close"]))
        if market.get("name"):
            b["name"] = market["name"]
    bo, bc = in_zone(open_dt, "berlin"), in_zone(close_dt, "berlin")
    b["berlin"] = {"open": hm(bo.time()), "close": hm(bc.time())}
    return b


def side_of(text):
    """--side -> "buy" or "sell"."""
    s = str(text or "buy").lower()
    if s in ("buy", "long"):
        return "buy"
    if s in ("sell", "short"):
        return "sell"
    raise EntryError("--side is buy or sell (short)")


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None, side="buy", market=None):
    """Pure function: the setup's status at now_utc from one session's bars.
    side "buy" waits for a close above the level, "sell" (short) for a close below it."""
    cfg = cfg or dict(DEFAULTS)
    market = market or STOCK
    side = side_of(side)
    short_side = side == "sell"
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day %s)" % (
            "low" if short_side else "high"))
    n_or = or_minutes * 60 // step
    local = to_eastern(now_utc)
    day = session_day(local, market)
    res = {"symbol": chart["symbol"], "setup": setup, "side": side, "date": day.isoformat(),
           "market": market["kind"], "interval": interval, "or_minutes": or_minutes,
           "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": chart.get("prev_close"),
           "calendar_known": is_24h(market) or day.year in CALENDAR_YEARS, "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = to_eastern(utc_of(dt)).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    ok, why = trading_day(day, market)
    if not ok:
        res.update(status="no-data", phase="closed",
                   reason=("the market is closed today (%s)" if is_24h(market) else "the NYSE is closed today (%s)") % why,
                   next_open=next_open(day, market))
        return res
    open_dt, close_dt = session_window(day, market)
    o, c = open_dt.time(), close_dt.time()
    if is_24h(market):
        early = False
        before_t = cfg.get("no_entries_before_24h")
        after_t = cfg.get("no_entries_after_24h")
        before = _wall_after(open_dt, parse_hhmm(before_t, "no_entries_before_24h")) if before_t else open_dt
        cutoff = (_wall_after(open_dt, parse_hhmm(after_t, "no_entries_after_24h")) if after_t
                  else close_dt - datetime.timedelta(minutes=LAST_ENTRY_BEFORE_CLOSE))
    else:
        early = c != CLOSE
        before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
        cutoff = early_cutoff(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after"), close_dt)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res["session"] = session_block(market, open_dt, close_dt)
    res.update(opens=hm(o), closes=hm(c), early_close=None if is_24h(market) else NYSE_EARLY_CLOSE.get(day.isoformat()),
               no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # Yahoo's live tick (10:49:45) is not a bar
        et = to_eastern(utc_of(b["t"]))
        if not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Yahoo has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
                res["lag_note"] = ("Yahoo's last trade is %s, before the %s bar's close: it counts once Yahoo "
                                   "catches up (a minute at most)" % (to_eastern(utc_of(dt)).strftime("%H:%M:%S"),
                                                                       short(b["et"])))
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        res.update(phase="pre-open", reason="%s opens at %s%s" % (
            "the session" if is_24h(market) else "the NYSE", short(open_dt),
            " and the %d-minute range is set at %s" % (or_minutes, short(or_end)) if setup == "orb" else ""))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end, "avg_volume": int(round(avg_vol))}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, short(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Yahoo sent no bars for the opening range")
            return res
        lvl = min(b["l"] for b in or_bars) if short_side else max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    def beyond(price):
        return price < lvl if short_side else price > lvl

    def pct_beyond(price):
        return 100.0 * ((1 - price / lvl) if short_side else (price / lvl - 1))

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if beyond(b["c"]):
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}
    word = "below" if short_side else "above"
    edge = "the range low" if short_side else "the range high"

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed %s %s" % (short(trig["et"]), word, edge if setup == "orb" else "the level"))
        res["trigger"] = {"time": ampm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(pct_beyond(trig["c"]))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if dt and utc_of(dt) < eastern_to_utc(trig["end"]):
            res["data_note"] = ("Yahoo's last trade is %s, before this bar closed: its close can still move a cent or two"
                                % to_eastern(utc_of(dt)).strftime("%H:%M:%S"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Yahoo sent no bars for %s yet" % day.isoformat())
        return res
    what = ("close %s %s $%.2f" % (word, edge, lvl)) if setup == "orb" else "close %s $%.2f" % (word, lvl)
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry%s" % (
                       what, short(cutoff), " (1:00 p.m. early close today)" if early else ""))
        return res
    res["phase"] = "open"
    if beyond(last["c"]) and last in thin:
        res["reason"] = "the %s bar closed %s at $%.2f but on %.2fx the range's volume, under your %.2fx" % (
            short(last["et"]), word, last["c"], last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif beyond(last["c"]) and last["end"] < before:
        res["reason"] = "the %s bar closed %s at $%.2f, but no entries before %s (config)" % (
            short(last["et"]), word, last["c"], short(before))
    else:
        gap = (last["c"] - lvl) if short_side else (lvl - last["c"])
        res["reason"] = "last close $%.2f (%s bar), $%.2f %s %s (%+.2f%%)" % (
            last["c"], short(last["et"]), abs(gap), ("over" if gap >= 0 else "under") if short_side
            else ("under" if gap >= 0 else "over"),
            ("%s $%.2f" % (edge, lvl)) if setup == "orb" else "the level $%.2f" % lvl,
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-entry", "symbol": res["symbol"], "setup": res["setup"], "side": res.get("side", "buy"),
            "date": res["date"], "market": res.get("market", "stock"),
            "session": res.get("session") or {"kind": "stock"}, "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
            "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
            "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": "yahoo", "checked_at": iso(now_utc), "reason": res.get("reason")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    word = "below" if res.get("side") == "sell" else "above"
    return "close %s $%.2f" % (word, res["level"]) if res["level"] is not None else "close %s a level" % word


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  %s %s ET  %s%s on %s bars (Yahoo, free, can lag)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"),
        "SHORT " if res.get("side") == "sell" else "", setup_name(res), bars)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high $%.2f, low $%.2f (%d of %d bars%s)." % (
            short(start), short(end), orr["high"], orr["low"], orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        sell = res.get("side") == "sell"
        out.append("TRIGGERED %s at $%.2f: the %s bar (bar %d) closed %s %s $%.2f (%+.2f%%%s)." % (
            t["time"], t["price"], short(datetime.datetime.strptime(t["bar_start"], "%H:%M")), t["bar_index"],
            "below" if sell else "above",
            ("the range low" if sell else "the range high") if res["setup"] == "orb" else "the level",
            res["level"], t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is $%.2f (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], res["last"]["close"], res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you %s: /trader-exit "
                   "(exit.py plan --from trader/entry.json)." % ("sell short" if sell else "buy"))
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming ($%.2f so far, closes %s); it does not count until it closes." % (
                f["bar_start"].lstrip("0"), f["price"], f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s New York." % res["next_open"])
    if res.get("early_close"):
        out.append("Early close today at 1:00 p.m. (%s): no new entries after %s." % (
            res["early_close"], res["no_entries_after"]))
    if res.get("market") == "24h":
        sess = res.get("session") or {}
        name = {"london": "London session, ", "ny": "New York session, "}.get(sess.get("name"), "")
        berlin = (" (%s to %s Berlin)" % (sess["berlin"]["open"], sess["berlin"]["close"])) if sess.get("berlin") else ""
        out.append("24h market: %ssession %s to %s New York%s, no holiday calendar. A holiday or thin day shows up "
                   "as missing bars." % (name, res["opens"], res["closes"], berlin))
    elif not res.get("calendar_known"):
        out.append("Holiday list covers %s only: check nyse.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = short(b["et"])
    sell = res.get("side") == "sell"
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far $%.2f to $%.2f" % (min(x["l"] for x in seen), max(x["h"] for x in seen))
        if b["slot"] == n_or - 1:
            note = "opening range set: high $%.2f, low $%.2f" % (orr["high"], orr["low"])
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed %s $%.2f (%+.2f%%). Get in now if you take it: %s, $%.2f" % (
            "below" if sell else "above", lvl, t["pct_over"], t["time"], t["price"])
    elif lvl is None:
        note = ""
    elif (b["c"] < lvl) if sell else (b["c"] > lvl):
        note = "%s $%.2f" % ("below" if sell else "above", lvl)
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = ("$%.2f over $%.2f" % (b["c"] - lvl, lvl)) if sell else ("$%.2f under $%.2f" % (lvl - b["c"], lvl))
    return "%5s bar  closed $%.2f  %s" % (start, b["c"], note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    market = opts.get("market") or STOCK
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = to_eastern(now)
        day = session_day(local, market)
        s = session(day, market)
        if not s:
            emit("%s: market closed today (%s). Nothing to watch." % (day.isoformat(), trading_day(day, market)[1]))
            return 1, None
        open_dt, close_dt = session_window(day, market)
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s ET  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), "not open yet" if local < open_dt else "open, no bar closed yet",
                    short(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = eastern_to_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s ET  could not read bars: %s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (short(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = _wall_after(open_dt, parse_hhmm(res.get("no_entries_after") or "15:30", "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    or_minutes = a.or_minutes if a.or_minutes is not None else (30 if a.session else 15)
    return {"setup": a.setup, "or_minutes": or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg, "side": side_of(a.side),
            "market": make_market(a.market, a.anchor, a.close, a.weekends, a.tz, a.session)}


def _load(a, sym, now):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = to_eastern(utc_now()).date()
    market = make_market(a.market, a.anchor, a.close, a.weekends, a.tz, a.session)
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, session_day(to_eastern(now), market), a.offline, real_today, market), now


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_eastern(utc_now()).date()

    market = opts["market"]

    def provider(now):
        return get_chart(sym, a.interval, session_day(to_eastern(now), market), a.offline, real_today, market)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        local_now = to_eastern(now_fn())
        until = datetime.datetime.combine(local_now.date(), t)
        if is_24h(market) and until < local_now:
            until += datetime.timedelta(days=1)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    if a.side is None and str(doc.get("side") or "").lower() == "sell":
        opts = dict(opts, side="sell")                  # the scan was for shorts
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <TICKER>.json files")
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows], indent=2))
    else:
        print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-6s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % (
                s, status[:9], t.get("time") or "-", ("$%.2f" % t["price"]) if t else "-",
                ("$%.2f" % r["level"]) if r.get("level") is not None else "-",
                ("$%.2f" % last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <TICKER> --write, and /trader-exit." % (n, len(rows)))
        print("Free Yahoo bars, unofficial and can lag. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches a setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=None,
                       help="opening range length (default 15; 30 with --session)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default=None, choices=["buy", "long", "sell", "short"],
                       help="buy/long (default) or sell/short: a close below the level "
                            "(watchlist: the scanner's side)")
        p.add_argument("--market", default="stock", choices=["stock", "24h"],
                       help="stock = NYSE hours (default); 24h = a session you name (futures, forex, crypto)")
        p.add_argument("--anchor", help="24h: where the opening range starts, HH:MM in --tz (default New York 08:20)")
        p.add_argument("--close", help="24h: where the session ends, HH:MM in --tz (default New York 17:00)")
        p.add_argument("--session", choices=["london", "ny"],
                       help="the London (08:00-14:30 London) or New York (09:30-16:00 New York) open, 30-minute range")
        p.add_argument("--tz", choices=["newyork", "london", "berlin"],
                       help="the zone of --anchor and --close (default newyork)")
        p.add_argument("--weekends", action="store_true", help="24h: trade Saturday and Sunday too (crypto)")
        p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-19 10:15"')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this New York time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
