"""trader-exit tests. Bars for the ATR stop reuse tests/fixtures/trader-entry/
(TSLA's real Oct 19 2026 five-minute bars, see test_entry.py)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("trader-exit", "exit")
entry = load("trader-entry", "entry")

TSLA = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")

# trader/plan.json for the reel, exactly as the contract writes it
REEL_PLAN = {"created_by": "trader-exit", "symbol": "TSLA", "side": "buy", "date": "2026-10-19", "setup": "orb",
             "entry": 364.18, "entry_time": "10:15 AM", "entry_bar": 8, "interval": "5m",
             "opening_range": {"high": 362.18, "low": 354.47, "minutes": 15},
             "stop": 358.32, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 370.04, "target_r": 1.0, "target_label": "1R (one times the risk)",
             "risk_per_share": 5.86, "reward_risk": 1.0, "flat_by": "15:55"}
REEL_ENTRY = {"created_by": "trader-entry", "symbol": "TSLA", "setup": "orb", "side": "buy", "date": "2026-10-19",
              "status": "triggered", "time": "10:15 AM", "bar_start": "10:10", "bar_index": 8, "price": 364.18,
              "level": 362.18, "interval": "5m", "opening_range": {"high": 362.18, "low": 354.47, "minutes": 15},
              "source": "yahoo", "checked_at": "2026-10-19T14:15:00Z"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=362.18, ol=354.47, atr=None, n=None, side="buy"):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, side)


class Math(unittest.TestCase):
    def test_the_reel_or_mid_1r(self):
        p = c(364.18, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("358.32"))           # floor(358.325)
        self.assertEqual(p["risk"], Decimal("5.86"))
        self.assertEqual(p["target"], Decimal("370.04"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertIn("rounded down", p["stop_detail"])

    def test_the_default_2r(self):
        p = c(364.18, "or-mid")
        self.assertEqual(p["target"], Decimal("375.90"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_raw_yahoo_floats_give_the_same_cents(self):
        p = c(364.1759948730469, "or-mid", "1R", 362.17999267578125, 354.4700012207031)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("364.18"), Decimal("358.32"), Decimal("370.04")))

    def test_other_stops(self):
        p = c(364.18, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("354.47"), Decimal("9.71"), Decimal("383.60")))
        p = c(364.18, "price:358.329")
        self.assertEqual(p["stop"], Decimal("358.32"))              # rounds away from the entry
        p = c(364.18, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("360.53"), Decimal("3.65"), Decimal("371.48")))
        p = c(364.18, "atr:1.5", atr=3.2181, n=9)
        self.assertEqual(p["stop"], Decimal("359.35"))              # 364.18 - 4.82715 = 359.35285
        self.assertEqual(p["stop_label"], "1.5 x ATR(9) under the entry")

    def test_target_rounds_half_up_and_price_targets(self):
        p = c(100, "price:99.01", "1.5R", None, None)
        self.assertEqual(p["target"], Decimal("101.49"))            # 100 + 1.485
        self.assertEqual(p["target_label"], "1.5R (1.5 times the risk)")
        p = c(364.18, "or-mid", "price:372")
        self.assertEqual(p["reward_risk"], Decimal("1.33"))
        self.assertEqual(p["target_label"], "your price (1.33R)")
        self.assertEqual(p["target_r"], Decimal("1.33"))

    def test_refusals_say_why(self):
        cases = [("price:364.18", "2R", "at or above the entry"),
                 ("price:365", "2R", "at or above the entry"),
                 ("or-mid", "price:364", "at or below the entry"),
                 ("price:364.01", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(364.18, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 364.18 is 0.182: a $0.19 risk is allowed, $0.18 is not
        self.assertEqual(c(364.18, "price:363.99")["risk"], Decimal("0.19"))
        with self.assertRaises(exitp.PlanError):
            c(364.18, "price:364.00")
        with self.assertRaises(exitp.PlanError):
            c(358.00, "or-mid")                                     # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(364.18, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(364.18, "or-mid", oh=354.47, ol=362.18)

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("TSLA", "5m", None, TSLA)
        day = exitp.datetime.date(2026, 10, 19)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=8)
        self.assertEqual(len(bars), 9)                              # 9:30 ... the 10:10 entry bar
        # by hand: the true range of each bar, the first one without the overnight gap
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 362.18 - 354.47, places=4)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 9)
        self.assertAlmostEqual(atr, sum(trs) / 9, places=9)
        self.assertAlmostEqual(atr, 3.2182, places=4)
        # later in the day it uses the last 14 bars; the file's in-progress 10:45 bar and tick never count
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 15)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-19 10:16"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "TSLA", "--offline", TSLA, "--now", "2026-10-19 10:15", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertIn("Stop          $358.32  opening-range midpoint", text)
        self.assertIn("Target        $370.04  1R (one times the risk)", text)
        self.assertIn("Risk a share  $5.86 (1.61% of the entry)", text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Flat by       15:55", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/trader-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 375.90)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-19 10:20"])
        self.assertEqual(code, 0)
        self.assertIn("$375.90", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-20 10:20"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "tsla", "--entry", "364.18", "--or-high", "362.18", "--or-low", "354.47",
                                  "--time", "10:15", "--entry-bar", "8", "--date", "2026-10-19", "--target", "1R",
                                  "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", TSLA, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (359.35, 4.83, 373.84))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 9))
        self.assertAlmostEqual(p["atr"], 3.2182, places=4)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", TSLA, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("TSLA is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "362.19", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (362.19, 358.32, None))

        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, setup="level", level=362.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("is a buy but --side says sell", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, side="sideways"))
        code, _, err = self.plan()
        self.assertIn("buy or sell", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "TSLA")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "TSLA", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "TSLA", "--entry", "364.18", "--or-high", "362.18")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_config_half_day_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:45", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:45")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        os.remove(os.path.join("trader", "config.json"))

        half = dict(REEL_ENTRY, date="2026-11-27")
        self.write(os.path.join("trader", "entry.json"), half)
        code, text, _ = run(exitp, ["plan", "--now", "2026-11-27 10:16"])
        self.assertEqual(code, 0)
        self.assertIn("Flat by       12:55 ET", text)
        self.assertIn("early close", text)
        code, _, err = run(exitp, ["plan", "--now", "2026-11-27 10:16", "--flat-by", "14:00"])
        self.assertEqual(code, 1)
        self.assertIn("after the 1:00 PM close", err)

        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, _, err = self.plan("--flat-by", "16:30")
        self.assertIn("after the 4:00 PM close", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="3:58 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money", "/trader-timemachine", "/trader-budget", "short side", "$358.32", "$370.04", "$5.86", "375.90", "too tight to survive the spread"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()


GOLD_ENTRY = {"created_by": "trader-entry", "symbol": "GC=F", "setup": "orb", "side": "sell", "market": "24h",
              "session": {"kind": "24h", "open": "08:20", "close": "17:00", "weekends": False},
              "date": "2026-10-21", "status": "triggered", "time": "8:45 AM", "bar_start": "08:40", "bar_index": 4,
              "price": 98.6, "level": 99.0, "interval": "5m",
              "opening_range": {"high": 101.0, "low": 99.0, "minutes": 15},
              "source": "yahoo", "checked_at": "2026-10-21T12:50:00Z"}


class ShortMath(unittest.TestCase):
    def test_short_stop_is_above_and_rounds_up(self):
        p = c(98.60, "or-mid", "2R", oh=101.0, ol=99.0, side="sell")
        self.assertEqual((p["stop"], p["risk"], p["target"], p["reward_risk"]),
                         (Decimal("100.00"), Decimal("1.40"), Decimal("95.80"), Decimal("2.00")))
        p = c(98.60, "or-mid", "1R", oh=101.01, ol=99.0, side="sell")      # midpoint 100.005
        self.assertEqual(p["stop"], Decimal("100.01"))
        self.assertIn("rounded up", p["stop_detail"])
        self.assertEqual(p["target"], Decimal("97.19"))                    # 98.60 - 1.41

    def test_or_high_pct_and_price_stops(self):
        self.assertEqual(c(98.60, "or-high", oh=101.0, ol=99.0, side="sell")["stop"], Decimal("101.00"))
        p = c(98.60, "pct:1", side="sell")
        self.assertEqual(p["stop"], Decimal("99.59"))                      # 98.60 x 1.01 = 99.586, up
        self.assertEqual(p["stop_label"], "1% above the entry")
        self.assertEqual(c(98.60, "price:99.50", "price:97.00", side="sell")["reward_risk"], Decimal("1.78"))

    def test_atr_stop_is_above_a_short(self):
        p = c(364.18, "atr:1.5", atr=2.0, n=14, side="sell")
        self.assertEqual(p["stop"], Decimal("367.18"))
        self.assertIn("above the entry", p["stop_label"])

    def test_wrong_side_refusals(self):
        with self.assertRaises(exitp.PlanError) as cm:
            c(98.60, "price:98.00", side="sell")
        self.assertIn("a short's stop goes above the entry", str(cm.exception))
        with self.assertRaises(exitp.PlanError) as cm:
            c(98.60, "price:99.50", "price:99.00", side="sell")
        self.assertIn("a short's target goes below it", str(cm.exception))
        with self.assertRaises(exitp.PlanError) as cm:
            c(10.00, "price:16.00", "2R", side="sell")                       # 10 - 2 x 6 is below $0
        self.assertIn("cannot make more than the whole price", str(cm.exception))
        with self.assertRaises(exitp.PlanError) as cm:
            c(98.60, "price:98.62", side="sell")
        self.assertIn("too tight", str(cm.exception))
        with self.assertRaises(exitp.PlanError) as cm:
            c(98.60, "price:99.50", "price:97.00", side="buy")
        self.assertIn("long's stop goes under", str(cm.exception))


class ShortAnd24hCli(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp)

    def write(self, name, doc):
        with open(os.path.join("trader", name), "w") as fh:
            json.dump(doc, fh)

    def plan(self, *extra):
        return run(exitp, ["plan", "--now", "2026-10-21 08:50", "--json"] + list(extra))

    def test_a_24h_short_entry_makes_a_short_plan(self):
        self.write("entry.json", GOLD_ENTRY)
        code, text, err = self.plan()
        self.assertEqual((code, err), (0, ""))
        p = json.loads(text)
        self.assertEqual((p["side"], p["market"], p["stop"], p["target"]), ("sell", "24h", 100.0, 95.8))
        self.assertEqual(p["session"], {"kind": "24h", "open": "08:20", "close": "17:00", "weekends": False})
        self.assertEqual(p["flat_by"], "16:55")                               # 5 minutes before the close
        self.assertEqual(p["entry_time"], "8:45 AM")
        code, text, _ = run(exitp, ["plan", "--now", "2026-10-21 08:50"])
        self.assertIn("GC=F short", text)
        self.assertIn("Flat by       16:55 ET, win or lose (session 08:20 to 17:00 ET, 24h market)", text)
        self.assertIn("A short has no ceiling", text)
        self.assertIn("Risk a unit", text)

    def test_flat_by_for_24h(self):
        self.write("entry.json", GOLD_ENTRY)
        self.assertEqual(json.loads(self.plan("--flat-by", "15:00")[1])["flat_by"], "15:00")
        code, _, err = self.plan("--flat-by", "17:30")
        self.assertIn("at or after the session close", err)
        code, _, err = self.plan("--flat-by", "08:30")
        self.assertIn("no time left to hold it", err)
        self.write("config.json", {"flat_by_24h": "16:00", "flat_by": "09:00"})
        self.assertEqual(json.loads(self.plan()[1])["flat_by"], "16:00")

    def test_an_overnight_session_flattens_after_midnight(self):
        e = dict(GOLD_ENTRY, session={"kind": "24h", "open": "22:00", "close": "05:00", "weekends": False},
                 date="2026-10-20", time="10:45 PM", bar_index=9)
        self.write("entry.json", e)
        code, text, err = run(exitp, ["plan", "--now", "2026-10-20 22:50", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(text)["flat_by"], "04:55")

    def test_side_flag_must_match_the_entry(self):
        self.write("entry.json", GOLD_ENTRY)
        code, _, err = self.plan("--side", "buy")
        self.assertIn("is a short but --side says buy", err)
        self.assertEqual(self.plan("--side", "short")[0], 0)
        code, _, err = self.plan("--stop", "or-low")
        self.assertIn("or-low is under a short's entry", err)

    def test_flags_only_short_and_stock_market_mismatch(self):
        code, text, err = run(exitp, ["plan", "--symbol", "TSLA", "--entry", "364.18", "--or-high", "370",
                                     "--or-low", "366", "--side", "short", "--now", "2026-10-19 10:15", "--json"])
        self.assertEqual((code, err), (0, ""))
        p = json.loads(text)
        self.assertEqual((p["side"], p["stop"], p["market"]), ("sell", 368.0, "stock"))
        self.write("entry.json", dict(REEL_ENTRY))
        code, _, err = run(exitp, ["plan", "--market", "24h", "--now", "2026-10-19 10:15"])
        self.assertIn("is a stock-hours entry", err)

    def test_atr_for_a_short_uses_the_bars_above_the_entry(self):
        code, text, err = run(exitp, ["plan", "--symbol", "TSLA", "--entry", "364.18", "--side", "sell", "--setup", "orb",
                                     "--or-high", "362.18", "--or-low", "354.47", "--time", "10:15 AM",
                                     "--entry-bar", "8", "--stop", "atr:1", "--offline", TSLA,
                                     "--date", "2026-10-19", "--now", "2026-10-19 10:15", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertGreater(json.loads(text)["stop"], 364.18)

    def test_show_a_short_plan(self):
        self.write("entry.json", GOLD_ENTRY)
        run(exitp, ["plan", "--now", "2026-10-21 08:50", "--write"])
        code, text, _ = run(exitp, ["show", "--now", "2026-10-21 08:50"])
        self.assertEqual(code, 0)
        self.assertIn("GC=F short", text)


class NamedSessions(unittest.TestCase):
    """The london and ny sessions from trader-entry, through the exit plan: short, midpoint stop, R targets."""

    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp)

    def entry_doc(self, **over):
        doc = {"created_by": "trader-entry", "symbol": "GC=F", "setup": "orb", "side": "sell", "market": "24h",
               "session": {"kind": "24h", "open": "03:00", "close": "09:30", "weekends": False, "tz": "london",
                           "local_open": "08:00", "local_close": "14:30", "name": "london",
                           "berlin": {"open": "09:00", "close": "15:30"}},
               "date": "2026-10-21", "status": "triggered", "time": "3:40 AM", "bar_start": "03:35", "bar_index": 7,
               "price": 98.4, "level": 99.0, "interval": "5m",
               "opening_range": {"high": 101.0, "low": 99.0, "minutes": 30}}
        doc.update(over)
        with open(os.path.join("trader", "entry.json"), "w") as fh:
            json.dump(doc, fh)

    def plan(self, *extra):
        return run(exitp, ["plan", "--now", "2026-10-21 03:45", "--json"] + list(extra))

    def test_short_stop_at_the_range_midpoint_and_the_target_in_r(self):
        self.entry_doc()
        for target, want in (("2R", 95.2), ("3R", 93.6), ("1R", 96.8), ("1.5R", 96.0)):
            code, text, err = self.plan("--target", target)
            self.assertEqual((code, err), (0, ""))
            p = json.loads(text)
            self.assertEqual((p["side"], p["stop"], p["target"]), ("sell", 100.0, want), target)   # midpoint (101 + 99) / 2
            self.assertEqual(p["risk_per_share"], 1.6)
        self.assertEqual(json.loads(self.plan("--target", "3R")[1])["reward_risk"], 3.0)

    def test_the_session_rides_along_with_its_zone_and_the_berlin_clock(self):
        self.entry_doc()
        p = json.loads(self.plan()[1])
        self.assertEqual(p["session"], {"kind": "24h", "open": "03:00", "close": "09:30", "weekends": False,
                                        "tz": "london", "local_open": "08:00", "local_close": "14:30",
                                        "name": "london", "berlin": {"open": "09:00", "close": "15:30"}})
        self.assertEqual(p["flat_by"], "09:25")                           # 5 minutes before the session closes
        code, text, _ = run(exitp, ["plan", "--now", "2026-10-21 03:45"])
        self.assertIn("Flat by       09:25 ET, win or lose (session 03:00 to 09:30 ET, 09:00 to 15:30 Berlin, 24h market)", text)

    def test_ny_session_flattens_before_the_stock_close(self):
        self.entry_doc(session={"kind": "24h", "open": "09:30", "close": "16:00", "weekends": False, "tz": "newyork",
                                "local_open": "09:30", "local_close": "16:00", "name": "ny",
                                "berlin": {"open": "15:30", "close": "22:00"}}, time="10:10 AM", bar_index=8)
        code, text, err = run(exitp, ["plan", "--now", "2026-10-21 10:15", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(text)["flat_by"], "15:55")

    def test_the_long_side_of_the_same_range_has_its_stop_at_the_midpoint_too(self):
        self.entry_doc(side="buy", price=101.5, time="3:45 AM", bar_index=8)
        p = json.loads(self.plan()[1])
        self.assertEqual((p["side"], p["stop"], p["target"]), ("buy", 100.0, 104.5))

    def test_the_plan_is_refused_after_the_flat_by_time(self):
        self.entry_doc(time="9:30 AM")
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)
