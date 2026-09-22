"""T01 RED: baseline experimental SMA20/50 spot (datos, señales, riesgo, perfil).

Seams reales: market.train (validación/agregación/segmentos), SmaCrossBaseline
como IStrategy con callbacks estándar, y launcher prepare_baseline/run_baseline
coherentes con smoke. Sin duplicar la suite smoke.

Contratos según PRD/plan/T02 (solo spot, sin short):
- TRAIN 5m semiabierto UTC [2018-01-01, 2023-01-01); 1h solo de grupos
  completos de 12 velas alineadas, sin rellenar; segmentos solo por cobertura.
- Señales sobre 1h cerrada con 51 velas consecutivas finitas y volumen
  positivo; cruce SMA20/50 alcista entra, bajista sale; igualdad/warmup/
  hueco/volumen inválido no entran; futuro no cambia pasado; OHLCV intactas.
- Riesgo: cap = min(1000 USDT, 10% capital, 0.25% capital / 0.026, max motor);
  con 10.000 USDT el cap ~= 961.54 USDT (literal trabajado, no fórmula).
  min > cap -> 0; saldo/precio malformado -> 0 sin lanzar (sin fallback del
  motor a proposed_stake); confirmación revalida notional <= cap y solo long.
- Perfil baseline cerrado con DB/rutas propias; inputs smoke/baseline
  cruzados rechazados; smoke conserva garantías.

RED temporal: import condicional solo para ausencia de implementación;
eliminar cuando GREEN. En host sin pandas se omiten explícitamente solo los
tests que exigen pandas/freqtrade (runtime fijado es autoritativo); los tests
stdlib (perfil/launcher) fallan en host y en runtime hasta GREEN.

Run host: python3 -m unittest discover -s tests -v (raíz).
Run runtime: imagen fijada con libs del motor, sin red.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operations import launch

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

try:
    from market.train import aggregate_hourly as _AGG
    from market.train import contiguous_segments as _SEG
    from market.train import validate_ohlcv as _VAL
    _MARKET_ERROR = None
except Exception as exc:  # ausencia de implementación -> RED explícito
    _AGG = _SEG = _VAL = None
    _MARKET_ERROR = exc

try:
    from strategies.baseline.SmaCrossBaseline import SmaCrossBaseline
    _STRATEGY_ERROR = None
except Exception as exc:  # ausencia de implementación -> RED explícito
    SmaCrossBaseline = None
    _STRATEGY_ERROR = exc

ROOT = Path(__file__).resolve().parents[1]
TRAIN_START = datetime(2018, 1, 1, tzinfo=timezone.utc)
TRAIN_END = datetime(2023, 1, 1, tzinfo=timezone.utc)
# Literal trabajado para capital 10.000: 0.25% * 10000 / 0.026 = 25 / 0.026.
BASELINE_CAP_10K = 961.5384615


def _require_pandas(test):
    if not HAS_PANDAS:
        test.skipTest("host sin pandas: solo runtime fijado ejecuta datos/señales")


def _require_strategy(test):
    _require_pandas(test)
    if not HAS_FREQTRADE:
        test.skipTest("host sin freqtrade: solo runtime fijado ejecuta estrategia")
    if SmaCrossBaseline is None:
        test.fail(f"RED: strategies/baseline/SmaCrossBaseline ausente: {_STRATEGY_ERROR!r}")


def _require_market(test):
    _require_pandas(test)
    if _VAL is None or _AGG is None or _SEG is None:
        test.fail(f"RED: market.train ausente: {_MARKET_ERROR!r}")


def _make_5m_frame(n, start, price=100.0, volume=10.0):
    idx = pd.date_range(start=start, periods=n, freq="5min", tz="UTC")
    return DataFrame({
        "date": idx,
        "open": [float(price)] * n,
        "high": [float(price) + 0.5] * n,
        "low": [float(price) - 0.5] * n,
        "close": [float(price)] * n,
        "volume": [float(volume)] * n,
    })


def _make_1h_frame(prices, volumes=None, start="2021-01-01"):
    n = len(prices)
    idx = pd.date_range(start=start, periods=n, freq="1h", tz="UTC")
    vols = list(volumes) if volumes is not None else [10.0] * n
    return DataFrame({
        "date": idx,
        "open": [float(p) for p in prices],
        "high": [float(p) + 0.5 for p in prices],
        "low": [float(p) - 0.5 for p in prices],
        "close": [float(p) for p in prices],
        "volume": [float(v) for v in vols],
    })


def _configure_strategy_wallet(strategy, wallet=10000.0):
    strategy.config = dict(getattr(strategy, "config", {}) or {})
    strategy.config.update({
        "stake_currency": "USDT",
        "dry_run_wallet": wallet,
        "candle_type_def": "spot",
        "runmode": "backtest",
    })
    class _Wallets:
        def get_available_stake_amount(self):
            return wallet
    strategy.wallets = _Wallets()
    return strategy


def _new_strategy(wallet=10000.0):
    cfg = {
        "stake_currency": "USDT",
        "dry_run_wallet": wallet,
        "candle_type_def": "spot",
        "runmode": "backtest",
    }
    strat = SmaCrossBaseline(config=cfg)
    return _configure_strategy_wallet(strat, wallet)


class MarketDataCase(unittest.TestCase):
    def test_validate_acepta_tramo_valido_train(self):
        _require_market(self)
        frame = _make_5m_frame(24, "2021-06-01")
        # No debe lanzar con tramo dentro de TRAIN y OHLCV sanos.
        _VAL(frame, TRAIN_START, TRAIN_END, 5)

    def test_validate_rechaza_duplicados(self):
        _require_market(self)
        frame = _make_5m_frame(12, "2021-06-01")
        dup = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
        with self.assertRaises(ValueError, msg="timestamp duplicado debe rechazarse"):
            _VAL(dup, TRAIN_START, TRAIN_END, 5)

    def test_validate_rechaza_ohlc_invalido_y_no_finito(self):
        _require_market(self)
        bad_variants = []
        base = _make_5m_frame(12, "2021-06-01")
        high_low = base.copy()
        high_low.loc[3, "high"] = high_low.loc[3, "low"] - 1.0
        bad_variants.append(("high<low", high_low))
        outside = base.copy()
        outside.loc[4, "close"] = outside.loc[4, "high"] + 5.0
        bad_variants.append(("close fuera de rango", outside))
        nonfinite = base.copy()
        nonfinite.loc[5, "close"] = float("nan")
        bad_variants.append(("NaN", nonfinite))
        infinite = base.copy()
        infinite.loc[5, "close"] = float("inf")
        bad_variants.append(("inf", infinite))
        nonpositive = base.copy()
        nonpositive.loc[6, "low"] = 0.0
        nonpositive.loc[6, "close"] = 0.0
        bad_variants.append(("precio no positivo", nonpositive))
        negvol = base.copy()
        negvol.loc[7, "volume"] = -1.0
        bad_variants.append(("volumen negativo", negvol))
        for label, bad in bad_variants:
            with self.subTest(label=label):
                with self.assertRaises(ValueError, msg=label):
                    _VAL(bad, TRAIN_START, TRAIN_END, 5)

    def test_validate_rechaza_fuera_de_limites_train(self):
        _require_market(self)
        before = _make_5m_frame(12, "2017-12-31 23:00")
        with self.assertRaises(ValueError, msg="antes de TRAIN"):
            _VAL(before, TRAIN_START, TRAIN_END, 5)
        after = _make_5m_frame(12, "2023-01-01 00:00")
        with self.assertRaises(ValueError, msg="fin TRAIN es exclusivo"):
            _VAL(after, TRAIN_START, TRAIN_END, 5)

    def test_aggregate_hourly_exige_doce_alineadas_sin_relleno(self):
        _require_market(self)
        full = _make_5m_frame(24, "2021-06-01 00:00", price=100.0, volume=2.0)
        # Varía última vela para comprobar OHLC agregados con literales.
        full.loc[11, "high"] = 110.0
        full.loc[0, "low"] = 90.0
        full.loc[0, "open"] = 99.0
        full.loc[11, "close"] = 101.0
        hourly = _AGG(full)
        self.assertEqual(len(hourly), 2, "24 velas 5m -> 2 velas 1h")
        first = hourly.iloc[0]
        self.assertEqual(first["open"], 99.0)
        self.assertEqual(first["high"], 110.0)
        self.assertEqual(first["low"], 90.0)
        self.assertEqual(first["close"], 101.0)
        self.assertEqual(first["volume"], 12 * 2.0)
        short = _make_5m_frame(11, "2021-06-01 00:00")
        self.assertEqual(len(_AGG(short)), 0, "grupo incompleto no se inventa")
        gap = _make_5m_frame(12, "2021-06-01 00:00")
        gap = gap.drop(index=5).reset_index(drop=True)
        self.assertEqual(len(_AGG(gap)), 0, "hueco dentro de la hora no rellena")

    def test_contiguous_segments_solo_por_cobertura(self):
        _require_market(self)
        hourly = _make_1h_frame([100.0] * 30, start="2021-06-01")
        self.assertEqual(len(_SEG(hourly)), 1)
        broken = pd.concat(
            [_make_1h_frame([100.0] * 10, start="2021-06-01"),
             _make_1h_frame([100.0] * 8, start="2021-06-01 13:00")],
            ignore_index=True,
        )
        segs = _SEG(broken)
        self.assertEqual(len(segs), 2, "hueco de 3h parte en dos segmentos")
        self.assertEqual([len(s) for s in segs], [10, 8])
        total = sum(len(s) for s in segs)
        self.assertEqual(total, len(broken), "sin velas inventadas ni perdidas")
        # Segmento corto se conserva (no evaluable, no oculto por rentabilidad).
        tiny = _make_1h_frame([100.0] * 5, start="2021-06-02")
        mixed = pd.concat([hourly, tiny], ignore_index=True)
        # Hay salto temporal entre ambos bloques -> dos segmentos.
        segs2 = _SEG(mixed)
        self.assertTrue(any(len(s) == 5 for s in segs2), "segmento corto visible")


class SignalCase(unittest.TestCase):
    def _signals(self, frame):
        strat = _new_strategy()
        out = strat.populate_indicators(frame.copy(), {"pair": "BTC/USDT"})
        out = strat.populate_entry_trend(out, {"pair": "BTC/USDT"})
        out = strat.populate_exit_trend(out, {"pair": "BTC/USDT"})
        return strat, out

    def test_bull_cross_entra_y_bear_sale(self):
        _require_strategy(self)
        bull_prices = [100.0] * 50 + [200.0]
        strat, out = self._signals(_make_1h_frame(bull_prices))
        self.assertEqual(out["enter_long"].iloc[-1], 1, "cruce alcista entra")
        self.assertEqual(out["exit_long"].iloc[-1], 0)
        self.assertTrue((out["enter_long"].iloc[:-1] == 0).all(), "sin entradas previas")
        bear_prices = [200.0] * 50 + [100.0]
        _, bout = self._signals(_make_1h_frame(bear_prices))
        self.assertEqual(bout["exit_long"].iloc[-1], 1, "cruce bajista sale")
        self.assertEqual(bout["enter_long"].iloc[-1], 0)

    def test_igualdad_y_warmup_no_entran(self):
        _require_strategy(self)
        _, flat = self._signals(_make_1h_frame([100.0] * 60))
        self.assertTrue((flat["enter_long"] == 0).all(), "igualdad SMA no entra")
        self.assertTrue((flat["exit_long"] == 0).all(), "igualdad SMA no sale")
        _, short = self._signals(_make_1h_frame([100.0] * 30 + [200.0]))
        # Solo 31 velas consecutivas (<51): aunque haya salto, warmup bloquea.
        self.assertTrue((short["enter_long"] == 0).all(), "warmup 51 exigido")

    def test_hueco_y_volumen_invalido_bloquean_entrada(self):
        _require_strategy(self)
        prices = [100.0] * 50 + [200.0]
        gapped = _make_1h_frame(prices)
        gapped = gapped.drop(index=25).reset_index(drop=True)
        _, gout = self._signals(gapped)
        self.assertEqual(gout["enter_long"].iloc[-1], 0, "secuencia incompleta no entra")
        for label, vols in [
            ("volumen cero", [10.0] * 50 + [0.0]),
            ("volumen negativo", [10.0] * 50 + [-3.0]),
            ("volumen NaN", [10.0] * 50 + [float("nan")]),
        ]:
            with self.subTest(label=label):
                _, vout = self._signals(_make_1h_frame(prices, volumes=vols))
                self.assertEqual(vout["enter_long"].iloc[-1], 0, label)

    def test_futuro_no_cambia_pasado_y_ohlcv_intactas(self):
        _require_strategy(self)
        prices = [100.0] * 55 + [200.0] * 5
        frame = _make_1h_frame(prices)
        _, before = self._signals(frame)
        past_before = before["enter_long"].iloc[:50].tolist()
        mutated = frame.copy()
        mutated.loc[55:, "close"] = 1.0
        mutated.loc[55:, "open"] = 1.0
        mutated.loc[55:, "high"] = 1.5
        mutated.loc[55:, "low"] = 0.5
        _, after = self._signals(mutated)
        self.assertEqual(after["enter_long"].iloc[:50].tolist(), past_before,
                         "alterar futuro no reescribe pasado")
        for col in ("open", "high", "low", "close", "volume", "date"):
            self.assertIn(col, before.columns, f"columna {col} preservada")
        self.assertTrue((before["date"] == frame["date"]).all(), "fechas intactas")

    def test_spot_solo_largo_sin_cortos(self):
        _require_strategy(self)
        _, flat = self._signals(_make_1h_frame([100.0] * 60))
        self.assertTrue((flat["enter_short"] == 0).all(), "nunca enter_short")
        self.assertTrue((flat["exit_short"] == 0).all(), "nunca exit_short")
        bear = _make_1h_frame([200.0] * 50 + [100.0])
        strat, bout = self._signals(bear)
        self.assertTrue((bout["enter_short"] == 0).all())
        self.assertFalse(strat.can_short, "spot sin cortos")
        self.assertEqual(strat.timeframe, "1h")
        self.assertEqual(strat.stoploss, -0.02)
        self.assertEqual(strat.minimal_roi, {})
        self.assertFalse(getattr(strat, "position_adjustment_enable", False))


class SizingCase(unittest.TestCase):
    def _stake(self, strat, **over):
        now = datetime(2021, 6, 1, tzinfo=timezone.utc)
        params = dict(pair="BTC/USDT", current_time=now, current_rate=20000.0,
                      proposed_stake=1000.0, min_stake=None, max_stake=100000.0,
                      leverage=1.0, entry_tag=None, side="long")
        params.update(over)
        return strat.custom_stake_amount(**params)

    def test_cap_con_capital_10k(self):
        _require_strategy(self)
        strat = _new_strategy(wallet=10000.0)
        got = self._stake(strat)
        self.assertLessEqual(got, 1000.0, "cap nunca supera 1000 USDT")
        self.assertLessEqual(got, 0.1 * 10000.0 + 1e-9)
        # Literal trabajado independiente: 25 / 0.026 ~= 961.54.
        self.assertTrue(961.0 < got < 962.0, f"cap 10k ~= 961.54, fue {got!r}")
        self.assertAlmostEqual(got, BASELINE_CAP_10K, delta=1.0)

    def test_max_motor_y_minimo_por_encima_rechazan(self):
        _require_strategy(self)
        strat = _new_strategy(wallet=10000.0)
        capped = self._stake(strat, max_stake=500.0)
        self.assertTrue(0 < capped <= 500.0, f"max exchange limita, fue {capped!r}")
        blocked = self._stake(strat, min_stake=2000.0, max_stake=100000.0)
        self.assertEqual(blocked, 0, "mínimo por encima del cap -> 0")

    def test_malformado_devuelve_cero_sin_lanzar(self):
        _require_strategy(self)
        strat = _new_strategy(wallet=10000.0)
        bad_cases = [
            ("rate NaN", {"current_rate": float("nan")}),
            ("rate inf", {"current_rate": float("inf")}),
            ("rate cero", {"current_rate": 0.0}),
            ("rate negativo", {"current_rate": -5.0}),
            ("proposed NaN", {"proposed_stake": float("nan")}),
            ("max NaN", {"max_stake": float("nan")}),
            ("max cero", {"max_stake": 0.0}),
            ("min NaN", {"min_stake": float("nan")}),
        ]
        for label, over in bad_cases:
            with self.subTest(label=label):
                try:
                    got = self._stake(strat, **over)
                except Exception as exc:
                    self.fail(f"RED: {label} lanzó {exc!r}, debe devolver 0 sin fallback")
                self.assertEqual(got, 0, label)
        # Saldo inaccesible tampoco lanza ni usa fallback a proposed.
        class _Broken:
            def get_available_stake_amount(self):
                raise RuntimeError("saldo roto")
        strat.wallets = _Broken()
        try:
            got = self._stake(strat)
        except Exception as exc:
            self.fail(f"RED: saldo roto lanzó {exc!r}, debe devolver 0")
        self.assertEqual(got, 0, "fallo de saldo -> 0")

    def test_confirmacion_revalida_notional_solo_long(self):
        _require_strategy(self)
        strat = _new_strategy(wallet=10000.0)
        now = datetime(2021, 6, 1, tzinfo=timezone.utc)
        ok = strat.confirm_trade_entry(
            pair="BTC/USDT", order_type="market", amount=0.04, rate=20000.0,
            time_in_force="GTC", current_time=now, entry_tag="bull", side="long")
        self.assertTrue(ok, "800 USDT <= cap debe confirmar")
        over = strat.confirm_trade_entry(
            pair="BTC/USDT", order_type="market", amount=0.06, rate=20000.0,
            time_in_force="GTC", current_time=now, entry_tag="bull", side="long")
        self.assertFalse(over, "1200 USDT > cap debe rechazar")
        short = strat.confirm_trade_entry(
            pair="BTC/USDT", order_type="market", amount=0.01, rate=20000.0,
            time_in_force="GTC", current_time=now, entry_tag="bull", side="short")
        self.assertFalse(short, "spot no confirma cortos")
        for label, kw in [
            ("amount NaN", {"amount": float("nan"), "rate": 20000.0}),
            ("rate NaN", {"amount": 0.01, "rate": float("nan")}),
            ("amount cero", {"amount": 0.0, "rate": 20000.0}),
        ]:
            with self.subTest(label=label):
                got = strat.confirm_trade_entry(
                    pair="BTC/USDT", order_type="market", time_in_force="GTC",
                    current_time=now, entry_tag="bull", side="long", **kw)
                self.assertFalse(got, label)


class LauncherBaselineCase(unittest.TestCase):
    def test_baseline_config_cerrada_spot(self):
        cfg_path = ROOT / "configs" / "baseline.json"
        self.assertTrue(cfg_path.is_file(), "RED: configs/baseline.json ausente")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("strategy"), "SmaCrossBaseline")
        self.assertEqual(cfg.get("trading_mode"), "spot")
        self.assertIs(cfg.get("dry_run"), True)
        self.assertEqual(cfg.get("timeframe"), "1h")
        self.assertEqual(cfg.get("exchange", {}).get("pair_whitelist"), ["BTC/USDT"])
        self.assertEqual(cfg.get("exchange", {}).get("name"), "binance")
        for alias in ("key", "secret", "password"):
            self.assertEqual(cfg.get("exchange", {}).get(alias), "")
        self.assertFalse(cfg.get("api_server", {}).get("enabled", True))
        self.assertFalse(cfg.get("telegram", {}).get("enabled", True))

    def test_prepare_run_baseline_coherentes_con_smoke(self):
        self.assertTrue(callable(getattr(launch, "prepare_baseline", None)),
                        "RED: operations.launch.prepare_baseline ausente")
        self.assertTrue(callable(getattr(launch, "run_baseline", None)),
                        "RED: operations.launch.run_baseline ausente")
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            # Árbol mínimo para preflight; el mock Git/imagen lo cierra el runner.
            (Path(code) / "configs").mkdir(parents=True, exist_ok=True)
            (Path(code) / "strategies" / "baseline").mkdir(parents=True, exist_ok=True)
            first = launch.prepare_baseline(code, store, launch.PINNED_IMAGE)
            frozen = Path(first).read_bytes()
            second = launch.prepare_baseline(code, store, launch.PINNED_IMAGE)
            self.assertNotEqual(Path(first), Path(second), "cada prepare ruta única")
            self.assertEqual(Path(first).read_bytes(), frozen, "input inmutable")
            manifest = json.loads(Path(first).read_text(encoding="utf-8"))
            self.assertEqual(manifest.get("kind"), "btc-lab-baseline-input")
            self.assertEqual(manifest.get("profile"), "baseline")

    def test_inputs_cruzados_rechazados_y_db_separada(self):
        self.assertTrue(callable(getattr(launch, "run_baseline", None)),
                        "RED: operations.launch.run_baseline ausente")
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            (Path(code) / "configs").mkdir(parents=True, exist_ok=True)
            (Path(code) / "strategies" / "baseline").mkdir(parents=True, exist_ok=True)
            (Path(code) / "strategies" / "smoke").mkdir(parents=True, exist_ok=True)
            smoke_manifest = {
                "kind": "btc-lab-smoke-input", "status": "PREPARED",
                "input_id": "smoke-cross", "commit": "0" * 40,
                "image_ref": launch.PINNED_IMAGE,
            }
            smoke_path = Path(store) / "smoke-cross.json"
            smoke_path.write_text(json.dumps(smoke_manifest), encoding="utf-8")
            with self.assertRaises(ValueError, msg="baseline rechaza input smoke"):
                launch.run_baseline(code, store, str(smoke_path))
            # Rutas efectivas separadas: baseline no reutiliza el comando smoke.
            # Si el implementer expone baseline_command, debe ser propio y sin
            # la DB smoke; si usa otra vía (manifiesto), ese contrato se cierra
            # en GREEN sin imponer nombre aquí.
            baseline_cmd = getattr(launch, "baseline_command", None)
            smoke_cmd = launch.container_command()
            if baseline_cmd is not None:
                argv = baseline_cmd() if callable(baseline_cmd) else baseline_cmd
                joined = " ".join(argv)
                self.assertNotEqual(list(argv), list(smoke_cmd), "comando baseline propio")
                self.assertNotIn("tradesv3.dryrun.sqlite", joined,
                                 "DB baseline separada de smoke")


class EngineIntegrationCase(unittest.TestCase):
    def test_curva_sintetica_produce_entrada_y_salida(self):
        _require_strategy(self)
        # Valle sintético 1h: lateral, subida (bull), meseta, bajada (bear).
        prices = [100.0] * 50 + [110.0] * 10 + [200.0] + [200.0] * 10 + [100.0] * 5
        frame = _make_1h_frame(prices, start="2021-01-01")
        strat = _new_strategy()
        out = strat.populate_indicators(frame.copy(), {"pair": "BTC/USDT"})
        out = strat.populate_entry_trend(out, {"pair": "BTC/USDT"})
        out = strat.populate_exit_trend(out, {"pair": "BTC/USDT"})
        entries = out.index[out["enter_long"] == 1].tolist()
        exits = out.index[out["exit_long"] == 1].tolist()
        self.assertTrue(entries, "integración sintética produce al menos una entrada")
        self.assertTrue(exits, "integración sintética produce al menos una salida")
        self.assertLess(min(entries), max(exits), "entrada precede a salida final")
        self.assertTrue((out["enter_short"] == 0).all())
        self.assertTrue((out["exit_short"] == 0).all())


if __name__ == "__main__":
    unittest.main()
