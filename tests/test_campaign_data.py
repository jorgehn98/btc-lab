"""Tests for UTC partitions and the mark-to-market ledger.

Critical cases include native USDT fees, open losses, cash-only spot accounting
without margin, 5m gaps, and a terminal event exactly at the exclusive end.
"""
import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]

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


HIST_COMMIT = "9f3a1c2d4e5b6a7890abcdef1234567890abcde1"


class HistoryLedgerCase(unittest.TestCase):
    """cmd_ledger real sobre snapshot TRAIN eligible (203h: 201 warmup + 2 eval).

    Bug PR01: leia el segmento completo y lo pasaba con ventana eval, por lo
    que todo elegible fallaba. Ademas los fallos no persistian intento FAILED.
    Loader de snapshot, hashes, bounds de rol y cmd_ledger son reales; solo
    el preflight (_load_input/_verify) se dirige a tmp via constantes.
    """

    SEG_START = datetime(2021, 6, 1, 0, 0, tzinfo=UTC)
    N_1H = 203  # >= 201+1 eligible; eval = ultimas 2h (24 filas 5m)

    def _tree(self, tmp):
        _require_pandas(self)
        import pandas as pd

        from operations import history as H
        from operations import launch

        res = Path(tmp) / "hist"
        snaps = res / "snapshots"
        snap_dir = snaps / "train-snap-eval"
        seg_dir = snap_dir / "seg00"
        seg_dir.mkdir(parents=True)
        n1, n5 = self.N_1H, self.N_1H * 12
        idx1 = pd.date_range(start=self.SEG_START, periods=n1, freq="1h", tz="UTC")
        f1 = pd.DataFrame({
            "date": idx1,
            "open": [100.0] * n1,
            "high": [100.5] * n1,
            "low": [99.5] * n1,
            "close": [100.0] * n1,
            "volume": [10.0] * n1,
        })
        idx5 = pd.date_range(start=self.SEG_START, periods=n5, freq="5min", tz="UTC")
        f5 = pd.DataFrame({
            "date": idx5,
            "open": [100.0] * n5,
            "high": [100.5] * n5,
            "low": [99.5] * n5,
            "close": [100.0] * n5,
            "volume": [10.0] * n5,
        })
        whole_5m = snap_dir / H._pair_file(H.PAIR, H.TIMEFRAME_5M)
        whole_1h = snap_dir / H._pair_file(H.PAIR, H.TIMEFRAME_1H)
        seg_5 = seg_dir / H._pair_file(H.PAIR, H.TIMEFRAME_5M)
        seg_1 = seg_dir / H._pair_file(H.PAIR, H.TIMEFRAME_1H)
        f5.to_feather(str(whole_5m))
        f1.to_feather(str(whole_1h))
        f5.to_feather(str(seg_5))
        f1.to_feather(str(seg_1))
        eval_start = self.SEG_START + timedelta(hours=201)
        end_excl = self.SEG_START + timedelta(hours=n1)
        meta = {
            "index": 0,
            "length": n1,
            "start": self.SEG_START.isoformat(),
            "end_last": (end_excl - timedelta(hours=1)).isoformat(),
            "end_exclusive": end_excl.isoformat(),
            "eval_start": eval_start.isoformat(),
            "eligible": True,
            "seg_dir": "seg00",
            "file_5m_sha256": launch.file_hash(seg_5),
            "file_1h_sha256": launch.file_hash(seg_1),
        }
        (snaps / "train-snap-eval.json").write_text(json.dumps({
            "kind": H.SNAPSHOT_KIND, "status": "FROZEN",
            "snapshot_id": "eval", "snapshot_dir": snap_dir.name,
            "role": "train",
            "whole_5m_sha256": launch.file_hash(whole_5m),
            "whole_1h_sha256": launch.file_hash(whole_1h),
            "segments_meta": [meta],
        }), encoding="utf-8")
        rstart, rend = H._role_bounds("train")
        input_path = Path(tmp) / "input.json"
        input_path.write_text(json.dumps({
            "kind": H.INPUT_KIND, "status": "PREPARED",
            "image_ref": launch.PINNED_IMAGE, "commit": HIST_COMMIT,
            "input_id": "in1", "role": "train",
            "range_start": rstart.isoformat(), "range_end": rend.isoformat(),
            **H._code_hashes(ROOT),
        }), encoding="utf-8")
        return res, input_path, meta

    def _eval_trade(self, meta, **over):
        from datetime import datetime as _dt

        estart = _dt.fromisoformat(str(meta["eval_start"]).replace("Z", "+00:00"))
        base = {
            "amount": 1.0,
            "open_rate": 100.0,
            "close_rate": 100.0,
            "fee_open": 0.001,
            "fee_close": 0.001,
            "open_date": estart.isoformat(),
            "close_date": (estart + timedelta(hours=1)).isoformat(),
        }
        base.update(over)
        return base

    def _write_trades(self, res, name, payload):
        sessions = res / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / name).write_text(json.dumps(payload), encoding="utf-8")
        return name

    def _patched(self, H, res, input_path):
        return (
            mock.patch.object(H, "CONTAINER_HISTORY", str(res)),
            mock.patch.object(H, "CONTAINER_INPUT", str(input_path)),
            mock.patch.object(H, "CONTAINER_CODE", str(ROOT)),
        )

    def test_cmd_ledger_trims_warmup_uses_eval_window(self):
        _require_pandas(self)
        from operations import history as H

        with tempfile.TemporaryDirectory() as tmp:
            res, input_path, meta = self._tree(tmp)
            trade = self._eval_trade(meta)
            # PnL nativo: 1x(100->100) pierde 0.2 en fees.
            self._write_trades(res, "trades.json",
                               {"trades": [trade], "profit_total_abs": -0.2})
            p1, p2, p3 = self._patched(H, res, input_path)
            with p1, p2, p3:
                rc = H.cmd_ledger("train-snap-eval.json", 0, "trades.json",
                                  10000.0, -0.2)
            self.assertEqual(rc, 0, "elegible con trades solo-eval debe SUCCESS")
            manifests = list((res / "sessions").glob("ledger-*.json"))
            self.assertEqual(len(manifests), 1, f"un intento: {manifests}")
            payload = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "SUCCEEDED", payload)
            summary = payload["result"]["summary"]
            # Ventana efectiva eval, no segmento completo con warmup.
            self.assertEqual(summary["window_start"], meta["eval_start"])
            self.assertEqual(summary["window_end"], meta["end_exclusive"])
            self.assertAlmostEqual(summary["days_observed"], 2 / 24, delta=1e-9)
            self.assertEqual(payload["result"]["trades"], 1)
            self.assertTrue(payload["result"]["reconcile"]["ok"], payload)

    def test_cmd_ledger_persists_failed_attempt(self):
        _require_pandas(self)
        from operations import history as H

        with tempfile.TemporaryDirectory() as tmp:
            res, input_path, meta = self._tree(tmp)
            # Trade en warmup (fuera de la ventana eval) y profit erroneo.
            bad = self._eval_trade(
                meta,
                open_date=self.SEG_START.isoformat(),
                close_date=(self.SEG_START + timedelta(hours=1)).isoformat(),
            )
            self._write_trades(res, "trades-bad.json", [bad])
            ok_trade = self._eval_trade(meta)
            self._write_trades(res, "trades-ok.json",
                               {"trades": [ok_trade], "profit_total_abs": -0.2})
            p1, p2, p3 = self._patched(H, res, input_path)
            with p1, p2, p3:
                rc_bad = H.cmd_ledger("train-snap-eval.json", 0, "trades-bad.json",
                                      10000.0, None)
            self.assertNotEqual(rc_bad, 0, "trade fuera de eval no es exito")
            failed = list((res / "sessions").glob("ledger-*.json"))
            self.assertEqual(len(failed), 1, f"intento FAILED persistente: {failed}")
            self.assertEqual(json.loads(failed[0].read_text(encoding="utf-8"))["status"],
                             "FAILED")
            with p1, p2, p3:
                rc_wrong = H.cmd_ledger("train-snap-eval.json", 0, "trades-ok.json",
                                        10000.0, 999.0)
            self.assertNotEqual(rc_wrong, 0, "reconcile erroneo no es exito")
            failed = sorted((res / "sessions").glob("ledger-*.json"))
            self.assertEqual(len(failed), 2, f"segundo FAILED persistente: {failed}")
            # Preflight denegado: sin intento nuevo.
            with mock.patch.object(H, "_load_input", side_effect=ValueError("denied")), \
                    mock.patch.object(H, "CONTAINER_HISTORY", str(res)):
                rc_denied = H.cmd_ledger("train-snap-eval.json", 0, "trades-ok.json",
                                         10000.0, None)
            self.assertEqual(rc_denied, 2, "input denegado no crea intento")
            self.assertEqual(len(list((res / "sessions").glob("ledger-*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
