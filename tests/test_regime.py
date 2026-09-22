"""Contratos puros del estudio SMA50/200 preregistrado (solo TRAIN)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    pd = None

from research import selection


class RegimeRegistryCase(unittest.TestCase):
    def test_grid_is_exact_and_stable(self):
        from research.regime import generate_variants, neighbors, render_strategy_module

        variants = generate_variants()
        self.assertEqual(len(variants), 24)
        self.assertEqual([v["id"] for v in variants], [f"R{i:03d}" for i in range(24)])
        self.assertEqual(len({v["class_name"] for v in variants}), 24)
        self.assertEqual({v["stop"] for v in variants}, {0.02})
        self.assertEqual({v["risk_profile"] for v in variants}, {"low", "medium", "high"})
        self.assertEqual({tuple(v["params"][k] for k in (
            "reentry", "slope", "early_exit")) for v in variants},
            {(r, s, e) for r in (False, True) for s in (False, True)
             for e in (False, True)})
        self.assertEqual(neighbors("R000"), ["R003", "R006", "R012"])
        self.assertEqual(neighbors("R023"), ["R011", "R017", "R020"])
        rendered = render_strategy_module(variants)
        self.assertEqual(rendered.count("class RegimeCandidateR"), 24)
        variants[0]["params"]["reentry"] = True
        self.assertFalse(generate_variants()[0]["params"]["reentry"])


class RegimeSelectionCase(unittest.TestCase):
    @staticmethod
    def _records():
        from research.regime import generate_variants

        return [{
            "variant_id": v["id"], "role": "train", "year": year,
            "fee": selection.FEE_PRIMARY, "valid": True, "days": 20.0,
            "g": 0.001, "bh_g": 0.0, "max_drawdown_pct": 0.1,
            "trades_nonforced": 30, "turnover": 1.0,
            "g_without_positive_forced": 0.0001,
        } for v in generate_variants() for year in selection.TRAIN_ALL]

    def test_top_three_only_one_per_risk(self):
        from research.regime_selection import choose_train_finalists

        records = self._records()
        self.assertEqual(choose_train_finalists(records), ["R000", "R001", "R002"])
        for rec in records:
            if rec["variant_id"] == "R000" and rec["year"] == 2022:
                rec["trades_nonforced"] = 0
        self.assertEqual(choose_train_finalists(records), ["R001", "R002", "R003"])

    def test_common_249_hour_context_before_every_evaluation_window(self):
        from operations.regime import effective_windows

        snapshot = {"segments_meta": [{
            "index": 0, "seg_dir": "seg00",
            "start": "2019-01-01T00:00:00+00:00",
            "eval_start": "2019-01-09T09:00:00+00:00",
            "end_exclusive": "2019-02-01T00:00:00+00:00",
        }]}
        windows = effective_windows(snapshot)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["start"], "2019-01-11T09:00:00+00:00")


class RegimeSignalCase(unittest.TestCase):
    def test_reentry_early_exit_and_slope_are_causal(self):
        if pd is None:
            self.skipTest("pandas solo en imagen fijada")
        from strategies.regime.SpotRegime import RegimeSpotBase

        prices = [100.0] * 250 + [150.0] * 20 + [110.0, 151.0]
        frame = pd.DataFrame({
            "date": pd.date_range("2020-01-01", periods=len(prices), freq="1h", tz="UTC"),
            "open": prices, "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices], "close": prices,
            "volume": [10.0] * len(prices),
        })

        class Original(RegimeSpotBase):
            _REENTRY = False
            _SLOPE_FILTER = False
            _EARLY_EXIT = False

        class Modified(Original):
            _REENTRY = True
            _EARLY_EXIT = True

        base = Original({})
        altered = Modified({})
        base_frame = base.populate_indicators(frame.copy(), {})
        modified_frame = altered.populate_indicators(frame.copy(), {})
        self.assertEqual(int(base.populate_entry_trend(base_frame.copy(), {}).iloc[-1]["enter_long"]), 0)
        self.assertEqual(int(altered.populate_entry_trend(modified_frame.copy(), {}).iloc[-1]["enter_long"]), 1)
        self.assertEqual(int(base.populate_exit_trend(base_frame.copy(), {}).iloc[-2]["exit_long"]), 0)
        self.assertEqual(int(altered.populate_exit_trend(modified_frame.copy(), {}).iloc[-2]["exit_long"]), 1)

        class Slope(Modified):
            _SLOPE_FILTER = True

        slope = Slope({})
        slope_frame = slope.populate_indicators(frame.copy(), {})
        slope_frame.loc[slope_frame.index[-1], "sma200_prior"] = (
            slope_frame.iloc[-1]["sma200"] + 1.0)
        self.assertEqual(int(slope.populate_entry_trend(slope_frame, {}).iloc[-1]["enter_long"]), 0)
        changed_future = frame.copy()
        changed_future.loc[changed_future.index[-1], "close"] = 1000.0
        prior = altered.populate_entry_trend(altered.populate_indicators(frame.copy(), {}), {})
        future = altered.populate_entry_trend(altered.populate_indicators(changed_future, {}), {})
        self.assertEqual(prior["enter_long"].iloc[:-1].tolist(),
                         future["enter_long"].iloc[:-1].tolist())

    def test_native_image_resolves_all_24_generated_classes(self):
        try:
            import freqtrade  # noqa: F401
        except ImportError:
            self.skipTest("Freqtrade solo en imagen fijada")
        from research.regime import generate_variants, render_strategy_module

        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "RegimeCandidatesGenerated.py"
            generated.write_text(render_strategy_module(), encoding="utf-8")
            proc = subprocess.run([
                sys.executable, "-m", "freqtrade", "list-strategies",
                "--config", "/opt/btc-lab/configs/search.json",
                "--strategy-path", tmp, "--userdir", tmp, "-1",
            ], capture_output=True, text=True, timeout=45, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for variant in generate_variants():
                self.assertIn(variant["class_name"], proc.stdout,
                              f"native resolver: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
