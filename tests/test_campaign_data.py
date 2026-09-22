"""T01 — particiones por rol y ledger mark-to-market (RED).

Seams propuestos (implementer en PR01):
- market/history.py: partition_bounds(role) -> (start, end) UTC semiabierto.
  Roles exactos: "train", "validation", "test". Sin fechas configurables
  ni flags de bypass.
- market/history.py: authorize_partition(role, selection) -> dict con
  start/end autorizados y seleccion normalizada. TRAIN sin holdouts,
  VALIDATION 1-3 finalistas, TEST 1 candidata con consumo unico.
- market/equity.py: build_ledger(prices_5m, trades, initial_balance,
  window_start, window_end) -> (curve, summary). Contabilidad analitica
  long spot sin DCA/margen, MTM en velas 5m cerradas, reconciliacion
  nativa tol 0.01 USDT.
- market/equity.py: buyhold_comparable(...), time_weighted_g(...),
  median_excess_by_year(...). Benchmark comparable y metricas
  ponderadas por tiempo/ano, no por episodios.

Literales independientes (no copiar el 1039.58 inconsistente del encargo):
amount 2 entry 100 fee .001 -> cash 799.8; precio 80 -> equity 959.8
DD 40.2 USDT / 0.0402; salida 110 fee .001 -> final 1019.58, fees 0.42.
La perdida abierta debe verse aunque el cierre sea beneficio.

Run host (sin pandas): stdlib corre, pandas salta.
Run runtime fijado: imagen con pandas/numpy, sin red.
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
            partition_bounds("train", TRAIN_START)  # type: ignore[arg-type]
        with self.assertRaises((ValueError, TypeError)):
            partition_bounds("train", force=True)  # type: ignore[call-arg]


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
            ("corto", prices, [_trade(open_dt, close_dt, side="short")]),
            ("apalancado", prices, [_trade(open_dt, close_dt, leverage=2.0)]),
            ("amount no finito", prices, [_trade(open_dt, close_dt, amount=float("inf"))]),
            ("fee negativa", prices, [_trade(open_dt, close_dt, fee_open=-0.01)]),
        ]
        for label, px, tr in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError, msg=label):
                    build_ledger(px, tr, 1000.0, self.WINDOW_START, self.WINDOW_END)

    def test_sampling_strictly_within_end_and_ordered(self):
        build_ledger = self._ledger()
        prices, trades = self._fixture()
        curve, _ = build_ledger(prices, trades, 1000.0,
                                self.WINDOW_START, self.WINDOW_END)
        dates = list(curve["date"])
        self.assertTrue(all(d < self.WINDOW_END for d in dates), "end exclusivo")
        self.assertTrue(all(d >= self.WINDOW_START for d in dates))
        self.assertEqual(dates, sorted(dates), "orden temporal")
        self.assertEqual(len(set(dates)), len(dates), "sin duplicados")
        # Fills simultaneos: mismo timestamp, orden de lista determinista.
        t2 = _trade(datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
                    datetime(2021, 6, 1, 0, 55, tzinfo=UTC),
                    amount=1.0, open_rate=100.0, close_rate=100.0)
        _, summary = build_ledger(prices, trades + [t2], 1000.0,
                                  self.WINDOW_START, self.WINDOW_END)
        # Segundo trade 1x(100->100) pierde 0.2 fees: 1019.58-0.2=1019.38.
        self.assertAlmostEqual(summary["final_cash"], 1019.38, delta=0.02)


class BenchmarkCase(unittest.TestCase):
    def test_comparable_uses_same_profile_from_9900(self):
        _require_pandas(self)
        from market.equity import buyhold_comparable

        # Bajo 2%: min(990, 12.375/0.026) = 475.9615... literal.
        bajo = buyhold_comparable(100.0, 100.0, 9900.0, 0.00125, 0.10, 0.02, 0.001)
        self.assertAlmostEqual(bajo["notional"], 475.9615385, delta=1e-4)
        self.assertAlmostEqual(bajo["pnl"], -0.9519231, delta=1e-4)
        # Medio 2%: 951.9231, distinto del Bajo con mismo precio.
        medio = buyhold_comparable(100.0, 100.0, 9900.0, 0.0025, 0.20, 0.02, 0.001)
        self.assertAlmostEqual(medio["notional"], 951.9230769, delta=1e-4)
        self.assertAlmostEqual(medio["pnl"], -1.9038462, delta=1e-4)
        # Fee fuera del principal: qty = notional / first_open.
        self.assertAlmostEqual(medio["quantity"], 951.9230769 / 100.0, delta=1e-9)

    def test_comparable_uses_effective_opens_and_marks_exposure(self):
        _require_pandas(self)
        from market.equity import buyhold_comparable

        got = buyhold_comparable(100.0, 110.0, 9900.0, 0.0025, 0.20, 0.02, 0.001)
        # 9.5192*110*0.999 - 951.9231*1.001 = 93.1932... literal.
        self.assertAlmostEqual(got["pnl"], 93.1932692, delta=1e-4)
        self.assertIn("exposure", got, "exposicion marcada")
        self.assertAlmostEqual(float(got["exposure"]), 0.20, delta=1e-9)


class TimeWeightedCase(unittest.TestCase):
    def test_g_weighted_by_days_not_episodes(self):
        _require_pandas(self)
        from market.equity import time_weighted_g

        episodes = [
            {"equity_initial": 1000.0, "equity_final": 1019.58, "days": 1.0},
            {"equity_initial": 1000.0, "equity_final": 990.0, "days": 2.0},
        ]
        got = time_weighted_g(episodes)
        # (ln1.01958 + ln0.99) / 3 = 0.0031134... literal.
        self.assertAlmostEqual(got, 0.00311348, delta=1e-6)
        # Promedio por episodios (0.00718...) no vale.
        self.assertGreater(abs(got - 0.00718280), 0.002, "pondera por dias")

    def test_median_excess_per_year_not_per_episode(self):
        _require_pandas(self)
        from market.equity import median_excess_by_year

        cand = {2019: 0.01, 2020: 0.02, 2021: 0.03}
        bench = {2019: 0.005, 2020: 0.015, 2021: 0.02}
        # Excesos [0.005, 0.005, 0.01] -> mediana 0.005, media 0.0066.
        got = median_excess_by_year(cand, bench)
        self.assertAlmostEqual(got, 0.005, delta=1e-9)
        self.assertLess(got, (0.005 + 0.005 + 0.01) / 3, "mediana, no media")


if __name__ == "__main__":
    unittest.main()
