"""trader-entry tests. Fixtures in tests/fixtures/trader-entry/:

TSLA-2026-10-19-5m.json  TSLA's Oct 19 2026 five-minute bars, cut from the reel's
                         TSLA-60d5m.json (Yahoo chart JSON v8, fetched 10:49:45 ET).
                         It ends with the in-progress 10:45 bar and Yahoo's live tick
                         at 10:49:45. meta trimmed to one day, like a range=1d answer.
DEMO-1m-synthetic.json   SYNTHETIC one-minute bars (not market data): a wick above the
                         range high, a close AT the high, a null bar, a live tick.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("trader-entry", "entry")

EDIR = os.path.join(FIX, "trader-entry")
TSLA = os.path.join(EDIR, "TSLA-2026-10-19-5m.json")
DEMO = os.path.join(EDIR, "DEMO-1m-synthetic.json")
OPEN_TS = 1792416600       # 2026-10-19 09:30 EDT
DT = datetime.datetime


def ny(text):
    return entry.parse_now(text)


def doc(path=TSLA):
    with open(path) as fh:
        return json.load(fh)


def chart(d=None, sym="TSLA"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ny(text), **kw)


def shift_to(d, day, edt):
    """The same session moved to another date: 9:30 New York is 13:30 UTC in EDT, 14:30 in EST."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT.combine(day, datetime.time(13 if edt else 14, 30)) - DT(1970, 1, 1)).total_seconds())
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    r["meta"]["timezone"] = "EDT" if edt else "EST"
    return d


def no_breakout(d=None):
    """Every bar after the range stays under the 362.18 high."""
    d = copy.deepcopy(d if d is not None else doc())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    for i in range(3, len(q["close"])):
        q["close"][i] = min(q["close"][i], 362.00)
        q["high"][i] = min(q["high"][i], 362.10)
        q["open"][i] = min(q["open"][i], 362.00)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheReel(unittest.TestCase):
    def test_triggers_at_1015_on_the_1010_bar(self):
        r = at("2026-10-19 10:15")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 362.18)
        self.assertEqual(r["opening_range"]["low"], 354.47)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "10:15 AM")
        self.assertEqual(t["bar_start"], "10:10")
        self.assertEqual(t["bar_index"], 8)
        self.assertEqual(t["price"], 364.18)
        self.assertEqual(t["close"], 364.1759948730469)
        self.assertEqual(t["pct_over"], 0.55)
        self.assertEqual(r["level"], 362.18)
        self.assertEqual(r["prev_close"], 354.72)

    def test_at_1012_the_1010_bar_is_still_forming(self):
        r = at("2026-10-19 10:12")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "10:05")
        self.assertEqual(r["last"]["close"], 361.68)
        self.assertEqual(r["forming"]["bar_start"], "10:10")
        self.assertIn("$0.50 under the range high $362.18", r["reason"])
        self.assertEqual(at("2026-10-19 10:14:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-19 09:20")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        r = at("2026-10-19 09:44")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-19 09:46")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        c = chart()
        self.assertEqual(c["bars"][-1]["t"], 1792421385)          # 10:49:45 is in the file
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))     # read as of the file's own time
        self.assertEqual(r["now"], "2026-10-19 10:49:45")
        self.assertEqual(r["_bars"][-1]["slot"], 14)               # the 10:40 bar
        self.assertEqual(r["forming"]["bar_start"], "10:45")
        self.assertEqual(r["bars_closed"], 15)
        # 10:50: closed by the clock, but Yahoo's last trade (10:49:45) is before its close
        r = at("2026-10-19 10:50")
        self.assertEqual(r["_bars"][-1]["slot"], 14)
        self.assertIn("Yahoo's last trade is 10:49:45", r["lag_note"])
        # a minute later with no newer trade it counts; the 10:49:45 tick never becomes a bar
        r = at("2026-10-19 10:51")
        self.assertEqual(r["_bars"][-1]["slot"], 15)
        self.assertEqual(r["bars_closed"], 16)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-19 16:05")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "10:15 AM")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        r = at("2026-10-19 10:49", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 1658945)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"]), ("10:35 AM", 12, 365.75))
        self.assertEqual(t["volume_x"], 7.98)
        r = at("2026-10-19 10:16", min_volume_x=1)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("0.60x the range's volume, under your 1.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-10-19 10:30", setup="level", level=365.5)
        self.assertEqual(r["trigger"]["time"], "10:25 AM")
        self.assertEqual(r["trigger"]["bar_index"], 10)
        self.assertEqual(r["trigger"]["price"], 365.75)
        # a level under the open: the first eligible close is the 9:40 bar's, at 9:45 (no_entries_before)
        r = at("2026-10-19 10:30", setup="level", level=350)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:45 AM", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-10-19 10:30", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = no_breakout()
        r = at("2026-10-19 10:49", d)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(at("2026-10-19 15:29", d)["status"], "waiting")
        r = at("2026-10-19 15:30", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 15:30", r["reason"])
        self.assertEqual(at("2026-10-19 16:30", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = no_breakout()
        r0 = d["chart"]["result"][0]
        keep = [i for i, t in enumerate(r0["timestamp"]) if t <= OPEN_TS + 14 * 300]   # live: up to the 10:40 bar
        r0["timestamp"] = [r0["timestamp"][i] for i in keep]
        q = r0["indicators"]["quote"][0]
        for k in q:
            q[k] = [q[k][i] for i in keep]
        r0["meta"]["regularMarketTime"] = OPEN_TS + 74 * 60 + 40                       # last trade 10:44:40
        cfg = {"no_entries_before": "09:45", "no_entries_after": "10:45"}
        r = at("2026-10-19 10:45:10", d, cfg=cfg)                           # the 10:40 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 10:44:40", r["lag_note"])
        self.assertEqual(at("2026-10-19 10:46", d, cfg=cfg)["status"], "expired")

    def test_config_cutoff(self):
        cfg = {"no_entries_before": "09:45", "no_entries_after": "10:10"}
        r = at("2026-10-19 10:15", cfg=cfg)
        self.assertEqual(r["status"], "expired")
        cfg = {"no_entries_before": "10:20", "no_entries_after": "15:30"}
        r = at("2026-10-19 10:30", cfg=cfg)
        self.assertEqual(r["trigger"]["time"], "10:20 AM")       # the 10:15 bar, first to close at or after 10:20

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-19 10:15", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-19 10:15", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-10-19 10:15"), DT(2026, 10, 19, 14, 15))
        self.assertEqual(entry.parse_now("2026-10-19T14:15:00Z"), DT(2026, 10, 19, 14, 15))
        self.assertEqual(entry.parse_now("2026-10-19 10:15 ET"), DT(2026, 10, 19, 14, 15))

    def test_or_minutes_30(self):
        r = at("2026-10-19 10:49", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 362.92)
        self.assertEqual(r["trigger"]["time"], "10:15 AM")


class OneMinute(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(doc(DEMO)), "DEMO")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-19 10:06:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10667})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:59 AM", "09:58", 28, 100.62))
        r = entry.evaluate(c, ny("2026-10-19 09:58:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:58")
        self.assertEqual(r["last"]["close"], 100.5)             # the 9:57 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("10:02 AM", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 9:49 came as nulls
        self.assertEqual(slots[-1], 35)                           # 10:06 still forming, 10:06:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "10:06")

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_dst_both_sides(self):
        for day, edt in ((datetime.date(2026, 12, 7), False), (datetime.date(2026, 11, 2), False),
                         (datetime.date(2026, 10, 30), True), (datetime.date(2026, 3, 9), True)):
            d = shift_to(doc(), day, edt)
            r = entry.evaluate(chart(d), ny("%s 10:15" % day))
            self.assertEqual(r["status"], "triggered", day)
            self.assertEqual(r["trigger"]["time"], "10:15 AM", day)
            self.assertEqual(r["trigger"]["bar_index"], 8, day)
            self.assertEqual(entry.evaluate(chart(d), ny("%s 10:12" % day))["status"], "waiting", day)
        # Dec 7 at 10:15 New York is 15:15 UTC
        self.assertEqual(ny("2026-12-07 10:15"), DT(2026, 12, 7, 15, 15))

    def test_half_day_moves_the_cutoff_up(self):
        d = shift_to(no_breakout(), datetime.date(2026, 11, 27), False)
        r = entry.evaluate(chart(d), ny("2026-11-27 12:29"))
        self.assertEqual(r["status"], "waiting")
        self.assertEqual((r["closes"], r["no_entries_after"]), ("13:00", "12:30"))
        r = entry.evaluate(chart(d), ny("2026-11-27 12:30"))
        self.assertEqual(r["status"], "expired")
        self.assertIn("early close", r["reason"])
        r = entry.evaluate(chart(shift_to(doc(), datetime.date(2026, 11, 27), False)), ny("2026-11-27 10:15"))
        self.assertEqual(r["trigger"]["time"], "10:15 AM")
        self.assertIn("Early close today", "\n".join(entry.describe(r)))
        # a cutoff already before the early close stays put
        cfg = {"no_entries_before": "09:45", "no_entries_after": "12:00"}
        self.assertEqual(entry.evaluate(chart(d), ny("2026-11-27 11:00"), cfg=cfg)["no_entries_after"], "12:00")

    def test_weekend_holiday_and_empty_data(self):
        r = at("2026-10-17 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-10-19 09:30")
        r = at("2026-11-26 10:30")
        self.assertIn("Thanksgiving", r["reason"])
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-19 10:15", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-10-20 10:15")                                # Yahoo sent yesterday's bars only
        self.assertEqual(r["status"], "no-data")
        self.assertFalse(at("2028-03-01 10:30")["calendar_known"])

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "TSLA")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        with open(TSLA) as fh:
            self.body = fh.read()

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("TSLA", "5m", datetime.date(2026, 10, 19), today=datetime.date(2026, 10, 19))
        self.assertEqual(len(c["bars"]), 17)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/TSLA?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        entry.get_chart("TSLA", "5m", datetime.date(2026, 10, 19), today=datetime.date(2026, 10, 19))
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("TSLA", "5m", datetime.date(2026, 10, 19), today=datetime.date(2026, 10, 19))
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "TSLA", "--now", "2026-10-19 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_ticker_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", datetime.date(2026, 10, 19), today=datetime.date(2026, 10, 19))
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 10, 20)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 19), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 1), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 8, 31), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 1), today, "1m")
        self.assertIn("/chart/BRK-B?", entry.chart_path("brk.b", "1d", "5m"))


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 363.00 (above the high, then it closes under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 363.00, max(b["h"], 363.00)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 363.0, "h": 363.0, "l": 363.0, "c": 363.0, "v": 0})
        return {"symbol": "TSLA", "meta": {}, "bars": bars, "prev_close": 354.72, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ny(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-10-19 10:07", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:15 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 364.18)
        # it looked at 10:07 (10:05 bar spiking to 363 in progress) and at 10:10:02 (that bar closed 361.68)
        self.assertEqual([entry.to_eastern(c).strftime("%H:%M:%S") for c in calls], ["10:07:00", "10:10:02", "10:15:02"])
        text = "\n".join(lines)
        self.assertIn("10:05 bar  closed $361.68  $0.50 under $362.18", text)
        self.assertIn("10:10 bar  closed $364.18  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ny("2026-10-19 10:15:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=lag, calls=calls),
                                                   "2026-10-19 10:12", every=30)
        self.assertEqual(code, 0)
        times = [entry.to_eastern(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["10:12:00", "10:15:02", "10:15:32"])   # 10:15:02: Yahoo still at 10:14:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:15 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-10-19 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn(" 9:40 bar  closed $360.39  opening range set: high $362.18, low $354.47", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 9)    # 9:30 ... 10:10, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ny("2026-10-19 09:46"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_eastern(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:46:00", "10:04:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in (" 9:45", " 9:50", " 9:55", "10:00"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:15 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-19 15:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_eastern(clock.now()).strftime("%H:%M"), "15:30")
        until = DT(2026, 10, 19, 11, 0)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-19 10:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-17 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-19 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "TSLA", "--offline", TSLA, "--now", "2026-10-19 10:15", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 10:15 AM at $364.18", text)
        self.assertIn("/trader-exit", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "trader-entry", "symbol": "TSLA", "setup": "orb", "side": "buy", "date": "2026-10-19",
                "status": "triggered", "time": "10:15 AM", "bar_start": "10:10", "bar_index": 8, "price": 364.18,
                "level": 362.18, "interval": "5m", "opening_range": {"high": 362.18, "low": 354.47, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-10-19T14:15:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "TSLA", "--offline", TSLA, "--now", "2026-10-19 10:12", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "TSLA", "--offline", TSLA, "--now", "2026-10-19 10:12", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_gz_folder_and_mismatch(self):
        os.mkdir("charts")
        with open(TSLA, "rb") as src, gzip.open(os.path.join("charts", "TSLA.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "tsla", "--offline", "charts", "--now", "2026-10-19 10:15"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "NVDA", "--offline", TSLA])
        self.assertEqual(code, 1)
        self.assertIn("holds TSLA, not NVDA", err)

    def test_flag_errors(self):
        code, _, err = run(["check", "TSLA", "--offline", TSLA, "--market", "24h", "--anchor", "25:00"])
        self.assertEqual(code, 1)
        self.assertIn("--anchor wants a New York time", err)
        code, _, err = run(["check", "TSLA", "--offline", TSLA, "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "TSLA", "--offline", TSLA, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "10:10"}, fh)
        code, text, _ = run(["check", "TSLA", "--offline", TSLA, "--now", "2026-10-19 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "TSLA", "--offline", TSLA])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "TSLA", "--offline", TSLA, "--now", "2026-10-19 09:50", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("10:10 bar  closed $364.18  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 8)
        code, text, _ = run(["watch", "TSLA", "--offline", TSLA, "--now", "2026-10-19 09:50", "--until", "10:00",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(TSLA, os.path.join("charts", "TSLA.json"))
        shutil.copy(DEMO, os.path.join("charts", "DEMO.json"))
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [
                {"symbol": "TSLA", "setups": ["orb"], "or_high": 362.18, "or_low": 354.47},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "DEMO", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-19 10:15"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("TSLA    triggered  10:15 AM  $364.18   $362.18"), rows[1])
        self.assertIn("ZZZZ    error: no offline file", rows[2])
        self.assertIn("DEMO    error: DEMO has 1m bars", rows[3])
        self.assertIn("1 of 3 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-19 10:12", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /trader-scanner first", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/trader-exit", "short side", "in the background", "/loop", "nothing was placed", "10:15 am", "$364.18", "bar 8"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()


def synth(open_utc, rows, sym="GC=F", step=300, now_extra=0):
    """SYNTHETIC chart JSON (not market data): rows are (open, high, low, close, volume) bars
    that start at open_utc (a datetime in UTC) and run `step` seconds apart."""
    t0 = int((open_utc - DT(1970, 1, 1)).total_seconds())
    ts = [t0 + i * step for i in range(len(rows))]
    q = {k: [r[i] for r in rows] for i, k in enumerate(("open", "high", "low", "close", "volume"))}
    return {"chart": {"result": [{"meta": {"symbol": sym, "dataGranularity": "5m", "previousClose": rows[0][0],
                                           "regularMarketTime": ts[-1] + step + now_extra},
                                  "timestamp": ts, "indicators": {"quote": [q]}}], "error": None}}


# Wed 2026-10-21, 08:20 EDT = 12:20 UTC. Range bars 08:20/08:25/08:30: high 101, low 99.
# 08:35 closes 99.5 (inside), 08:40 closes 98.6 (below the low), 08:45 closes 101.4 (above the high).
BARS_24H = [(100, 101, 99.5, 100.5, 10), (100.5, 101, 99.2, 99.8, 12), (99.8, 100.4, 99, 100, 11),
            (100, 100.2, 99.3, 99.5, 9), (99.5, 99.6, 98.4, 98.6, 20), (98.6, 101.6, 98.5, 101.4, 30),
            (101.4, 101.5, 100.9, 101.0, 8)]
OPEN_24H = DT(2026, 10, 21, 12, 20)
M24 = {"anchor": datetime.time(8, 20), "close": datetime.time(17, 0), "weekends": False}


class ShortSide(unittest.TestCase):
    def ev(self, now, side, **kw):
        c = entry.parse_chart(json.dumps(synth(OPEN_24H, BARS_24H)), "GC=F")
        return entry.evaluate(c, ny(now), side=side, market=dict(M24, kind="24h"), **kw)

    def test_short_triggers_on_the_close_below_the_range_low(self):
        r = self.ev("2026-10-21 08:50", "sell")
        self.assertEqual((r["status"], r["side"]), ("triggered", "sell"))
        self.assertEqual(r["level"], 99.0)
        self.assertEqual(r["trigger"]["bar_start"], "08:40")
        self.assertEqual(r["trigger"]["time"], "8:45 AM")
        self.assertEqual(r["trigger"]["price"], 98.6)
        self.assertEqual(r["trigger"]["pct_over"], 0.4)          # 0.4 % beyond the level, in the trade's direction
        text = "\n".join(entry.describe(r))
        self.assertIn("closed below the range low $99.00", text)
        self.assertIn("BEFORE you sell short", text)

    def test_long_on_the_same_bars_waits_for_the_range_high(self):
        r = self.ev("2026-10-21 08:44", "buy")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["level"], 101.0)
        r = self.ev("2026-10-21 08:50", "buy")
        self.assertEqual((r["status"], r["trigger"]["bar_start"], r["trigger"]["price"]), ("triggered", "08:45", 101.4))

    def test_a_wick_or_a_close_at_the_low_is_not_a_trigger(self):
        rows = list(BARS_24H)
        rows[4] = (99.5, 99.6, 98.0, 99.0, 20)                   # wick under the low, closes AT it
        c = entry.parse_chart(json.dumps(synth(OPEN_24H, rows)), "GC=F")
        r = entry.evaluate(c, ny("2026-10-21 08:45"), side="sell", market=dict(M24, kind="24h"))
        self.assertEqual(r["status"], "waiting")

    def test_waiting_words_for_a_short(self):
        r = self.ev("2026-10-21 08:39", "sell")
        self.assertEqual(r["status"], "waiting")
        self.assertIn("$1.00 over the range low $99.00", r["reason"])

    def test_level_setup_short_and_entry_json(self):
        r = self.ev("2026-10-21 08:50", "sell", setup="level", level=99.6)
        self.assertEqual((r["status"], r["trigger"]["bar_start"]), ("triggered", "08:35"))
        self.assertEqual(entry.entry_doc(r, ny("2026-10-21 08:50"))["side"], "sell")
        self.assertEqual(entry.entry_doc(r, ny("2026-10-21 08:50"))["market"], "24h")

    def test_cli_accepts_short(self):
        code, text, err = run(["check", "GC=F", "--offline", _write_tmp(synth(OPEN_24H, BARS_24H)), "--now",
                               "2026-10-21 08:50", "--side", "short", "--market", "24h", "--anchor", "08:20"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("SHORT", text)


def _write_tmp(d):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh)
    return path


class Market24h(unittest.TestCase):
    def ev(self, now, anchor="08:20", close="17:00", weekends=False, **kw):
        c = entry.parse_chart(json.dumps(synth(OPEN_24H, BARS_24H)), "GC=F")
        return entry.evaluate(c, ny(now), market=entry.make_market("24h", anchor, close, weekends), **kw)

    def test_the_session_is_the_one_you_name(self):
        r = self.ev("2026-10-21 08:50")
        self.assertEqual((r["opens"], r["closes"], r["market"]), ("08:20", "17:00", "24h"))
        self.assertEqual(r["no_entries_after"], "16:30")          # 30 minutes before the close
        self.assertEqual(r["opening_range"]["high"], 101.0)
        self.assertEqual(r["trigger"]["price"], 101.4)

    def test_before_the_anchor_it_is_not_open(self):
        r = self.ev("2026-10-21 08:00")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("the session opens at 8:20", r["reason"])

    def test_after_the_cutoff_it_expires(self):
        c = entry.parse_chart(json.dumps(synth(OPEN_24H, BARS_24H[:4])), "GC=F")
        r = entry.evaluate(c, ny("2026-10-21 16:31"), market=entry.make_market("24h", "08:20", "17:00"))
        self.assertEqual(r["status"], "expired")

    def test_config_sets_the_24h_cutoff(self):
        cfg = dict(entry.DEFAULTS, no_entries_after_24h="09:00")
        c = entry.parse_chart(json.dumps(synth(OPEN_24H, BARS_24H[:4])), "GC=F")
        r = entry.evaluate(c, ny("2026-10-21 09:00"), cfg=cfg, market=entry.make_market("24h", "08:20", "17:00"))
        self.assertEqual((r["status"], r["no_entries_after"]), ("expired", "09:00"))

    def test_weekend_is_closed_unless_you_ask_for_it(self):
        sat = entry.make_market("24h", "08:20", "17:00")
        self.assertFalse(entry.trading_day(datetime.date(2026, 10, 24), sat)[0])
        self.assertFalse(entry.trading_day(datetime.date(2026, 10, 25), sat)[0])      # Sunday daytime
        self.assertTrue(entry.trading_day(datetime.date(2026, 10, 26), sat)[0])
        self.assertTrue(entry.trading_day(datetime.date(2026, 10, 24), entry.make_market("24h", "08:20", "17:00", True))[0])
        self.assertEqual(entry.next_open(datetime.date(2026, 10, 23), sat), "2026-10-26 08:20")

    def test_an_evening_anchor_wraps_past_midnight(self):
        m = entry.make_market("24h", "18:00", "17:00")
        open_dt, close_dt = entry.session_window(datetime.date(2026, 10, 25), m)       # Sunday 18:00
        self.assertEqual((open_dt, close_dt), (DT(2026, 10, 25, 18, 0), DT(2026, 10, 26, 17, 0)))
        self.assertTrue(entry.trading_day(datetime.date(2026, 10, 25), m)[0])           # Sunday evening opens the week
        self.assertFalse(entry.trading_day(datetime.date(2026, 10, 23), m)[0])          # Friday 18:00 is after the close
        # at 02:00 Monday the session that began Sunday is the one running
        self.assertEqual(entry.session_day(DT(2026, 10, 26, 2, 0), m), datetime.date(2026, 10, 25))
        self.assertEqual(entry.session_day(DT(2026, 10, 26, 17, 30), m), datetime.date(2026, 10, 26))

    def test_wrapped_session_finds_its_bars_after_midnight(self):
        # anchor 22:00 ET Tue 2026-10-20 (02:00 UTC Wed); close 05:00 next day
        rows = [(100, 101, 99.5, 100.5, 10)] * 3 + [(100.5, 100.8, 100.4, 100.6, 9), (100.6, 102, 100.5, 101.8, 30)]
        c = entry.parse_chart(json.dumps(synth(DT(2026, 10, 21, 2, 0), rows)), "GC=F")
        m = entry.make_market("24h", "22:00", "05:00")
        r = entry.evaluate(c, ny("2026-10-20 23:30"), market=m)
        self.assertEqual((r["date"], r["status"], r["trigger"]["bar_start"]), ("2026-10-20", "triggered", "22:20"))
        r = entry.evaluate(c, ny("2026-10-21 00:30"), market=m)                          # after midnight: same session
        self.assertEqual(r["date"], "2026-10-20")

    def test_chart_range_covers_an_evening_start(self):
        today = datetime.date(2026, 10, 21)
        self.assertEqual(entry.chart_range(today, today, "5m", entry.make_market("24h")), "5d")
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")

    def test_watch_replays_a_24h_session(self):
        lines = []
        c = entry.parse_chart(json.dumps(synth(OPEN_24H, BARS_24H)), "GC=F")
        clock = entry.SimClock(ny("2026-10-21 08:20"))
        opts = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None,
                "cfg": dict(entry.DEFAULTS), "side": "sell", "market": entry.make_market("24h", "08:20", "17:00")}
        code, res = entry.watch_loop(lambda now: c, clock.now, clock.sleep, lines.append, opts, every=30)
        self.assertEqual(code, 0)
        self.assertEqual(res["trigger"]["bar_start"], "08:40")
        self.assertTrue(any("TRIGGERED" in l for l in lines))


# ---------------------------------------------------------------- named sessions: london and ny, in their own zones

LONDON_RANGE = [(100, 101, 99.5, 100.5, 10), (100.5, 100.9, 99.2, 99.8, 12), (99.8, 100.4, 99.0, 100.0, 11),
                (100.0, 100.8, 99.4, 100.2, 9), (100.2, 100.6, 99.5, 99.9, 10), (99.9, 100.5, 99.3, 100.1, 9)]
# 30 minutes = 6 bars, range high 101.0, low 99.0. Then: inside, a close under the low, a close over the high.
LONDON_AFTER = [(100.1, 100.3, 99.4, 99.6, 8), (99.6, 99.7, 98.2, 98.4, 30), (98.4, 101.8, 98.3, 101.5, 40),
                (101.5, 101.6, 101.0, 101.2, 8)]
LONDON_BARS = LONDON_RANGE + LONDON_AFTER
LONDON_OPEN_UTC = DT(2026, 10, 21, 7, 0)            # 08:00 London summer time = 07:00 UTC = 03:00 New York = 09:00 Berlin


class Zones(unittest.TestCase):
    def test_europe_and_us_clock_changes(self):
        for utc, ny_off, lon, ber in ((DT(2026, 3, 8, 6, 59), -5, 0, 1), (DT(2026, 3, 8, 7, 0), -4, 0, 1),     # US first
                                      (DT(2026, 3, 29, 0, 59), -4, 0, 1), (DT(2026, 3, 29, 1, 0), -4, 1, 2),   # then Europe
                                      (DT(2026, 10, 25, 0, 59), -4, 1, 2), (DT(2026, 10, 25, 1, 0), -4, 0, 1),  # Europe first
                                      (DT(2026, 11, 1, 6, 0), -5, 0, 1), (DT(2026, 7, 1, 12, 0), -4, 1, 2)):
            self.assertEqual((entry.zone_offset(utc, "newyork"), entry.zone_offset(utc, "london"),
                              entry.zone_offset(utc, "berlin")), (ny_off, lon, ber), utc)

    def test_local_time_round_trip(self):
        for local in (DT(2026, 10, 21, 8, 0), DT(2026, 1, 15, 8, 0), DT(2026, 10, 27, 8, 0), DT(2026, 3, 30, 14, 30)):
            for zone in ("london", "berlin", "newyork"):
                utc = entry.zone_to_utc(local, zone)
                self.assertEqual(utc + datetime.timedelta(hours=entry.zone_offset(utc, zone)), local, (local, zone))

    def test_the_two_sessions_in_new_york_and_berlin_time(self):
        # (date, london ET window, london Berlin, ny ET window, ny Berlin)
        table = (("2026-10-21", ("03:00", "09:30"), ("09:00", "15:30"), ("09:30", "16:00"), ("15:30", "22:00")),
                 ("2026-01-14", ("03:00", "09:30"), ("09:00", "15:30"), ("09:30", "16:00"), ("15:30", "22:00")),
                 ("2026-03-10", ("04:00", "10:30"), ("09:00", "15:30"), ("09:30", "16:00"), ("14:30", "21:00")),   # US on summer time, Europe not yet
                 ("2026-10-27", ("04:00", "10:30"), ("09:00", "15:30"), ("09:30", "16:00"), ("14:30", "21:00")),   # Europe back, US not yet
                 ("2026-11-03", ("03:00", "09:30"), ("09:00", "15:30"), ("09:30", "16:00"), ("15:30", "22:00")))
        for day, l_et, l_be, n_et, n_be in table:
            d = datetime.date.fromisoformat(day)
            for name, et, be in (("london", l_et, l_be), ("ny", n_et, n_be)):
                m = entry.make_market(session=name)
                o, c = entry.session_window(d, m)
                b = entry.session_block(m, o, c)
                self.assertEqual(((entry.hm(o.time()), entry.hm(c.time())), (b["berlin"]["open"], b["berlin"]["close"])),
                                 (et, be), (day, name))
                self.assertEqual((c - o).total_seconds(), 6.5 * 3600)

    def test_a_literal_berlin_clock_window(self):
        m = entry.make_market(tz="berlin", anchor="15:30", close="22:00", kind="24h")
        o, c = entry.session_window(datetime.date(2026, 10, 27), m)               # a gap week: 15:30 Berlin is 10:30 New York
        self.assertEqual((entry.hm(o.time()), entry.hm(c.time())), ("10:30", "17:00"))
        o, c = entry.session_window(datetime.date(2026, 10, 21), m)
        self.assertEqual((entry.hm(o.time()), entry.hm(c.time())), ("09:30", "16:00"))

    def test_bad_session_flags(self):
        for kw, word in ((dict(session="paris"), "--session is london or ny"), (dict(tz="tokyo", kind="24h"), "--tz is"),
                         (dict(session="london", tz="newyork"), "drop --tz")):
            with self.assertRaises(entry.EntryError) as e:
                entry.make_market(**kw)
            self.assertIn(word, str(e.exception))


class LondonAndNewYork(unittest.TestCase):
    def ev(self, now, side, bars=LONDON_BARS, open_utc=LONDON_OPEN_UTC, session="london", or_minutes=30, **kw):
        c = entry.parse_chart(json.dumps(synth(open_utc, bars)), "GC=F")
        return entry.evaluate(c, ny(now), side=side, or_minutes=or_minutes, market=entry.make_market(session=session), **kw)

    def test_the_london_range_is_the_first_30_minutes_after_09_00_berlin(self):
        r = self.ev("2026-10-21 03:45", "sell")
        self.assertEqual((r["opens"], r["closes"], r["no_entries_after"]), ("03:00", "09:30", "09:00"))
        self.assertEqual(r["opening_range"], {"high": 101.0, "low": 99.0, "minutes": 30, "bars": 6, "of": 6,
                                               "complete": True, "avg_volume": 10})

    def test_short_breakout_down_through_the_range_low(self):
        r = self.ev("2026-10-21 03:55", "sell")
        self.assertEqual((r["status"], r["side"], r["level"]), ("triggered", "sell", 99.0))
        t = r["trigger"]
        self.assertEqual((t["bar_start"], t["time"], t["price"], t["bar_index"]), ("03:35", "3:40 AM", 98.4, 7))
        text = "\n".join(entry.describe(r))
        self.assertIn("London session, session 03:00 to 09:30 New York (09:00 to 15:30 Berlin)", text)
        self.assertIn("closed below the range low $99.00", text)
        doc = entry.entry_doc(r, ny("2026-10-21 03:55"))
        self.assertEqual((doc["side"], doc["market"], doc["date"]), ("sell", "24h", "2026-10-21"))
        self.assertEqual(doc["session"], {"kind": "24h", "open": "03:00", "close": "09:30", "weekends": False,
                                          "tz": "london", "local_open": "08:00", "local_close": "14:30",
                                          "name": "london", "berlin": {"open": "09:00", "close": "15:30"}})
        self.assertEqual(doc["opening_range"], {"high": 101.0, "low": 99.0, "minutes": 30})

    def test_long_breakout_up_through_the_range_high_is_the_mirror(self):
        r = self.ev("2026-10-21 04:05", "buy")
        self.assertEqual((r["status"], r["level"], r["trigger"]["bar_start"], r["trigger"]["price"]),
                         ("triggered", 101.0, "03:40", 101.5))
        self.assertEqual(self.ev("2026-10-21 03:44", "buy")["status"], "waiting")

    def test_nothing_before_the_range_is_complete_and_nothing_after_the_cutoff(self):
        r = self.ev("2026-10-21 03:25", "sell")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        r = self.ev("2026-10-21 02:30", "sell")
        self.assertEqual((r["phase"], r["reason"][:24]), ("pre-open", "the session opens at 3:0"))
        quiet = LONDON_RANGE + [(100.1, 100.3, 99.4, 99.6, 8)] * 5
        r = self.ev("2026-10-21 09:05", "sell", bars=quiet)
        self.assertEqual(r["status"], "expired")

    def test_the_new_york_session_opens_at_09_30_new_york(self):
        ny_bars = [tuple(b) for b in LONDON_BARS]
        r = self.ev("2026-10-21 10:25", "sell", bars=ny_bars, open_utc=DT(2026, 10, 21, 13, 30), session="ny")
        self.assertEqual((r["opens"], r["closes"], r["status"]), ("09:30", "16:00", "triggered"))
        self.assertEqual(r["trigger"]["time"], "10:10 AM")
        self.assertEqual(r["session"]["berlin"], {"open": "15:30", "close": "22:00"})
        self.assertEqual(r["session"]["name"], "ny")

    def test_in_the_weeks_the_clocks_differ_the_sessions_move_in_new_york_time(self):
        # Tue 2026-10-27: London is back on winter time (08:00 = 04:00 New York), Berlin 09:00 as always
        r = self.ev("2026-10-27 04:45", "sell", open_utc=DT(2026, 10, 27, 8, 0))
        self.assertEqual((r["date"], r["opens"], r["status"]), ("2026-10-27", "04:00", "triggered"))
        self.assertEqual(r["session"]["berlin"]["open"], "09:00")
        # the same bars read as a stock-style fixed 03:00 window would be wrong: nothing before 04:00 counts
        r = self.ev("2026-10-27 03:45", "sell", open_utc=DT(2026, 10, 27, 8, 0))
        self.assertEqual(r["phase"], "pre-open")

    def test_week_and_weekend_rules_use_the_new_york_window(self):
        m = entry.make_market(session="london")
        self.assertTrue(entry.trading_day(datetime.date(2026, 10, 23), m)[0])        # Friday, ends 09:30 New York
        self.assertFalse(entry.trading_day(datetime.date(2026, 10, 24), m)[0])       # Saturday
        self.assertFalse(entry.trading_day(datetime.date(2026, 10, 25), m)[0])       # Sunday morning: the market is shut
        self.assertEqual(entry.next_open(datetime.date(2026, 10, 23), m), "2026-10-26 04:00")   # Europe is back on winter time from Oct 25

    def test_session_day_inside_the_window(self):
        m = entry.make_market(session="london")
        self.assertEqual(entry.session_day(DT(2026, 10, 21, 5, 0), m), datetime.date(2026, 10, 21))

    def test_cli_london_short_defaults_to_a_30_minute_range(self):
        path = _write_tmp(synth(LONDON_OPEN_UTC, LONDON_BARS))
        code, text, err = run(["check", "GC=F", "--offline", path, "--now", "2026-10-21 03:55", "--session", "london",
                               "--side", "short"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("(30-minute range)", text)
        self.assertIn("09:00 to 15:30 Berlin", text)
        code, text, err = run(["check", "GC=F", "--offline", path, "--now", "2026-10-21 03:55", "--session", "london",
                               "--side", "short", "--or-minutes", "15"])
        self.assertIn("(15-minute range)", text)
        code, _, err = run(["check", "GC=F", "--offline", path, "--session", "london", "--tz", "berlin"])
        self.assertEqual(code, 1)
        self.assertIn("drop --tz", err)
