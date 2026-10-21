#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-19 10:15"        # pin the clock (New York time)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side, both or neither of qty / notional,
                notional on anything but a market day order (Alpaca's rule),
                a limit order without a limit price, unknown type; a bracket
                that is not whole shares, not day / gtc, or misses a leg
  asset         options (OCC symbols) unless "allow_options": true; crypto
                and anything that is not a stock or ETF
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order_usd, over max_order_usd
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a bracket stop_loss_stop_price
                or the order's plan stop); a stop on the wrong side of the entry
  loss          qty x (entry - stop) over max_loss_per_trade_usd. The entry is
                the limit price for a limit order, else the HIGHER of the plan's
                entry and a fresh price (under 15 minutes old), so a stale plan
                cannot understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_usd (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (New York
                time), or the market is closed (weekend, NYSE holiday, outside
                9:30 to the close; on a 1:00 p.m. early close the window ends
                3 hours earlier too)
  pdt           only with "pdt_guard": true: 3 day trades already in the last
                5 business days (a 4th would trip the old pattern-day-trader rule)

Short side. A short entry (a sell that goes short) needs "allow_short": true, then runs the
same rules mirrored: its stop must be ABOVE the entry, its target below, and the loss is
measured from the LOWER of the plan's entry and a fresh price (a price that fell since the
plan means a bigger loss at the same stop). A short ties up margin like any entry: the
notional / leverage must fit in your cash after the buffer, unless "allow_margin" is true.

24h markets (futures, forex, crypto, CFDs: a plan from trader-exit with "market": "24h").
They are off until you say "allow_24h": true, because the pack was built for US stocks and
ETFs. With it on, the symbol may look like GC=F, EURUSD=X or BTC-USD, a bracket may carry
fractional units, and the entry window is the plan's own session (its "session": open, close;
no entries in the last 30 minutes, or between "no_entries_before_24h" and "no_entries_after_24h")
instead of the NYSE hours. There is no holiday calendar for these markets.

Leverage and currency. With "leverage" above 1 the max order, position cap and cash checks
count the margin an order ties up (notional / leverage), like trader-budget. "fx_rate" converts
the money settings from your account currency into the instrument's, and
"max_loss_per_trade_pct" makes the per-trade limit a percent of the budget.

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000,
    "min_order_usd": 5, "max_loss_per_trade_usd": 50, "max_daily_loss_usd": 150, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:45", "no_entries_after": "15:30", "flat_by": "15:55",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "pdt_guard": False, "allow_24h": False, "leverage": 1, "fx_rate": 1, "max_loss_per_trade_pct": None,
    "no_entries_before_24h": None, "no_entries_after_24h": None,
}
ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
PRICE_FIELDS = ("qty", "notional", "limit_price", "stop_price", "take_profit_limit_price",
                "stop_loss_stop_price", "stop_loss_limit_price")
TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
TIFS = {"day", "gtc", "opg", "cls", "ioc", "fok"}
OCC = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
TICKER = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")
TICKER_24H = re.compile(r"^\^?[A-Z][A-Z0-9.=\-]{0,11}$")       # GC=F, EURUSD=X, BTC-USD, ^GSPC
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the stop price", "stale price": "an old price"}

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


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time (New York, no tz files)

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


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise RiskError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NYSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def hhmm(value, key):
    """"09:45" -> time(9, 45); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be a New York time like \"09:45\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def _shift(t, hours):
    dt = datetime.datetime.combine(datetime.date(2000, 1, 1), t) - datetime.timedelta(hours=hours)
    return dt.time()


def window_check(now_et, c):
    """Reasons an entry cannot be placed now (New York time). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_et.date(), now_et.time().replace(second=0, microsecond=0)
    if d.year not in CALENDAR_YEARS:
        warns.append("the holiday list covers %s only: check nyse.com for %s"
                     % (" and ".join(map(str, CALENDAR_YEARS)), d.isoformat()))
    ok, why = trading_day(d)
    stamp = now_et.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: the market is closed today (%s, %s New York)" % (why, stamp)], warns
    close = EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE
    if t < OPEN or now_et.time() >= close:
        return ["window: the market is closed (%s New York; the session is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), close.strftime("%H:%M"))], warns
    if close == EARLY and after is not None:
        after = _shift(after, 3)
        warns.append("early close at 1:00 p.m. today (%s): no new entries after %s"
                     % (NYSE_EARLY_CLOSE[d.isoformat()], after.strftime("%H:%M")))
    if before is not None and t < before:
        return ["window: no new entries before %s New York (it is %s)" % (before.strftime("%H:%M"),
                                                                          t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s New York (it is %s)" % (after.strftime("%H:%M"),
                                                                         t.strftime("%H:%M"))], warns
    return [], warns


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


def _wall_after(open_dt, t):
    at = datetime.datetime.combine(open_dt.date(), t)
    return at if at >= open_dt else at + datetime.timedelta(days=1)


def window_check_24h(now_et, sess, c):
    """Reasons an entry cannot be placed now in a 24h market's own session (plan["session"])."""
    o_t, c_t = hhmm(sess.get("open"), "session open"), hhmm(sess.get("close"), "session close")
    if o_t is None or c_t is None:
        return ["window: the plan's 24h session has no open or close"], []
    weekends = bool(sess.get("weekends"))
    stamp = now_et.strftime("%a %Y-%m-%d %H:%M")
    warns = ["no holiday calendar for 24h markets: a holiday or a thin day is not checked"]
    for back in (0, 1):
        d = now_et.date() - datetime.timedelta(days=back)
        o, cl = datetime.datetime.combine(d, o_t), datetime.datetime.combine(d, c_t)
        if cl <= o:
            cl += datetime.timedelta(days=1)
        if not (o <= now_et < cl):
            continue
        if not weekends and not (_in_market_week(o) and _in_market_week(cl - datetime.timedelta(seconds=1))):
            continue
        b_t, a_t = c.get("no_entries_before_24h"), c.get("no_entries_after_24h")
        before = _wall_after(o, hhmm(b_t, "no_entries_before_24h")) if b_t else o
        after = _wall_after(o, hhmm(a_t, "no_entries_after_24h")) if a_t else cl - datetime.timedelta(minutes=30)
        if now_et < before:
            return ["window: no new entries before %s New York (it is %s)" % (before.strftime("%H:%M"),
                                                                              now_et.strftime("%H:%M"))], warns
        if now_et > after:
            return ["window: no new entries after %s New York (it is %s)" % (after.strftime("%H:%M"),
                                                                             now_et.strftime("%H:%M"))], warns
        return [], warns
    return ["window: the 24h session is closed (%s New York; the session is %s to %s%s)"
            % (stamp, o_t.strftime("%H:%M"), c_t.strftime("%H:%M"), "" if weekends else ", Sunday 18:00 to Friday 17:00")], warns


def convert_money(c, portfolio):
    """Your money settings are in your account currency; fx_rate says how many units of the
    instrument's currency one unit of it buys. Returns (config, portfolio) in the instrument's."""
    fx = 1.0 if c.get("fx_rate") is None else _f(c.get("fx_rate"))
    if fx is None or fx <= 0:
        raise RiskError("fx_rate must be above 0")
    budget_acct = _f(c.get("budget"))
    c = dict(c)
    pct = _f(c.get("max_loss_per_trade_pct"))
    if pct:
        if not 0 < pct <= 100:
            raise RiskError("max_loss_per_trade_pct must be above 0 and at most 100")
        if not budget_acct:
            raise RiskError("max_loss_per_trade_pct needs a budget in the config")
        c["max_loss_per_trade_usd"] = round(budget_acct * pct / 100.0 * fx, 2)
    elif fx != 1:
        for k in ("max_loss_per_trade_usd",):
            if _f(c.get(k)):
                c[k] = round(float(c[k]) * fx, 2)
    if fx != 1:
        for k in ("budget", "max_order_usd", "min_order_usd", "max_daily_loss_usd"):
            if _f(c.get(k)):
                c[k] = round(float(c[k]) * fx, 2)
        if isinstance(portfolio, dict) and _f(portfolio.get("cash")) is not None:
            portfolio = dict(portfolio, cash=round(float(portfolio["cash"]) * fx, 2))
    lev = 1.0 if c.get("leverage") is None else _f(c.get("leverage"))
    if lev is None or lev < 1:
        raise RiskError("leverage must be 1 or more")
    c["leverage"] = lev
    return c, portfolio


def business_days(today, n=5):
    """The last n NYSE trading days up to and including today, newest first."""
    out, d = [], today
    for _ in range(40):
        if trading_day(d)[0]:
            out.append(d.isoformat())
            if len(out) == n:
                break
        d -= datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in ALPACA_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    return "$" + "{:,.2f}".format(float(x)).replace(".00", "")


def _cent(x):
    """A price to the cent, half up (366.475 -> 366.48)."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def _p(x):
    return "%.2f" % float(x)


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


def day_trades(state, today):
    """Day trades in the last 5 business days: a (day, symbol) with both a buy
    and a sell placed or filled that day. An entry placed today counts too: it
    is meant to be flat by the close. Returns (count, sorted list of "day SYM")."""
    days = set(business_days(datetime.date.fromisoformat(today)))
    sides = {}
    for rec in (state or {}).get("orders", []):
        if rec.get("status") not in ("placed", "filled"):
            continue
        d = rec_day(rec)
        if d not in days:
            continue
        key = (d, str(rec.get("symbol", "")).upper())
        sides.setdefault(key, set()).add(str(rec.get("side", "")).lower())
        if d == today and rec.get("status") == "placed" and rec_is_entry(rec):
            sides[key].add("pending")
    hits = sorted("%s %s" % k for k, s in sides.items()
                  if ("buy" in s and "sell" in s) or (k[0] == today and "pending" in s))
    return len(hits), hits


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its New York date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    c, portfolio = convert_money(c, portfolio)
    lev = c["leverage"]
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_et = to_eastern(now_utc)
    today = today or now_et.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), r.get("side")) for r in done_today}
    win_reasons, win_warns = window_check(now_et, c)
    pdt_n, pdt_list = day_trades(state, today) if c.get("pdt_guard") else (0, [])
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty, notional = o.get("qty"), o.get("notional")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        is24 = str(plan.get("market") or "").lower() == "24h" and isinstance(plan.get("session"), dict)
        oc = str(o.get("order_class") or "").strip().lower()
        legs = o.get("take_profit_limit_price") is not None or o.get("stop_loss_stop_price") is not None
        bracket = oc == "bracket" or (not oc and legs)
        value = None

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if (qty is None) == (notional is None):
            reasons.append("format: give exactly one of qty or notional")
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not isinstance(o.get(k), str):
                    warns.append("send %s as a string (\"%s\"), the way place_stock_order types it"
                                 % (k, o.get(k)))
        if typ not in TYPES:
            reasons.append("format: unknown order type %r" % typ)
        if tif not in TIFS:
            reasons.append("format: unknown time_in_force %r" % tif)
        if notional is not None and (typ != "market" or tif != "day"):
            reasons.append("format: a notional (dollar) order must be a market order with time_in_force day")
        if typ in ("limit", "stop_limit") and o.get("limit_price") is None:
            reasons.append("format: a %s order needs a limit_price" % typ)
        if typ in ("stop", "stop_limit") and o.get("stop_price") is None:
            reasons.append("format: a %s order needs a stop_price" % typ)
        if o.get("extended_hours") and not (typ == "limit" and tif in ("day", "gtc")):
            reasons.append("format: extended hours needs a limit order with time_in_force day or gtc")
        if o.get("stop_loss_limit_price") is not None and o.get("stop_loss_stop_price") is None:
            reasons.append("format: stop_loss_limit_price needs a stop_loss_stop_price")
        if bracket:
            if notional is not None:
                reasons.append("format: a bracket order needs a whole-share qty, not a notional amount")
            elif _f(qty) is not None and abs(_f(qty) - round(_f(qty))) > 1e-9:
                if is24:
                    warns.append("fractional units (%s): Alpaca's bracket takes whole shares only, so this is a "
                                 "ticket for your own broker" % qty)
                else:
                    reasons.append("format: a bracket order takes whole shares only, got qty %r" % qty)
            if tif not in ("day", "gtc"):
                reasons.append("format: a bracket order needs time_in_force day or gtc, got %r" % tif)
            if o.get("take_profit_limit_price") is None or o.get("stop_loss_stop_price") is None:
                reasons.append("format: a bracket order needs both take_profit_limit_price and "
                               "stop_loss_stop_price")
        elif oc or o.get("advanced_instructions"):
            warns.append("advanced order (%s): check every leg before you say yes"
                         % (oc or "advanced_instructions"))

        # asset
        if sym and OCC.match(sym.replace(" ", "")):
            if not c.get("allow_options"):
                reasons.append("asset: %s is an option contract; options are off "
                               "(\"allow_options\": false)" % sym)
            else:
                warns.append("option order: options can lose 100%% of what you pay")
        elif is24:
            if not c.get("allow_24h"):
                reasons.append("asset: %s is a 24h-market order (futures, forex, crypto or CFD); those are off "
                               "(\"allow_24h\": false). This pack was built for US stocks and ETFs" % sym)
            elif not TICKER_24H.match(sym):
                reasons.append("asset: %s does not look like a Yahoo symbol such as GC=F, EURUSD=X or BTC-USD" % sym)
            else:
                warns.append("24h market: %s is outside Alpaca's stock tool; this is a ticket for your own "
                             "broker's demo or live account" % sym)
        elif sym and ("/" in sym or not TICKER.match(sym)):
            reasons.append("asset: %s does not look like a US stock or ETF ticker; "
                           "this pack trades stocks and ETFs only" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("limit", "stop_limit"):
            ref, ref_from = _f(o.get("limit_price")), "limit"
        elif typ == "stop":
            ref, ref_from = _f(o.get("stop_price")), "stop price"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                pick = min if side == "sell" else max          # the price that makes the loss bigger
                ref, ref_from = pick(cands, key=lambda x: x[0])
                ref = _cent(ref)
                if live and ref_from == "price" and plan_entry and \
                        ((live < plan_entry - CENT) if side == "sell" else (live > plan_entry + CENT)):
                    info.append("%s last %s is %s the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), "under" if side == "sell" else "above", _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "market":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        q = _f(qty)
        if notional is not None and _f(notional):
            value = _f(notional)
        elif q:
            if ref:
                value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9:
            if q is not None:
                exiting = q <= held + 1e-9
            elif value is not None:
                exiting = value <= held_value + CENT
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        tied = value / lev if value is not None else None      # the margin an order ties up
        if value is not None:
            if value < float(c.get("min_order_usd") or 0) - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_usd"])))
            if c.get("max_order_usd") and tied > float(c["max_order_usd"]) + CENT:
                later.append("size: %s%s is over your %s max order"
                             % (money(tied), " of margin (%s at 1:%g)" % (money(value), lev) if lev > 1 else "",
                                money(c["max_order_usd"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_usd = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        short_entry = side == "sell" and kind == "entry"
        if value is not None and ((side == "buy" and kind == "entry") or (short_entry and c.get("allow_short"))):
            after_cash = cash - tied
            if after_cash < -CENT and not c.get("allow_margin"):
                later.append("cash: needs %s%s but only %s cash; that would be margin (borrowed money)"
                             % (money(tied), " of margin" if lev > 1 or short_entry else "", money(cash)))
            elif after_cash < buffer_usd - CENT:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_usd)))
            pos_after = abs(held_value) + tied
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            if q is not None:
                over = q > held + 1e-9
            elif value is not None:
                over = value > held_value + CENT
            else:
                over = held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window, pdt
        max_loss = risk_ps = rr = stop = target = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("stop_loss_stop_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("take_profit_limit_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the bracket stop %s differs from the plan's stop %s: the bracket's is the one "
                             "the broker uses" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a bracket "
                                 "stop_loss_stop_price, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    shares = q if q is not None else ((value or 0) / ref)
                    max_loss = round(shares * risk_ps, 2)
                    cap = _f(c.get("max_loss_per_trade_usd"))
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share = %s at the stop, over your %s max loss "
                                     "per trade" % (_num(shares), _p(risk_ps), money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_usd"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            if is24:
                w24, w24_warns = window_check_24h(now_et, plan["session"], c)
                later.extend(w24)
                warns.extend(w for w in w24_warns if w not in warns)
            else:
                later.extend(win_reasons)
            if c.get("pdt_guard") and pdt_n >= 3:
                later.append("pdt: %d day trades in the last 5 business days (%s); a 4th trips the "
                             "pattern-day-trader rule your broker applies (\"pdt_guard\": true)"
                             % (pdt_n, ", ".join(pdt_list)))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s%s, reward:risk %s%s"
                            % (money(max_loss), (" (limit %s)" % money(c["max_loss_per_trade_usd"]))
                               if _f(c.get("max_loss_per_trade_usd")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy" and held < -1e-9:                 # buying back a short: release its margin
                if tied is not None:
                    acct["cash"] += tied
                    acct["values"]["~margin"] = max(acct["values"].get("~margin", 0.0) - tied, 0.0)
                if q is not None:
                    acct["shares"][sym] = held + q
                elif ref and value is not None:
                    acct["shares"][sym] = held + value / ref
            elif side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
                elif ref and value is not None:
                    acct["shares"][sym] = held + value / ref
            elif side == "sell" and short_entry:               # a short: its margin is set aside, not sale proceeds
                if tied is not None:
                    acct["cash"] -= tied
                    acct["values"]["~margin"] = acct["values"].get("~margin", 0.0) + tied
                if q is not None:
                    acct["shares"][sym] = held - q
                elif ref and value is not None:
                    acct["shares"][sym] = held - value / ref
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
                elif ref and value is not None:
                    acct["shares"][sym] = held - value / ref
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": money(_f(notional) or 0) if notional is not None else ("%s shares" % qty),
            "value_usd": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_new_york": now_et.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2), "day_trades_5d": pdt_n if c.get("pdt_guard") else None},
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s New York. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_new_york"], money(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--now", help="pretend it is this New York time, e.g. \"2026-10-19 10:15\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in New York)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-19")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
