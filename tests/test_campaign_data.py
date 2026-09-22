"""Tests for UTC partitions and the mark-to-market ledger.

Critical cases include native USDT fees, open losses, cash-only spot accounting
without margin, 5m gaps, and a terminal event exactly at the exclusive end.
"""
import math
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import pandas as pd

    HAS_PANDAS = True
except Exception:
    pd = None
    HAS_PANDAS = False

UTC = timezone.utc
TRAIN_START = datetime(2017, 8, 17, 4, 0, tzinfo=UTC)
TRAIN_END = datetime(2023, 1, 1, tzinfo=UTC)
VAL_START = datetime(2023, 1, 1, tzinfo=UTC)
VAL_END = datetime(2025, 1, 1, tzinfo=UTC)
TEST_START = datetime(2025, 1, 1, tzinfo=UTC)
TEST_END = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)


def _require_pandas(test):
    if not HAS_PANDAS:
        test.skipTest("host sin pandas: solo runtime fijado ejecuta ledger")


def _history():
    from market.history import authorize_partition, partition_bounds

    return partition_bounds, authorize_partition


def _make_prices(start, closes):
    """Vector 5m UTC contiguo con open==close==literal."""
    idx = pd.date_range(start=start, periods=len(closes), freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "date": idx,
            "open": [float(c) for c in closes],
            "high": [float(c) + 0.5 for c in closes],
            "low": [float(c) - 0.5 for c in closes],
            "close": [float(c) for c in closes],
            "volume": [10.0] * len(closes),
        }
    )


def _trade(open_dt, close_dt, **over):
    base = {
        "amount": 2.0,
        "open_rate": 100.0,
        "close_rate": 110.0,
        "fee_open": 0.001,
        "fee_close": 0.001,
        "open_date": open_dt,
        "close_date": close_dt,
        "side": "long",
        "leverage": 1.0,
    }
    base.update(over)
    return base


class PartitionBoundsCase(unittest.TestCase):
    def test_train_bounds_exact(self):
        (partition_bounds, _) = _history()
        start, end = partition_bounds("train")
        self.assertEqual(start, TRAIN_START)
        self.assertEqual(end, TRAIN_END)

    def test_validation_bounds_exact(self):
        (partition_bounds, _) = _history()
        start, end = partition_bounds("validation")
        self.assertEqual(start, VAL_START)
        self.assertEqual(end, VAL_END)

    def test_test_bounds_exact(self):
        (partition_bounds, _) = _history()
        start, end = partition_bounds("test")
        self.assertEqual(start, TEST_START)
        self.assertEqual(end, TEST_END)

    def test_bounds_utc_semiopen_contiguous(self):
        (partition_bounds, _) = _history()
        bounds = {r: partition_bounds(r) for r in ("train", "validation", "test")}
        for role, (start, end) in bounds.items():
            with self.subTest(role=role):
                self.assertIsNotNone(start.tzinfo, role)
                self.assertIsNotNone(end.tzinfo, role)
                self.assertLess(start, end, role)
        self.assertEqual(bounds["train"][1], bounds["validation"][0])
        self.assertEqual(bounds["validation"][1], bounds["test"][0])
        self.assertEqual(bounds["train"][0], TRAIN_START)
        self.assertEqual(bounds["test"][1], TEST_END)
        # No es el TRAIN antiguo 2018 ni un prefijo 2016 inexistente.
        self.assertNotEqual(bounds["train"][0], datetime(2018, 1, 1, tzinfo=UTC))
        self.assertGreater(bounds["train"][0], datetime(2016, 12, 31, tzinfo=UTC))

    def test_rejects_unknown_roles_and_extra_args(self):
        (partition_bounds, _) = _history()
        for bad in (None, "", "TRAIN", "VAL", "TEST", "train ", " all", "reserve", 123):
            with self.subTest(role=bad):
                with self.assertRaises((ValueError, TypeError)):
                    partition_bounds(bad)
        # Sin fechas configurables ni bypass: solo un arg posicional.
        with self.assertRaises(TypeError):
            partition_bounds("train", TRAIN_START)
        with self.assertRaises((ValueError, TypeError)):
            partition_bounds("train", force=True)


class AuthorizePartitionCase(unittest.TestCase):
    def test_train_allows_empty_forbids_holdouts(self):
        (_, authorize) = _history()
        for sel in (None, {}):
            with self.subTest(sel=sel):
                got = authorize("train", sel)
                self.assertEqual(got["start"], TRAIN_START)
                self.assertEqual(got["end"], TRAIN_END)
        for bad in ({"finalists": ["a"]}, {"candidate": "x"}, {"start": "2017"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    authorize("train", bad)

    def test_validation_one_to_three_finalists(self):
        (_, authorize) = _history()
        got = authorize("validation", {"finalists": ["cfg-a"]})
        self.assertEqual(got["finalists"], ["cfg-a"])
        got = authorize("validation", {"finalists": ["a", "b", "c"]})
        self.assertEqual(len(got["finalists"]), 3)
        for bad in ([], ["a", "b", "c", "d"], ["a", "a"], [123], None, {}):
            with self.subTest(bad=bad):
                sel = None if bad is None else ({} if bad == {} else {"finalists": bad})
                with self.assertRaises(ValueError):
                    authorize("validation", sel)

    def test_validation_rejects_bypass_and_dates(self):
        (_, authorize) = _history()
        base = {"finalists": ["a", "b"]}
        for extra in ({"force": True}, {"skip_checks": True}, {"allow_test": True},
                      {"start": VAL_START.isoformat()}, {"bypass": 1}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    authorize("validation", {**base, **extra})

    def test_test_single_consumption(self):
        (_, authorize) = _history()
        got = authorize("test", {"candidate": "cfg-01", "consumed": False})
        self.assertEqual(got["candidate"], "cfg-01")
        self.assertEqual(got["start"], TEST_START)
        self.assertEqual(got["end"], TEST_END)

    def test_test_rejects_reuse_and_wrong_shape(self):
        (_, authorize) = _history()
        with self.assertRaises(ValueError, msg="consumido no se reutiliza"):
            authorize("test", {"candidate": "cfg-01", "consumed": True})
        for bad in (None, {}, {"candidate": ""}, {"finalists": ["a"]},
                    {"candidates": ["a", "b"]}, {"candidate": "a"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    authorize("test", bad)
        with self.assertRaises(ValueError):
            authorize("test", {"candidate": "a", "consumed": False, "force": True})

    def test_authorize_returns_matching_bounds_and_rejects_unknown(self):
        (partition_bounds, authorize) = _history()
        cases = [
            ("train", None),
            ("validation", {"finalists": ["a", "b"]}),
            ("test", {"candidate": "cfg-01", "consumed": False}),
        ]
        for role, sel in cases:
            with self.subTest(role=role):
                start, end = partition_bounds(role)
                got = authorize(role, sel)
                self.assertEqual(got["role"], role)
                self.assertEqual(got["start"], start)
                self.assertEqual(got["end"], end)
        with self.assertRaises((ValueError, TypeError)):
            authorize("all", None)


class LedgerCase(unittest.TestCase):
    WINDOW_START = datetime(2021, 6, 1, 0, 0, tzinfo=UTC)
    WINDOW_END = datetime(2021, 6, 1, 1, 0, tzinfo=UTC)

    def _ledger(self):
        _require_pandas(self)
        from market.equity import build_ledger

        return build_ledger

    def _fixture(self):
        closes = [100.0, 100.0, 80.0, 80.0, 100.0, 100.0,
                  100.0, 100.0, 100.0, 100.0, 100.0, 110.0]
        prices = _make_prices("2021-06-01 00:00", closes)
        trade = _trade(
            datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
            datetime(2021, 6, 1, 0, 55, tzinfo=UTC),
        )
        return prices, [trade]

    def test_open_loss_visible_even_when_closed_profit(self):
        build_ledger = self._ledger()
        prices, trades = self._fixture()
        curve, summary = build_ledger(prices, trades, 1000.0,
                                      self.WINDOW_START, self.WINDOW_END)
        equities = curve["equity"].tolist()
        # Literal independiente: 799.8 + 2*80 = 959.8 en la caida.
        self.assertIn(959.8, [round(v, 1) for v in equities])
        self.assertLess(min(equities), 1000.0, "perdida abierta visible")
        self.assertGreater(summary["final_equity"], 1000.0, "cierre en beneficio")
        self.assertAlmostEqual(summary["final_cash"], 1019.58, delta=0.01)
        self.assertAlmostEqual(summary["fees"], 0.42, delta=1e-9)
        self.assertAlmostEqual(summary["max_drawdown_usdt"], 40.2, delta=0.05)

    def test_peak_includes_initial_and_units_separated(self):
        build_ledger = self._ledger()
        prices, trades = self._fixture()
        _, summary = build_ledger(prices, trades, 1000.0,
                                  self.WINDOW_START, self.WINDOW_END)
        self.assertAlmostEqual(summary["peak"], 1000.0, delta=1e-9)
        usdt = summary["max_drawdown_usdt"]
        pct = summary["max_drawdown_pct"]
        self.assertAlmostEqual(usdt, 40.2, delta=0.05)
        self.assertAlmostEqual(pct, 0.0402, delta=0.0005)
        self.assertGreater(usdt, 1.0, "USDT no es fraccion")
        self.assertLess(pct, 1.0, "pct no es importe")
        self.assertNotAlmostEqual(usdt, pct, delta=1.0, msg="unidades no intercambiables")

    def test_drawdown_pct_is_max_pct_not_abs_over_abs_peak(self):
        """1000->840 (16%) manda sobre 2000->1800 (200 abs pero 10%)."""
        build_ledger = self._ledger()
        start = datetime(2021, 6, 1, 0, 0, tzinfo=UTC)
        end = datetime(2021, 6, 1, 0, 20, tzinfo=UTC)
        prices = _make_prices("2021-06-01 00:00", [100.0, 20.0, 600.0, 500.0])
        trade = _trade(start, end, close_rate=500.0, fee_open=0.0, fee_close=0.0)
        _, summary = build_ledger(prices, [trade], 1000.0, start, end)
        self.assertAlmostEqual(summary["max_drawdown_usdt"], 200.0, delta=1e-6)
        self.assertAlmostEqual(summary["max_drawdown_pct"], 0.16, delta=1e-9)
        self.assertAlmostEqual(summary["peak"], 2000.0, delta=1e-6)

    def test_close_at_end_terminal_cash_in_curve(self):
        """Cierre EXACTO en end: caja 959.64 y DD 40.36 en curva terminal."""
        build_ledger = self._ledger()
        prices = _make_prices("2021-06-01 00:00", [100.0] * 12)
        trade = _trade(self.WINDOW_START, self.WINDOW_END,
                       close_rate=80.0)
        curve, summary = build_ledger(prices, [trade], 1000.0,
                                      self.WINDOW_START, self.WINDOW_END)
        # 799.8 + (2*80 - 0.16) = 959.64; DD 40.36 / 4.036%.
        self.assertAlmostEqual(summary["final_cash"], 959.64, delta=0.01)
        self.assertAlmostEqual(summary["final_equity"], 959.64, delta=0.01)
        self.assertAlmostEqual(summary["max_drawdown_usdt"], 40.36, delta=0.05)
        self.assertAlmostEqual(summary["max_drawdown_pct"], 0.04036, delta=0.0005)
        dates = list(curve["date"])
        self.assertEqual(dates[-1], self.WINDOW_END, "fila EVENTO terminal en end")
        self.assertAlmostEqual(float(curve["equity"].iloc[-1]), 959.64, delta=0.01)
        self.assertAlmostEqual(float(curve["quantity"].iloc[-1]), 0.0, delta=1e-9)
        self.assertTrue(all(d <= self.WINDOW_END for d in dates), "nada futuro")

    def test_reconciles_final_within_001_and_zero_qty(self):
        build_ledger = self._ledger()
        prices, trades = self._fixture()
        curve, summary = build_ledger(prices, trades, 1000.0,
                                      self.WINDOW_START, self.WINDOW_END)
        self.assertAlmostEqual(summary["final_equity"], 1019.58, delta=0.01)
        self.assertAlmostEqual(summary["final_cash"], 1019.58, delta=0.01)
        self.assertAlmostEqual(float(curve["quantity"].iloc[-1]), 0.0, delta=1e-9)
        self.assertAlmostEqual(summary["final_quantity"], 0.0, delta=1e-9)

    def test_fail_closed(self):
        build_ledger = self._ledger()
        prices, trades = self._fixture()
        open_dt = trades[0]["open_date"]
        close_dt = trades[0]["close_date"]
        # Hueco: faltan 2 velas mientras hay posicion abierta.
        gapped = prices.drop(index=[4, 5]).reset_index(drop=True)
        # Fuera de ventana, no finito, corto, apalancado.
        out_trade = _trade(datetime(2021, 5, 31, 0, 0, tzinfo=UTC), close_dt)
        nan_prices = prices.copy()
        nan_prices.loc[3, "close"] = float("nan")
        cases = [
            ("hueco sin invento", gapped, trades),
            ("trade fuera de ventana", prices, [out_trade]),
            ("precio NaN", nan_prices, trades),
            ("corto side", prices, [_trade(open_dt, close_dt, side="short")]),
            ("is_short True sin side", prices, [dict(
                {k: v for k, v in _trade(open_dt, close_dt).items() if k != "side"},
                is_short=True)]),
            ("is_short 1", prices, [_trade(open_dt, close_dt, is_short=1)]),
            ("apalancado", prices, [_trade(open_dt, close_dt, leverage=2.0)]),
            ("amount no finito", prices, [_trade(open_dt, close_dt, amount=float("inf"))]),
            ("fee negativa", prices, [_trade(open_dt, close_dt, fee_open=-0.01)]),
            ("fill 00:01 fuera de grid", prices, [_trade(
                datetime(2021, 6, 1, 0, 1, tzinfo=UTC),
                datetime(2021, 6, 1, 0, 6, tzinfo=UTC))]),
            ("cobertura falta inicio", _make_prices("2021-06-01 00:05", [100.0] * 11), trades),
            ("cobertura falta fin", _make_prices("2021-06-01 00:00", [100.0] * 11), trades),
            ("solape max1", prices, [
                _trade(open_dt, datetime(2021, 6, 1, 0, 20, tzinfo=UTC)),
                _trade(datetime(2021, 6, 1, 0, 5, tzinfo=UTC),
                       datetime(2021, 6, 1, 0, 15, tzinfo=UTC))]),
        ]
        for label, px, tr in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError, msg=label):
                    build_ledger(px, tr, 1000.0, self.WINDOW_START, self.WINDOW_END)

    def test_close_open_same_ts_accepted_any_order_and_intrabar(self):
        """Close+open mismo ts acepta en ambos ordenes; open==close valido."""
        build_ledger = self._ledger()
        prices = _make_prices("2021-06-01 00:00", [100.0] * 12)
        t_a = _trade(datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
                     datetime(2021, 6, 1, 0, 10, tzinfo=UTC),
                     amount=1.0, open_rate=100.0, close_rate=100.0)
        t_b = _trade(datetime(2021, 6, 1, 0, 10, tzinfo=UTC),
                     datetime(2021, 6, 1, 0, 20, tzinfo=UTC),
                     amount=1.0, open_rate=100.0, close_rate=100.0)
        curves = []
        for label, ordered in (("A,B", [t_a, t_b]), ("B,A", [t_b, t_a])):
            with self.subTest(orden=label):
                # Caja justa (150) para que open-antes-que-close falle por
                # margen: solo close-primero es valido en ambos ordenes.
                curve, summary = build_ledger(prices, ordered, 150.0,
                                              self.WINDOW_START, self.WINDOW_END)
                self.assertAlmostEqual(summary["final_cash"], 149.6, delta=0.02)
                self.assertLessEqual(float(curve["quantity"].max()), 1.0 + 1e-9,
                                     "max1: close antes que open")
                curves.append(curve["equity"].tolist())
        if len(curves) == 2:
            self.assertEqual(curves[0], curves[1], "independiente del orden de lista")
        # Misma vela open==close (stop intrabar nativo): valido.
        t_same = _trade(datetime(2021, 6, 1, 0, 10, tzinfo=UTC),
                        datetime(2021, 6, 1, 0, 10, tzinfo=UTC),
                        amount=1.0, open_rate=100.0, close_rate=90.0)
        _, summary = build_ledger(prices, [t_same], 1000.0,
                                  self.WINDOW_START, self.WINDOW_END)
        self.assertAlmostEqual(summary["final_cash"], 989.81, delta=0.02)

    def test_sampling_prices_within_end_ordered(self):
        build_ledger = self._ledger()
        prices, trades = self._fixture()
        curve, _ = build_ledger(prices, trades, 1000.0,
                                self.WINDOW_START, self.WINDOW_END)
        dates = list(curve["date"])
        # Filas de precio en [begin, end); solo un EVENTO terminal puede ser ==end.
        self.assertTrue(all(d <= self.WINDOW_END for d in dates), "nada futuro")
        self.assertTrue(all(d >= self.WINDOW_START for d in dates))
        price_dates = [d for d in dates if d < self.WINDOW_END]
        self.assertEqual(price_dates, sorted(price_dates), "orden temporal")
        self.assertEqual(len(set(dates)), len(dates), "sin duplicados")
        self.assertEqual(len(price_dates), len(prices), "cobertura completa")

    def test_prices_datetime_units_ns_vs_us_equal(self):
        """Dataset nativo ns vs us: mismo resultado (regresion pandas3 .asi8)."""
        build_ledger = self._ledger()
        import pandas as pd

        prices, trades = self._fixture()
        ns = prices.copy()
        ns["date"] = pd.to_datetime(ns["date"], utc=True).astype("datetime64[ns, UTC]")
        us = prices.copy()
        us["date"] = pd.to_datetime(us["date"], utc=True).astype("datetime64[us, UTC]")
        curve_ns, summary_ns = build_ledger(ns, trades, 1000.0,
                                            self.WINDOW_START, self.WINDOW_END)
        curve_us, summary_us = build_ledger(us, trades, 1000.0,
                                            self.WINDOW_START, self.WINDOW_END)
        self.assertEqual(curve_ns["equity"].tolist(), curve_us["equity"].tolist())
        self.assertAlmostEqual(summary_ns["final_cash"], summary_us["final_cash"], delta=1e-9)
        self.assertAlmostEqual(summary_ns["max_drawdown_usdt"],
                               summary_us["max_drawdown_usdt"], delta=1e-9)


class BenchmarkCase(unittest.TestCase):
    def test_comparable_uses_same_profile_from_9900(self):
        from market.equity import buyhold_comparable

        bajo = buyhold_comparable(100.0, 100.0, 9900.0, 0.00125, 0.10, 0.02, 0.001)
        self.assertAlmostEqual(bajo["notional"], 475.9615385, delta=1e-4)
        self.assertAlmostEqual(bajo["pnl"], -0.9519231, delta=1e-4)
        medio = buyhold_comparable(100.0, 100.0, 9900.0, 0.0025, 0.20, 0.02, 0.001)
        self.assertAlmostEqual(medio["notional"], 951.9230769, delta=1e-4)
        self.assertAlmostEqual(medio["pnl"], -1.9038462, delta=1e-4)
        # Fee fuera del principal: qty = notional / first_open.
        self.assertAlmostEqual(medio["quantity"], 951.9230769 / 100.0, delta=1e-9)

    def test_comparable_uses_effective_opens_and_marks_exposure(self):
        from market.equity import buyhold_comparable

        got = buyhold_comparable(100.0, 110.0, 9900.0, 0.0025, 0.20, 0.02, 0.001)
        self.assertAlmostEqual(got["pnl"], 93.1932692, delta=1e-4)
        self.assertIn("exposure", got, "exposicion marcada")
        self.assertAlmostEqual(float(got["exposure"]), 0.20, delta=1e-9)


class TimeWeightedCase(unittest.TestCase):
    def test_g_weighted_by_days_not_episodes(self):
        from market.equity import time_weighted_g

        episodes = [
            {"equity_initial": 1000.0, "equity_final": 1019.58, "days": 1.0},
            {"equity_initial": 1000.0, "equity_final": 990.0, "days": 2.0},
        ]
        got = time_weighted_g(episodes)
        self.assertAlmostEqual(got, 0.00311348, delta=1e-6)
        self.assertGreater(abs(got - 0.00718280), 0.002, "pondera por dias")

    def test_median_excess_per_year_not_per_episode(self):
        from market.equity import median_excess_by_year

        cand = {2019: 0.01, 2020: 0.02, 2021: 0.03}
        bench = {2019: 0.005, 2020: 0.015, 2021: 0.02}
        got = median_excess_by_year(cand, bench)
        self.assertAlmostEqual(got, 0.005, delta=1e-9)
        self.assertLess(got, (0.005 + 0.005 + 0.01) / 3, "mediana, no media")

    def test_median_rejects_mismatched_years(self):
        from market.equity import median_excess_by_year

        with self.assertRaises(ValueError, msg="anos distintos no se intersectan"):
            median_excess_by_year({2019: 0.01, 2020: 0.02},
                                  {2019: 0.005, 2020: 0.015, 2021: 0.02})


if __name__ == "__main__":
    unittest.main()
