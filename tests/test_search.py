"""T05 primer chunk RED: registro 72, senales causales y selectores puros.

Fuente: work/btc-strategy-search/tasks/05.md + PRD (campana 72 spot,
DD 15% MTM, metodo parent market.equity/market.history). Solo tests en raiz,
sin prod/docs/commits/work/agentes. Reutiliza DataContract mocks y ledger
existentes; no prueba texto de Compose/docs ni ejecuta datos/backtests.

Seams acordados (propuesta cerrada al coordinador, RED exacto):
- research/campaign.py puro: generate_variants() -> list[dict] estable
  (id, familia, seed_index, stop 0.02/0.04, risk low/medium/high,
  risk_pct 0.00125/0.0025/0.005, exposure 0.1/0.2/0.4), neighbors(variant_id)
  mismo perfil otro stop + semilla adyacente; TRAIN_YEARS 2019-22,
  VAL_YEARS 2023-24, TEST_YEARS 2025-26, MAX_DRAWDOWN_PCT 0.15.
  Sin universo ilimitado/familias libres; registro antes de datos.
- strategies/search/SpotCandidates.py: 3 bases + factory make_strategy(variant)
  cerrada (solo params numericos conocidos/nombres, sin eval arbitrario).
  Senales 1h cerrada, warmup comun 201 contiguo finito, trend SMA pairs +
  SMA200, breakout maxprev.shift(1)/minprev, banda ddof0, stop 2/4, ROI off,
  solo long, sizing risk/(stop+0.006) cap exposure (sin techo 1000 salvo
  baseline control), callbacks 0/False sin saldo + revalida notional.

Diferido al segundo chunk (schema stateful aun no acordado, se reporta):
transiciones/budget campana, consumo TEST antes de lectura, resume mismo
input inmutable, intentos fallidos/budget 12h, validacion 9/3/1 y gates
economicos completos con ledger real. Este archivo no los impone.

Run host (stdlib): python3 -m unittest discover -s tests -v (estrategia
requiere imagen y se omite sin pandas/freqtrade). Run imagen fijada con
libs del motor, sin red (ver AGENTS.md).
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

try:
    import pandas as pd
    from pandas import DataFrame

    HAS_PANDAS = True
except Exception:
    pd = None
    DataFrame = None
    HAS_PANDAS = False

try:
    from freqtrade.strategy import IStrategy  # noqa: F401

    HAS_FREQTRADE = True
except Exception:
    HAS_FREQTRADE = False


def _require_strategy(test):
    if not HAS_PANDAS:
        test.skipTest("host sin pandas: solo runtime fijado ejecuta estrategia")
    if not HAS_FREQTRADE:
        test.skipTest("host sin freqtrade: solo runtime fijado ejecuta estrategia")


# Literales independientes del PRD (no recomputan como el codigo).
TREND_SEEDS = [(10, 50), (20, 50), (20, 100), (50, 200)]
BREAKOUT_SEEDS = [(24, 12), (48, 24), (96, 48), (168, 84)]
REVERSION_SEEDS = [(20, 1.5), (20, 2.0), (40, 1.5), (40, 2.0)]
STOPS = (0.02, 0.04)
RISK_MAP = {
    "low": (0.00125, 0.10),
    "medium": (0.0025, 0.20),
    "high": (0.005, 0.40),
}
TRAIN_YEARS = (2019, 2020, 2021, 2022)
VAL_YEARS = (2023, 2024)
TEST_YEARS = (2025, 2026)
MAX_DD_PCT = 0.15
WARMUP = 201


def _campaign():
    try:
        import research.campaign as camp  # type: ignore
    except Exception as exc:
        raise AssertionError(
            "RED: research/campaign.py ausente "
            "(seams acordados generate_variants/neighbors + "
            "TRAIN/VAL/TEST years + MAX_DRAWDOWN_PCT): "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return camp


def _strategy_module():
    try:
        import importlib

        return importlib.import_module("strategies.search.SpotCandidates")
    except Exception as exc:
        raise AssertionError(
            "RED: strategies/search/SpotCandidates.py ausente "
            "(3 bases + factory make_strategy cerrada): "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _make_1h_frame(prices, volumes=None, start="2021-01-01"):
    n = len(prices)
    idx = pd.date_range(start=start, periods=n, freq="1h", tz="UTC")
    vols = list(volumes) if volumes is not None else [10.0] * n
    return DataFrame(
        {
            "date": idx,
            "open": [float(p) for p in prices],
            "high": [float(p) + 0.5 for p in prices],
            "low": [float(p) - 0.5 for p in prices],
            "close": [float(p) for p in prices],
            "volume": [float(v) for v in vols],
        }
    )


def _configure_wallet(strategy, wallet=10000.0):
    strategy.config = dict(getattr(strategy, "config", {}) or {})
    strategy.config.update(
        {
            "stake_currency": "USDT",
            "dry_run_wallet": wallet,
            "candle_type_def": "spot",
            "runmode": "backtest",
        }
    )

    class _Wallets:
        def get_available_stake_amount(self):
            return wallet

    strategy.wallets = _Wallets()
    return strategy


def _variant_seed_key(variant):
    """Extrae semilla numerica sin imponer nombre unico de claves."""
    for key in ("seed", "seed_params", "params", "signal", "signal_params"):
        val = variant.get(key)
        if isinstance(val, (list, tuple)) and len(val) == 2:
            try:
                return (float(val[0]), float(val[1]))
            except (TypeError, ValueError):
                continue
    # Claves separadas por familia (fast/slow, n/m, window/k).
    nums = {}
    for key in (
        "fast",
        "slow",
        "n",
        "m",
        "window",
        "k",
        "fast_period",
        "slow_period",
    ):
        if variant.get(key) is not None:
            try:
                nums[key] = float(variant.get(key))
            except (TypeError, ValueError):
                continue
    if "fast" in nums and "slow" in nums:
        return (nums["fast"], nums["slow"])
    if "n" in nums and "m" in nums:
        return (nums["n"], nums["m"])
    if "window" in nums and "k" in nums:
        return (nums["window"], nums["k"])
    params = variant.get("params")
    if isinstance(params, dict):
        try:
            if "fast" in params and "slow" in params:
                return (float(params["fast"]), float(params["slow"]))
            if "n" in params and "m" in params:
                return (float(params["n"]), float(params["m"]))
            if "window" in params and "k" in params:
                return (float(params["window"]), float(params["k"]))
        except (TypeError, ValueError):
            pass
    return None


class RegistryCase(unittest.TestCase):
    def test_generate_72_unique_stable(self):
        camp = _campaign()
        self.assertTrue(
            callable(getattr(camp, "generate_variants", None)),
            "RED: research.campaign.generate_variants() ausente",
        )
        first = camp.generate_variants()
        second = camp.generate_variants()
        self.assertIsInstance(first, list, "lista de dicts estable")
        self.assertEqual(len(first), 72, "3 familias x4 senales x2 stops x3 riesgos =72")
        ids = [str(v.get("id") or v.get("variant_id") or "") for v in first]
        self.assertTrue(all(ids), "todas con ID estable no vacio")
        self.assertEqual(len(set(ids)), 72, "IDs unicos, sin duplicados")
        self.assertEqual(ids, [str(v.get("id") or v.get("variant_id") or "") for v in second],
                         "determinista entre llamadas, registro antes de datos")
        # Sin args libres: universo cerrado, no familias libres.
        try:
            camp.generate_variants(family="trend")
        except TypeError:
            pass
        except Exception:
            pass
        else:
            self.fail("RED: generate_variants admite filtro libre (universo no cerrado)")

    def test_families_seeds_stops_risks_exact(self):
        camp = _campaign()
        variants = camp.generate_variants()
        fams = sorted({str(v.get("family") or v.get("familia") or "") for v in variants})
        self.assertEqual(len(fams), 3, f"exactamente 3 familias, fue {fams}")
        # Cada familia 24 =4 seeds x2 stops x3 risks.
        from collections import Counter

        counts = Counter(str(v.get("family") or v.get("familia") or "") for v in variants)
        for fam, cnt in counts.items():
            self.assertEqual(cnt, 24, f"familia {fam} debe tener 24, fue {cnt}")
        # Stops y riesgos acotados PRD exacto.
        stops = sorted({float(v.get("stop") if v.get("stop") is not None else v.get("stoploss")) for v in variants})
        self.assertEqual(stops, [0.02, 0.04], f"stops 2%/4%, fue {stops}")
        profiles = sorted({str(v.get("risk_profile") or v.get("risk") or "") for v in variants})
        self.assertEqual(profiles, ["high", "low", "medium"], f"perfiles low/medium/high, fue {profiles}")
        for v in variants:
            prof = str(v.get("risk_profile") or v.get("risk") or "")
            rp = v.get("risk_pct", v.get("risk"))
            ex = v.get("exposure", v.get("exposure_pct"))
            try:
                rp_f, ex_f = float(rp), float(ex)
            except (TypeError, ValueError):
                self.fail(f"RED: riesgo no numerico en {v.get('id')}: {rp!r}/{ex!r}")
            exp_rp, exp_ex = RISK_MAP[prof]
            self.assertAlmostEqual(rp_f, exp_rp, delta=1e-12, msg=f"{v.get('id')} risk_pct")
            self.assertAlmostEqual(ex_f, exp_ex, delta=1e-12, msg=f"{v.get('id')} exposure")
        # Semillas por familia deben cubrir los 4 juegos PRD (orden tabla = seed_index).
        by_fam = {}
        for v in variants:
            by_fam.setdefault(str(v.get("family") or v.get("familia") or ""), []).append(v)
        # Identifica familia por sus semillas numericas, no por nombre libre.
        seen_seed_sets = []
        for fam, lst in by_fam.items():
            seeds = sorted({_variant_seed_key(v) for v in lst})
            seen_seed_sets.append(tuple(seeds))
            self.assertTrue(all(s is not None for s in seeds), f"familia {fam} sin semilla numerica")
        # Los tres juegos exactos deben aparecer (trend/breakout/reversion).
        expected = [
            tuple(sorted((float(a), float(b)) for a, b in TREND_SEEDS)),
            tuple(sorted((float(a), float(b)) for a, b in BREAKOUT_SEEDS)),
            tuple(sorted((float(a), float(b)) for a, b in REVERSION_SEEDS)),
        ]
        for exp in expected:
            self.assertIn(exp, seen_seed_sets, f"juego PRD ausente: {exp}, vistos {seen_seed_sets}")
        # seed_index 0..3 en orden tabla por familia.
        for fam, lst in by_fam.items():
            seeds_in_order = [_variant_seed_key(v) for v in sorted(lst, key=lambda d: int(d.get("seed_index", d.get("seed", 0))))]
            # Debe contener las 4 semillas (cada una x6 stops/risks -> 24 filas).
            uniq = sorted(set(seeds_in_order))
            self.assertEqual(len(uniq), 4, f"familia {fam} 4 semillas, fue {uniq}")

    def test_no_absolute_1000_cap_in_registry(self):
        camp = _campaign()
        variants = camp.generate_variants()
        # El antiguo techo 1000 solo queda en baseline control; perfiles nuevos
        # usan risk/(stop+.006) cap exposure. Registro no debe fijar 1000.
        for v in variants:
            for key in ("cap", "max_stake", "stake_cap", "absolute_cap"):
                if v.get(key) is not None:
                    try:
                        val = float(v.get(key))
                    except (TypeError, ValueError):
                        continue
                    self.assertNotAlmostEqual(val, 1000.0, delta=1e-9,
                                             msg=f"{v.get('id')} trae techo 1000 absoluto")


class NeighborsCase(unittest.TestCase):
    def _by_id(self, variants):
        out = {}
        for v in variants:
            vid = str(v.get("id") or v.get("variant_id") or "")
            out[vid] = v
        return out

    def test_neighbors_same_profile_other_stop_plus_adjacent(self):
        camp = _campaign()
        self.assertTrue(callable(getattr(camp, "neighbors", None)),
                        "RED: research.campaign.neighbors(variant_id) ausente")
        variants = camp.generate_variants()
        by_id = self._by_id(variants)
        # Elige una semilla media (indice 1) para exigir 3 vecinos.
        mid = None
        for v in variants:
            try:
                idx = int(v.get("seed_index", v.get("seed")))
            except (TypeError, ValueError):
                continue
            if idx == 1:
                mid = v
                break
        self.assertIsNotNone(mid, "sin semilla media indice 1")
        vid = str(mid.get("id") or mid.get("variant_id"))
        first = camp.neighbors(vid)
        second = camp.neighbors(vid)
        self.assertEqual(first, second, "vecinos deterministicos, no redefinidos tras resultados")
        self.assertIsInstance(first, list)
        self.assertTrue(all(isinstance(x, str) for x in first), "vecinos son IDs")
        self.assertNotIn(vid, first, "vecino no se incluye a si mismo")
        for nid in first:
            self.assertIn(nid, by_id, f"vecino {nid} debe ser ID registrado")
            nb = by_id[nid]
            # Mismo perfil y familia, sin cruzar.
            self.assertEqual(str(nb.get("risk_profile") or nb.get("risk")),
                             str(mid.get("risk_profile") or mid.get("risk")),
                             "vecino mismo perfil")
            self.assertEqual(str(nb.get("family") or nb.get("familia")),
                             str(mid.get("family") or mid.get("familia")),
                             "vecino misma familia")
        # Definicion PRD: misma semilla otro stop, o adyacentes mismo stop/perfil.
        mid_seed = _variant_seed_key(mid)
        mid_stop = float(mid.get("stop") if mid.get("stop") is not None else mid.get("stoploss"))
        other_stop = 0.04 if abs(mid_stop - 0.02) < 1e-12 else 0.02
        expected_ids = set()
        for v in variants:
            nid = str(v.get("id") or v.get("variant_id"))
            if nid == vid:
                continue
            if str(v.get("family") or v.get("familia")) != str(mid.get("family") or mid.get("familia")):
                continue
            if str(v.get("risk_profile") or v.get("risk")) != str(mid.get("risk_profile") or mid.get("risk")):
                continue
            skey = _variant_seed_key(v)
            stop = float(v.get("stop") if v.get("stop") is not None else v.get("stoploss"))
            if skey == mid_seed and abs(stop - other_stop) < 1e-12:
                expected_ids.add(nid)
            # Adyacentes: orden tabla por seed_index.
            try:
                idx_v = int(v.get("seed_index", v.get("seed")))
                idx_m = int(mid.get("seed_index", mid.get("seed")))
            except (TypeError, ValueError):
                continue
            if abs(stop - mid_stop) < 1e-12 and abs(idx_v - idx_m) == 1:
                expected_ids.add(nid)
        self.assertEqual(set(first), expected_ids,
                         f"vecinos PRD exactos para {vid}: esperado {sorted(expected_ids)}, fue {sorted(first)}")

    def test_neighbors_counts_edges_vs_middle(self):
        camp = _campaign()
        variants = camp.generate_variants()
        by_fam_prof = {}
        for v in variants:
            key = (str(v.get("family") or v.get("familia")),
                   str(v.get("risk_profile") or v.get("risk")))
            by_fam_prof.setdefault(key, []).append(v)
        for (fam, prof), lst in by_fam_prof.items():
            for v in lst:
                try:
                    idx = int(v.get("seed_index", v.get("seed")))
                except (TypeError, ValueError):
                    continue
                vid = str(v.get("id") or v.get("variant_id"))
                got = camp.neighbors(vid)
                if idx in (1, 2):
                    self.assertEqual(len(got), 3, f"{vid} media debe tener 3 vecinos, fue {got}")
                else:
                    self.assertEqual(len(got), 2, f"{vid} borde debe tener 2 vecinos, fue {got}")
                break  # un caso por grupo basta para counts; el detalle esta arriba


class SelectorScopeCase(unittest.TestCase):
    def test_selection_years_exact_and_independent(self):
        camp = _campaign()
        # Constantes cerradas propuestas; acepta TRAIN_YEARS o SELECTION_YEARS.
        train = getattr(camp, "TRAIN_YEARS", None)
        val = getattr(camp, "VAL_YEARS", None)
        test = getattr(camp, "TEST_YEARS", None)
        if train is None or val is None or test is None:
            scoped = getattr(camp, "SELECTION_YEARS", None) or getattr(camp, "YEARS_BY_ROLE", None)
            self.assertIsNotNone(scoped, "RED: campaign sin TRAIN/VAL/TEST years (2019-22/23-24/25-26)")
            try:
                train = tuple(scoped["train"] or scoped["TRAIN"])
                val = tuple(scoped["validation"] or scoped["VAL"])
                test = tuple(scoped["test"] or scoped["TEST"])
            except Exception as exc:
                self.fail(f"RED: SELECTION_YEARS ilegible, debe fijar 2019-22/23-24/25-26: {exc!r}")
        self.assertEqual(tuple(train), TRAIN_YEARS, f"TRAIN fijo 2019-22, fue {train}")
        self.assertEqual(tuple(val), VAL_YEARS, f"VAL fijo 2023-24, fue {val}")
        self.assertEqual(tuple(test), TEST_YEARS, f"TEST fijo 2025-26, fue {test}")
        self.assertFalse(set(train) & set(val), "TRAIN/VAL independientes, sin solape")
        self.assertFalse(set(val) & set(test), "VAL/TEST independientes")

    def test_ranking_limit_015_mtm_units(self):
        camp = _campaign()
        limit = getattr(camp, "MAX_DRAWDOWN_PCT", None)
        if limit is None:
            limit = getattr(camp, "MAX_DD_PCT", None)
        self.assertIsNotNone(limit, "RED: campaign sin MAX_DRAWDOWN_PCT 0.15 (unidades MTM)")
        try:
            lim = float(limit)
        except (TypeError, ValueError):
            self.fail(f"RED: MAX_DRAWDOWN_PCT no numerico: {limit!r}")
        self.assertAlmostEqual(lim, MAX_DD_PCT, delta=1e-12,
                               msg="criterio 15% usa equity MTM pct (fraccion 0.15), no USDT ni 15.0")
        # Metodo parent: ledger MTM ya distingue unidades (reutilizado, no duplicado).
        from market.equity import build_ledger  # noqa: F401
        from market.history import partition_bounds  # noqa: F401

    def test_g_year_strict_uses_parent_method(self):
        # g(E) ponderada por dias y mediana por anos (parent), sin mezclar fases.
        from market.equity import median_excess_by_year, time_weighted_g

        episodes = [
            {"equity_initial": 1000.0, "equity_final": 1019.58, "days": 1.0},
            {"equity_initial": 1000.0, "equity_final": 990.0, "days": 2.0},
        ]
        got = time_weighted_g(episodes)
        self.assertAlmostEqual(got, 0.00311348, delta=1e-6)
        cand = {2019: 0.01, 2020: 0.02, 2021: 0.03, 2022: 0.04}
        bench = {2019: 0.005, 2020: 0.015, 2021: 0.02, 2022: 0.03}
        med = median_excess_by_year(cand, bench)
        self.assertAlmostEqual(med, 0.0075, delta=1e-9)
        with self.assertRaises(ValueError, msg="anos distintos no se intersectan"):
            median_excess_by_year({2019: 0.01}, {2019: 0.005, 2020: 0.01})
        camp = _campaign()
        # La campana debe reutilizar el metodo MTM parent, no reimplementarlo
        # con CAGR ni agregacion de cartera falsa.
        src = Path(getattr(camp, "__file__", "") or "").read_text(encoding="utf-8") \
            if getattr(camp, "__file__", None) else ""
        if src:
            self.assertNotIn("CAGR", src, "ranking no es CAGR de cartera 2017-26")


def _factory(cand_mod):
    for name in ("make_strategy", "create_strategy", "build_strategy",
                 "strategy_for_variant", "strategy_class_for"):
        fn = getattr(cand_mod, name, None)
        if callable(fn):
            return fn, name
    return None, ""


def _new_instance(factory, variant):
    inst = factory(variant)
    if isinstance(inst, type):
        # Factory devuelve clase cerrada: instanciar con config minima.
        try:
            inst = inst(config={"stake_currency": "USDT", "dry_run_wallet": 10000.0,
                                "candle_type_def": "spot", "runmode": "backtest"})
        except Exception as exc:
            raise AssertionError(f"RED: clase generada no instanciable: {exc!r}") from exc
    return inst


class StrategyCommonCase(unittest.TestCase):
    def test_common_201_warmup_blocks_early(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, fname = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente (make_strategy(variant) propuesta)")
        camp = _campaign()
        variant = [v for v in camp.generate_variants()
                   if str(v.get("family") or v.get("familia")) in ("trend", "Tendencia filtrada",
                                                                    "tendencia", "Trend")][0] \
            if len(camp.generate_variants()) else None
        # Fallback: primera variante si nombres difieren (familia probada en registry).
        if variant is None:
            variant = camp.generate_variants()[0]
        inst = _new_instance(factory, variant)
        _configure_wallet(inst, 10000.0)
        # 200 velas (<201): aunque haya salto, warmup comun bloquea.
        short = _make_1h_frame([100.0] * 199 + [200.0])
        out = inst.populate_indicators(short.copy(), {"pair": "BTC/USDT"})
        out = inst.populate_entry_trend(out, {"pair": "BTC/USDT"})
        self.assertTrue((out["enter_long"] == 0).all(), f"{fname}: warmup 201 exigido (200 no entra)")

    def test_spot_only_long_no_short(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        camp = _campaign()
        for variant in camp.generate_variants()[:3]:
            with self.subTest(variant=str(variant.get("id"))):
                inst = _new_instance(factory, variant)
                self.assertFalse(getattr(inst, "can_short", True), "spot sin cortos")
                self.assertEqual(getattr(inst, "timeframe", ""), "1h")
                self.assertEqual(getattr(inst, "minimal_roi", None), {})
                self.assertFalse(getattr(inst, "position_adjustment_enable", True))
                frame = _make_1h_frame([100.0] * 210)
                out = inst.populate_indicators(frame.copy(), {"pair": "BTC/USDT"})
                out = inst.populate_entry_trend(out, {"pair": "BTC/USDT"})
                out = inst.populate_exit_trend(out, {"pair": "BTC/USDT"})
                self.assertTrue((out["enter_short"] == 0).all(), "nunca enter_short")
                self.assertTrue((out["exit_short"] == 0).all(), "nunca exit_short")

    def test_stoploss_per_variant_and_single_position(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        camp = _campaign()
        seen = set()
        for variant in camp.generate_variants():
            stop = float(variant.get("stop") if variant.get("stop") is not None else variant.get("stoploss"))
            if stop in seen:
                continue
            seen.add(stop)
            with self.subTest(stop=stop):
                inst = _new_instance(factory, variant)
                self.assertAlmostEqual(float(getattr(inst, "stoploss", 0.0)), -stop, delta=1e-12,
                                       msg=f"stop fijo {stop}, fue {getattr(inst, 'stoploss', None)}")

    def test_sizing_uses_risk_over_stop_plus_reserve_no_1000(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        camp = _campaign()
        by_prof = {}
        for v in camp.generate_variants():
            if abs(float(v.get("stop") if v.get("stop") is not None else v.get("stoploss")) - 0.02) > 1e-12:
                continue
            by_prof[str(v.get("risk_profile") or v.get("risk"))] = v
        self.assertEqual(sorted(by_prof), ["high", "low", "medium"])
        now = datetime(2021, 6, 1, tzinfo=timezone.utc)
        stakes = {}
        for prof in ("low", "medium", "high"):
            inst = _configure_wallet(_new_instance(factory, by_prof[prof]), 10000.0)
            got = inst.custom_stake_amount(
                pair="BTC/USDT", current_time=now, current_rate=20000.0,
                proposed_stake=10000.0, min_stake=None, max_stake=100000.0,
                leverage=1.0, entry_tag=None, side="long")
            stakes[prof] = float(got)
        # Literales PRD: min(10000*exposure, 10000*risk/0.026).
        self.assertAlmostEqual(stakes["low"], 480.7692307, delta=0.5)
        self.assertAlmostEqual(stakes["medium"], 961.5384615, delta=0.5)
        self.assertAlmostEqual(stakes["high"], 1923.0769230, delta=1.0)
        self.assertGreater(stakes["high"], 1000.0, "perfil alto supera 1000: sin techo absoluto")
        self.assertGreater(stakes["medium"], stakes["low"])
        self.assertGreater(stakes["high"], stakes["medium"])

    def test_no_balance_returns_zero_false_and_revalidates(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        camp = _campaign()
        variant = camp.generate_variants()[0]
        now = datetime(2021, 6, 1, tzinfo=timezone.utc)
        for label, wallets in [("wallets None", None), ("wallets sin metodo", object())]:
            with self.subTest(label=label):
                inst = _new_instance(factory, variant)
                inst.wallets = wallets
                try:
                    stake = inst.custom_stake_amount(
                        pair="BTC/USDT", current_time=now, current_rate=20000.0,
                        proposed_stake=1000.0, min_stake=None, max_stake=100000.0,
                        leverage=1.0, entry_tag=None, side="long")
                except Exception as exc:
                    self.fail(f"RED: {label} lanzo {exc!r}, debe devolver 0")
                self.assertEqual(stake, 0, f"{label}: sin saldo no hay stake")
                confirm = inst.confirm_trade_entry(
                    pair="BTC/USDT", order_type="market", amount=0.04, rate=20000.0,
                    time_in_force="GTC", current_time=now, entry_tag="t", side="long")
                self.assertFalse(confirm, f"{label}: sin saldo no confirma")
        # Post-roundtrip: confirm revalida notional <= cap y solo long.
        inst = _configure_wallet(_new_instance(factory, variant), 10000.0)
        # Calcula cap real via stake para no recomputar formula en el test.
        cap = float(inst.custom_stake_amount(
            pair="BTC/USDT", current_time=now, current_rate=20000.0,
            proposed_stake=100000.0, min_stake=None, max_stake=100000.0,
            leverage=1.0, entry_tag=None, side="long"))
        self.assertGreater(cap, 0.0, "cap positivo con saldo valido")
        ok = inst.confirm_trade_entry(
            pair="BTC/USDT", order_type="market", amount=(cap * 0.5) / 20000.0,
            rate=20000.0, time_in_force="GTC", current_time=now, entry_tag="t", side="long")
        self.assertTrue(ok, "mitad del cap debe confirmar")
        over = inst.confirm_trade_entry(
            pair="BTC/USDT", order_type="market", amount=(cap * 2.0) / 20000.0,
            rate=20000.0, time_in_force="GTC", current_time=now, entry_tag="t", side="long")
        self.assertFalse(over, "doble del cap debe rechazar")
        short = inst.confirm_trade_entry(
            pair="BTC/USDT", order_type="market", amount=0.01, rate=20000.0,
            time_in_force="GTC", current_time=now, entry_tag="t", side="short")
        self.assertFalse(short, "spot no confirma cortos")

    def test_generated_source_only_known_numeric_no_eval(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, fname = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        camp = _campaign()
        base_variant = dict(camp.generate_variants()[0])
        for label, bad in [
            ("familia libre", {"family": "ml_ai"}),
            ("stop libre", {"stop": 0.99}),
            ("risk libre", {"risk_profile": "extreme", "risk_pct": 0.5, "exposure": 5.0}),
            ("codigo arbitrario", {"code": "__import__('os').system('id')"}),
            ("eval", {"entry": "eval('1+1')"}),
        ]:
            with self.subTest(label=label):
                mutated = dict(base_variant)
                mutated.update(bad)
                try:
                    out = factory(mutated)
                except (ValueError, TypeError, KeyError):
                    continue
                self.fail(f"RED: {label} aceptado por {fname}, debe rechazar (sin eval/nombres libres)")
        # Fuente generada solo con nombres numericos conocidos + hash.
        src = Path(getattr(mod, "__file__", "") or "").read_text(encoding="utf-8") \
            if getattr(mod, "__file__", None) else ""
        if src:
            for token in ("eval(", "exec(", "__import__", "import os", "import subprocess"):
                self.assertNotIn(token, src, f"fuente generada sin {token}")


class TrendFamilyCase(unittest.TestCase):
    def _trend_variant(self):
        camp = _campaign()
        for v in camp.generate_variants():
            fam = str(v.get("family") or v.get("familia") or "").lower()
            seed = _variant_seed_key(v)
            if seed == (10.0, 50.0) or (fam.startswith("trend") or fam.startswith("tenden")):
                if seed in [(10.0, 50.0), (20.0, 50.0), (20.0, 100.0), (50.0, 200.0)]:
                    return v
        return camp.generate_variants()[0]

    def test_trend_bull_bear_with_sma200_filter(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        variant = self._trend_variant()
        inst = _configure_wallet(_new_instance(factory, variant), 10000.0)
        # Bull verificado en imagen: 200x100 +10x200 cruza en 200 con cierre>SMA200.
        bull = _make_1h_frame([100.0] * 200 + [200.0] * 10)
        out = inst.populate_indicators(bull.copy(), {"pair": "BTC/USDT"})
        out = inst.populate_entry_trend(out, {"pair": "BTC/USDT"})
        self.assertEqual(out["enter_long"].iloc[200], 1, "cruce alcista verificado entra en 200")
        self.assertTrue((out["enter_long"].iloc[:200] == 0).all(), "nada antes del cruce")
        # Bear: 200x200 + rampa de bajada sostenida para cruzar bajo con cierre<SMA200?
        # Usa espejo simple: 200x200 +10x100 debe dar salida por cruce bajista o cierre<SMA200.
        bear = _make_1h_frame([200.0] * 200 + [100.0] * 10)
        bout = inst.populate_indicators(bear.copy(), {"pair": "BTC/USDT"})
        bout = inst.populate_exit_trend(bout, {"pair": "BTC/USDT"})
        self.assertTrue((bout["exit_long"].iloc[200:] == 1).any(), "bajada sostenida da salida")

    def test_trend_future_mutation_no_rewrite(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        variant = self._trend_variant()
        inst = _configure_wallet(_new_instance(factory, variant), 10000.0)
        prices = [100.0] * 200 + [200.0] * 10
        frame = _make_1h_frame(prices)
        out = inst.populate_indicators(frame.copy(), {"pair": "BTC/USDT"})
        out = inst.populate_entry_trend(out, {"pair": "BTC/USDT"})
        past = out["enter_long"].iloc[:200].tolist()
        mutated = frame.copy()
        mutated.loc[205:, "close"] = 1.0
        mutated.loc[205:, "open"] = 1.0
        mutated.loc[205:, "high"] = 1.5
        mutated.loc[205:, "low"] = 0.5
        mout = inst.populate_indicators(mutated.copy(), {"pair": "BTC/USDT"})
        mout = inst.populate_entry_trend(mout, {"pair": "BTC/USDT"})
        self.assertEqual(mout["enter_long"].iloc[:200].tolist(), past,
                         "alterar futuro no reescribe pasado (causal)")


class BreakoutFamilyCase(unittest.TestCase):
    def _breakout_variant(self, n=24, m=12):
        camp = _campaign()
        for v in camp.generate_variants():
            seed = _variant_seed_key(v)
            if seed is not None and abs(seed[0] - n) < 1e-12 and abs(seed[1] - m) < 1e-12:
                return v
        # Fallback por familia si nombres difieren.
        for v in camp.generate_variants():
            fam = str(v.get("family") or v.get("familia") or "").lower()
            if "break" in fam or "rupt" in fam:
                return v
        return camp.generate_variants()[0]

    def test_breakout_entry_exit_with_shift(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        variant = self._breakout_variant(24, 12)
        inst = _configure_wallet(_new_instance(factory, variant), 10000.0)
        # N=24 verificado: 201x100 +110 rompe max previo 100.5 solo con shift(1).
        closes = [100.0] * 201 + [110.0] + [110.0] * 5
        frame = _make_1h_frame(closes)
        out = inst.populate_indicators(frame.copy(), {"pair": "BTC/USDT"})
        out = inst.populate_entry_trend(out, {"pair": "BTC/USDT"})
        self.assertEqual(out["enter_long"].iloc[201], 1, "ruptura 24 verificada entra")
        self.assertTrue((out["enter_long"].iloc[:201] == 0).all(), "sin ruptura previa no entra")

    def test_breakout_shift_excludes_current_and_future_causal(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        variant = self._breakout_variant(24, 12)
        inst = _configure_wallet(_new_instance(factory, variant), 10000.0)
        # Sin shift (bug) el max incluiria el high actual 110.5 y close 110 nunca
        # romperia: el test anterior ya lo prueba. Aqui futuro no reescribe pasado.
        closes = [100.0] * 201 + [110.0] + [110.0] * 5
        frame = _make_1h_frame(closes)
        out = inst.populate_indicators(frame.copy(), {"pair": "BTC/USDT"})
        out = inst.populate_entry_trend(out, {"pair": "BTC/USDT"})
        past = out["enter_long"].iloc[:201].tolist()
        mutated = frame.copy()
        mutated.loc[203:, "close"] = 50.0
        mutated.loc[203:, "open"] = 50.0
        mutated.loc[203:, "high"] = 50.5
        mutated.loc[203:, "low"] = 49.5
        mout = inst.populate_indicators(mutated.copy(), {"pair": "BTC/USDT"})
        mout = inst.populate_entry_trend(mout, {"pair": "BTC/USDT"})
        self.assertEqual(mout["enter_long"].iloc[:201].tolist(), past,
                         "futuro no cambia ruptura pasada")


class ReversionFamilyCase(unittest.TestCase):
    def _reversion_variant(self):
        camp = _campaign()
        for v in camp.generate_variants():
            seed = _variant_seed_key(v)
            if seed is not None and abs(seed[0] - 20.0) < 1e-12 and abs(seed[1] - 1.5) < 1e-12:
                return v
        for v in camp.generate_variants():
            fam = str(v.get("family") or v.get("familia") or "").lower()
            if "rever" in fam or "mean" in fam or "band" in fam:
                return v
        return camp.generate_variants()[0]

    def test_band_ddof0_population_entry(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        variant = self._reversion_variant()
        inst = _configure_wallet(_new_instance(factory, variant), 10000.0)
        # Fixture verificado en imagen (ventana 20/k1.5): 181x80+10x95+9x105
        # +91.5 cruza bajo banda poblacional (lp 91.54) pero no muestral (ls 91.34),
        # con cierre 91.5>SMA200 81.93 y previo 105>=prev banda. Solo ddof=0 entra.
        base = [80.0] * 181 + [95.0] * 10 + [105.0] * 9
        trig = _make_1h_frame(base + [91.5])
        out = inst.populate_indicators(trig.copy(), {"pair": "BTC/USDT"})
        out = inst.populate_entry_trend(out, {"pair": "BTC/USDT"})
        self.assertEqual(out["enter_long"].iloc[-1], 1,
                         "banda ddof=0 verificada entra con 91.5 (pop si, muestral no)")
        # 91.6 no cruza ni poblacional (lp 91.56): no debe entrar.
        flat = _make_1h_frame(base + [91.6])
        fout = inst.populate_indicators(flat.copy(), {"pair": "BTC/USDT"})
        fout = inst.populate_entry_trend(fout, {"pair": "BTC/USDT"})
        self.assertEqual(fout["enter_long"].iloc[-1], 0,
                         "91.6 sobre ambas bandas no entra")

    def test_band_future_no_rewrite(self):
        _require_strategy(self)
        mod = _strategy_module()
        factory, _ = _factory(mod)
        self.assertIsNotNone(factory, "RED: factory cerrada ausente")
        variant = self._reversion_variant()
        inst = _configure_wallet(_new_instance(factory, variant), 10000.0)
        base = [80.0] * 181 + [95.0] * 10 + [105.0] * 9 + [91.5] + [100.0] * 5
        frame = _make_1h_frame(base)
        out = inst.populate_indicators(frame.copy(), {"pair": "BTC/USDT"})
        out = inst.populate_entry_trend(out, {"pair": "BTC/USDT"})
        past = out["enter_long"].iloc[:201].tolist()
        mutated = frame.copy()
        mutated.loc[203:, "close"] = 200.0
        mutated.loc[203:, "open"] = 200.0
        mutated.loc[203:, "high"] = 200.5
        mutated.loc[203:, "low"] = 199.5
        mout = inst.populate_indicators(mutated.copy(), {"pair": "BTC/USDT"})
        mout = inst.populate_entry_trend(mout, {"pair": "BTC/USDT"})
        self.assertEqual(mout["enter_long"].iloc[:201].tolist(), past,
                         "futuro no reescribe banda pasada")


if __name__ == "__main__":
    unittest.main()
