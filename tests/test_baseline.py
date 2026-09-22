"""T01: baseline experimental SMA20/50 spot (datos, señales, riesgo, perfil).

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

RED superado (T02 primer tramo GREEN): imports directos de market.train y
estrategia (fallo en colección si se rompen, sin ocultar bugs). En host sin
pandas/freqtrade se omiten explícitamente solo los tests que exigen esas
libs (runtime fijado es autoritativo); los tests stdlib corren en ambos.

Run host: python3 -m unittest discover -s tests -v (raíz).
Run runtime: imagen fijada con libs del motor, sin red.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

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

from market.train import aggregate_hourly as _AGG
from market.train import contiguous_segments as _SEG
from market.train import validate_ohlcv as _VAL

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


def _require_market(test):
    _require_pandas(test)


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
    from strategies.baseline.SmaCrossBaseline import SmaCrossBaseline

    cfg = {
        "stake_currency": "USDT",
        "dry_run_wallet": wallet,
        "candle_type_def": "spot",
        "runmode": "backtest",
    }
    strat = SmaCrossBaseline(config=cfg)
    return _configure_strategy_wallet(strat, wallet)


BASELINE_COMMIT = "9f3a1c2d4e5b6a7890abcdef1234567890abcde1"
BASELINE_IMAGE_ID = "sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4"


def _write_baseline_tree(root):
    """Árbol realista baseline: config + estrategia + market + research reales.

    research.py es hash obligatorio del manifiesto baseline desde code_root
    (validador común de perfil): el fixture debe incluirlo o prepare falla.
    """
    code = Path(root)
    cfg_dir = code / "configs"
    strat_dir = code / "strategies" / "baseline"
    market_dir = code / "market"
    ops_dir = code / "operations"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    strat_dir.mkdir(parents=True, exist_ok=True)
    market_dir.mkdir(parents=True, exist_ok=True)
    ops_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "baseline.json"
    cfg_file.write_bytes((ROOT / "configs" / "baseline.json").read_bytes())
    strat_file = strat_dir / "SmaCrossBaseline.py"
    strat_file.write_bytes((ROOT / "strategies" / "baseline" / "SmaCrossBaseline.py").read_bytes())
    market_file = market_dir / "train.py"
    market_file.write_bytes((ROOT / "market" / "train.py").read_bytes())
    research_file = ops_dir / "research.py"
    research_file.write_bytes((ROOT / "operations" / "research.py").read_bytes())
    return cfg_file, strat_file, market_file, research_file


def _write_research_tree(root):
    """Árbol con los 6 ficheros hasheados por research (código montado RO)."""
    code = Path(root)
    for rel in ("configs/baseline.json", "strategies/baseline/SmaCrossBaseline.py",
                "market/train.py", "operations/launch.py", "operations/health.py",
                "operations/research.py"):
        src = ROOT / rel
        dst = code / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    return code


@contextlib.contextmanager
def _baseline_git_image(git="clean", image="present"):
    """Preflight simulado como smoke: Git limpio + imagen fijada reales."""
    def fake_run(argv, **kwargs):
        head = argv[0] if isinstance(argv, list) and argv else ""
        text = " ".join(argv) if isinstance(argv, list) else str(argv)
        if head == "git":
            if git == "missing":
                raise FileNotFoundError("git no disponible")
            if "status" in text:
                out = " M configs/baseline.json\n" if git == "dirty" else ""
                return mock.Mock(returncode=0, stdout=out, stderr="")
            return mock.Mock(returncode=0, stdout=BASELINE_COMMIT + "\n", stderr="")
        if head == "docker":
            if image == "missing":
                raise launch.subprocess.CalledProcessError(1, argv, "No such image")
            meta = json.dumps([{"Id": BASELINE_IMAGE_ID,
                                "RepoDigests": [launch.PINNED_IMAGE]}])
            return mock.Mock(returncode=0, stdout=meta, stderr="")
        raise AssertionError(f"consulta subprocess inesperada: {argv!r}")

    with mock.patch.object(launch.subprocess, "run", side_effect=fake_run):
        yield


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

    def test_validate_respeta_subrango_y_exige_utc(self):
        _require_market(self)
        frame = _make_5m_frame(12, "2021-06-01")
        sub_start = datetime(2021, 6, 2, tzinfo=timezone.utc)
        sub_end = datetime(2021, 6, 3, tzinfo=timezone.utc)
        with self.assertRaises(ValueError, msg="ventana pasada manda, no TRAIN global"):
            _VAL(frame, sub_start, sub_end, 5)
        ok = _make_5m_frame(12, "2021-06-02")
        _VAL(ok, sub_start, sub_end, 5)
        naive = _make_5m_frame(12, "2021-06-01")
        naive["date"] = naive["date"].dt.tz_localize(None)
        with self.assertRaises(ValueError, msg="timestamp naive sin UTC"):
            _VAL(naive, TRAIN_START, TRAIN_END, 5)

    def test_validate_rechaza_desalineacion_y_desorden(self):
        _require_market(self)
        base = _make_5m_frame(12, "2021-06-01")
        shifted_min = base.copy()
        shifted_min["date"] = shifted_min["date"] + pd.Timedelta(minutes=1)
        shifted_sec = base.copy()
        shifted_sec["date"] = shifted_sec["date"] + pd.Timedelta(seconds=30)
        reversed_frame = base.iloc[::-1].reset_index(drop=True)
        for label, bad in [
            ("timestamp 5m desplazado 1min", shifted_min),
            ("timestamp con segundos", shifted_sec),
            ("filas en orden inverso", reversed_frame),
        ]:
            with self.subTest(label=label):
                with self.assertRaises(ValueError, msg=label):
                    _VAL(bad, TRAIN_START, TRAIN_END, 5)

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
        # Segmento corto se conserva (no evaluable, no oculto por rentabilidad):
        # segundo bloque después del final del primero + hueco (sin solape).
        last_stamp = hourly["date"].iloc[-1]
        tiny_start = (last_stamp + pd.Timedelta(hours=3)).isoformat()
        tiny = _make_1h_frame([100.0] * 5, start=tiny_start)
        mixed = pd.concat([hourly, tiny], ignore_index=True)
        # Hay salto temporal entre ambos bloques -> dos segmentos.
        segs2 = _SEG(mixed)
        self.assertEqual([len(s) for s in segs2], [30, 5], "segmento corto visible")


class ResearchLoadCase(unittest.TestCase):
    def test_duplicados_no_se_normalizan_silenciosamente(self):
        _require_market(self)
        from operations.research import _load_5m_frame

        def _frame_5m(n, start, close):
            idx = pd.date_range(start=start, periods=n, freq="5min", tz="UTC")
            return DataFrame({
                "date": idx,
                "open": [float(close)] * n,
                "high": [float(close) + 0.5] * n,
                "low": [float(close) - 0.5] * n,
                "close": [float(close)] * n,
                "volume": [10.0] * n,
            })

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            # Un archivo con timestamp duplicado contradictorio.
            single = _frame_5m(12, "2021-06-01", 100.0)
            dup = single.iloc[[0]].copy()
            dup["open"] = 999.0
            dup["high"] = 999.5
            dup["low"] = 998.5
            dup["close"] = 999.0
            conflicted = pd.concat([single, dup], ignore_index=True)
            single_path = tmpdir / "single.feather"
            conflicted.to_feather(str(single_path))
            # Dos archivos con el mismo rango y closes contradictorios.
            file_a = tmpdir / "a.feather"
            file_b = tmpdir / "b.feather"
            _frame_5m(12, "2021-06-01", 100.0).to_feather(str(file_a))
            _frame_5m(12, "2021-06-01", 200.0).to_feather(str(file_b))
            for label, paths in [
                ("duplicado en un archivo", [single_path]),
                ("duplicado en dos archivos", [file_a, file_b]),
            ]:
                with self.subTest(label=label):
                    try:
                        loaded = _load_5m_frame([str(p) for p in paths])
                    except ValueError:
                        continue  # el loader rechaza: comportamiento aceptable
                    with self.assertRaises(ValueError, msg=label):
                        _VAL(loaded, TRAIN_START, TRAIN_END, 5)


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

    def test_sin_saldo_no_hay_entrada(self):
        _require_strategy(self)
        now = datetime(2021, 6, 1, tzinfo=timezone.utc)
        for label, wallets in [
            ("wallets None", None),
            ("wallets sin metodo", object()),
        ]:
            with self.subTest(label=label):
                strat = _new_strategy(wallet=10000.0)
                strat.wallets = wallets
                try:
                    stake = strat.custom_stake_amount(
                        pair="BTC/USDT", current_time=now, current_rate=20000.0,
                        proposed_stake=1000.0, min_stake=None, max_stake=100000.0,
                        leverage=1.0, entry_tag=None, side="long")
                except Exception as exc:
                    self.fail(f"{label} lanzó {exc!r}, debe devolver 0 sin fallback a config")
                self.assertEqual(stake, 0, f"{label}: sin saldo no hay stake")
                confirm = strat.confirm_trade_entry(
                    pair="BTC/USDT", order_type="market", amount=0.04, rate=20000.0,
                    time_in_force="GTC", current_time=now, entry_tag="bull", side="long")
                self.assertFalse(confirm, f"{label}: sin saldo no confirma 800 USDT")

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
                        "operations.launch.prepare_baseline ausente")
        self.assertTrue(callable(getattr(launch, "run_baseline", None)),
                        "operations.launch.run_baseline ausente")
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            _write_baseline_tree(code)
            with _baseline_git_image():
                first = launch.prepare_baseline(code, store, launch.PINNED_IMAGE)
                frozen = Path(first).read_bytes()
                second = launch.prepare_baseline(code, store, launch.PINNED_IMAGE)
            self.assertNotEqual(Path(first), Path(second), "cada prepare ruta única")
            self.assertEqual(Path(first).read_bytes(), frozen, "input inmutable")
            manifest = json.loads(Path(first).read_text(encoding="utf-8"))
            self.assertEqual(manifest.get("kind"), "btc-lab-baseline-input")
            self.assertEqual(manifest.get("profile"), "baseline")

    def test_run_baseline_rechaza_research_alterado_antes_de_ejecutar(self):
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            _write_baseline_tree(code)
            with _baseline_git_image():
                input_path = launch.prepare_baseline(code, store, launch.PINNED_IMAGE)
            manifest = json.loads(Path(input_path).read_text(encoding="utf-8"))
            self.assertIn("research_hash", manifest, "hash research obligatorio")
            self.assertIn("market_hash", manifest, "hash market obligatorio")
            (Path(code) / "operations" / "research.py").write_bytes(b"# alterado\n")
            with mock.patch.object(launch, "CONTAINER_CODE", str(code)), \
                    mock.patch.object(launch, "CONTAINER_STORAGE", str(store)), \
                    mock.patch.object(launch.subprocess, "Popen") as popen:
                with self.assertRaises(ValueError, msg="research alterado frena"):
                    launch.run_baseline(code, store, input_path)
            self.assertFalse(popen.called, "research alterado frena antes del Popen")

    def test_inputs_cruzados_rechazados_y_db_separada(self):
        self.assertTrue(callable(getattr(launch, "run_baseline", None)),
                        "operations.launch.run_baseline ausente")
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            _write_baseline_tree(code)
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
        # Lateral, subida (bull en 50), meseta alta y bajada sostenida:
        # la SMA20 tarda ~15 velas en cruzar bajo la SMA50, 30 da margen.
        prices = [100.0] * 50 + [110.0] * 10 + [200.0] + [200.0] * 10 + [100.0] * 30
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


class ResearchPrepareCase(unittest.TestCase):
    def test_prepare_rechaza_dirty_imagen_mutable_y_config_insegura(self):
        from operations import research

        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            _write_research_tree(code)
            with _baseline_git_image(git="dirty"), \
                    self.assertRaises((ValueError, RuntimeError), msg="git sucio"):
                research.prepare_research(code, store, launch.PINNED_IMAGE)
            with _baseline_git_image(), \
                    self.assertRaises(ValueError, msg="imagen mutable sin digest"):
                research.prepare_research(code, store, "freqtradeorg/freqtrade:2026.8")
            with _baseline_git_image(), \
                    mock.patch.dict(os.environ, {"FREQTRADE__DRY_RUN": "false"}), \
                    self.assertRaises(ValueError, msg="override de entorno"):
                research.prepare_research(code, store, launch.PINNED_IMAGE)
            cfg_path = Path(code) / "configs" / "baseline.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["strategy"] = "OtraEstrategia"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            with _baseline_git_image(), \
                    self.assertRaises(ValueError, msg="config insegura"):
                research.prepare_research(code, store, launch.PINNED_IMAGE)
            leftovers = list(Path(store).rglob("*.json"))
            self.assertEqual(leftovers, [], f"rechazos sin manifiesto: {leftovers}")


class ResearchIdentityCase(unittest.TestCase):
    def test_runtime_rechaza_codigo_alterado_y_perfil_desconocido(self):
        from operations import research

        with tempfile.TemporaryDirectory() as code:
            _write_research_tree(code)
            code_path = Path(code)
            manifest = {
                "kind": research.INPUT_KIND,
                "status": "PREPARED",
                "image_ref": launch.PINNED_IMAGE,
                "commit": BASELINE_COMMIT,
                "input_id": "abc123",
                **research._code_hashes(code_path),
            }
            with mock.patch.object(research, "CONTAINER_CODE", str(code_path)):
                research._verify_current_against_input(manifest)  # control: pasa
                (code_path / "market" / "train.py").write_bytes(b"# alterado\n")
                with self.assertRaises(ValueError, msg="codigo alterado tras prepare"):
                    research._verify_current_against_input(manifest)
            other_profile = dict(manifest, kind="btc-lab-baseline-input")
            unknown_kind = dict(manifest, kind="btc-lab-otro")
            bad_status = dict(manifest, status="RUNNING")
            for label, bad in [
                ("perfil baseline no es research", other_profile),
                ("kind desconocido", unknown_kind),
                ("no PREPARED", bad_status),
            ]:
                with self.subTest(label=label):
                    with self.assertRaises(ValueError, msg=label):
                        research._check_input_manifest(bad)


class ResearchSnapshotCase(unittest.TestCase):
    def _snapshot_tree(self, root):
        from operations import research

        res = Path(root)
        snaps = res / "snapshots"
        snap_dir = snaps / "snap-abc123"
        seg_dir = snap_dir / "seg00"
        seg_dir.mkdir(parents=True, exist_ok=True)
        whole_5m = snap_dir / research._pair_file("BTC/USDT", "5m")
        whole_1h = snap_dir / research._pair_file("BTC/USDT", "1h")
        seg_5m = seg_dir / research._pair_file("BTC/USDT", "5m")
        seg_1h = seg_dir / research._pair_file("BTC/USDT", "1h")
        for target in (whole_5m, whole_1h, seg_5m, seg_1h):
            target.write_bytes(os.urandom(64))
        manifest = {
            "kind": research.SNAPSHOT_KIND,
            "status": "FROZEN",
            "snapshot_id": "abc123",
            "snapshot_dir": snap_dir.name,
            "whole_5m_sha256": launch.file_hash(whole_5m),
            "whole_1h_sha256": launch.file_hash(whole_1h),
            "segments_meta": [{
                "index": 0,
                "seg_dir": "seg00",
                "file_5m_sha256": launch.file_hash(seg_5m),
                "file_1h_sha256": launch.file_hash(seg_1h),
            }],
        }
        (snaps / "snap-abc123.json").write_text(json.dumps(manifest), encoding="utf-8")
        return res, manifest

    def test_snapshot_verifica_hash_faltante_y_contencion(self):
        from operations import research

        with tempfile.TemporaryDirectory() as tmp:
            res, _ = self._snapshot_tree(tmp)
            snap_dir = res / "snapshots" / "snap-abc123"
            seg_1h = snap_dir / "seg00" / research._pair_file("BTC/USDT", "1h")
            seg_5m = snap_dir / "seg00" / research._pair_file("BTC/USDT", "5m")
            whole_1h = snap_dir / research._pair_file("BTC/USDT", "1h")
            with mock.patch.object(research, "CONTAINER_RESEARCH", str(res)):
                research._load_eval_snapshot("snap-abc123.json")  # control: pasa
                with open(whole_1h, "r+b") as handle:
                    handle.seek(0)
                    handle.write(b"\x00")
                with self.assertRaises(ValueError, msg="hash 1h alterado"):
                    research._load_eval_snapshot("snap-abc123.json")
                with open(whole_1h, "r+b") as handle:  # restaura control
                    handle.seek(0)
                    handle.write(os.urandom(1))
                manifest_path = res / "snapshots" / "snap-abc123.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["whole_1h_sha256"] = launch.file_hash(whole_1h)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                seg_5m.unlink()
                with self.assertRaises(ValueError, msg="fichero de segmento ausente"):
                    research._load_eval_snapshot("snap-abc123.json")
                # Contención real: snapshot_dir absoluto fuera de la raíz con
                # ficheros plantados cuyos hashes sí coinciden con el manifiesto.
                seg_5m.write_bytes(os.urandom(64))
                manifest["segments_meta"][0]["file_5m_sha256"] = launch.file_hash(seg_5m)
                evil = Path(tmp) / "evil" / "snap-abc123"
                evil_seg = evil / "seg00"
                evil_seg.mkdir(parents=True)
                for name in (research._pair_file("BTC/USDT", "5m"),
                             research._pair_file("BTC/USDT", "1h")):
                    (evil / name).write_bytes((snap_dir / name).read_bytes())
                    (evil_seg / name).write_bytes((snap_dir / "seg00" / name).read_bytes())
                manifest["snapshot_dir"] = str(evil)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError, msg="snapshot_dir fuera de la raiz"):
                    research._load_eval_snapshot("snap-abc123.json")


class ResearchNativeCase(unittest.TestCase):
    def test_zip_sin_estrategia_o_sin_trades_no_es_exito(self):
        from operations import research

        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            empty = tmpdir / "empty"
            empty.mkdir()
            summary, error = research._summarize_native(empty)
            self.assertIsNone(summary, "sin ZIP no hay resumen")
            self.assertIsNotNone(error, "sin ZIP hay error, no exito silencioso")
            no_strategy = tmpdir / "nostrat"
            no_strategy.mkdir()
            with zipfile.ZipFile(str(no_strategy / "r.zip"), "w") as bundle:
                bundle.writestr("backtest-result-x.json", json.dumps({"otra": {}}))
            summary, error = research._summarize_native(no_strategy)
            self.assertIsNone(summary, "sin metricas de estrategia no hay resumen")
            self.assertIsNotNone(error, "schema sin estrategia es error")
            no_trades = tmpdir / "notrades"
            no_trades.mkdir()
            with zipfile.ZipFile(str(no_trades / "r.zip"), "w") as bundle:
                bundle.writestr("backtest-result-a.json", json.dumps(
                    {"SmaCrossBaseline": {"profit_total_abs": 10.0, "profit_total": 0.01}}))
            summary, error = research._summarize_native(no_trades)
            self.assertIsNotNone(error, "sin total_trades hay error, no exito")
            self.assertIsNone(summary, "sin total_trades no hay resumen usable")
            valid = tmpdir / "valid"
            valid.mkdir()
            trades = [{"enter_tag": "sma_bull", "exit_reason": "exit_signal",
                       "fee_open": 0.1, "fee_close": 0.1}]
            with zipfile.ZipFile(str(valid / "r.zip"), "w") as bundle:
                bundle.writestr("backtest-result-a.json", json.dumps(
                    {"SmaCrossBaseline": {"profit_total_abs": 10.0, "profit_total": 0.01,
                                          "total_trades": 6, "max_drawdown_abs": 5.0,
                                          "trades": trades}}))
            summary, error = research._summarize_native(valid)
            self.assertIsNone(error, f"control valido sin error: {error}")
            self.assertEqual(summary["trades"], 6, "control: 6 trades")


class ResearchLookaheadCase(unittest.TestCase):
    def test_csv_sesgo_baja_cobertura_y_ausente_no_dan_pass(self):
        from operations import research

        header = ("filename,strategy,has_bias,total_signals,biased_entry_signals,"
                  "biased_exit_signals,biased_indicators")
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            biased = tmpdir / "bias.csv"
            biased.write_text(header + "\n"
                              + "f.csv,SmaCrossBaseline,True,12,2,1,sma20\n",
                              encoding="utf-8")
            parsed, error = research._parse_lookahead_csv(biased)
            self.assertIsNone(error, f"CSV con sesgo parsea: {error}")
            self.assertTrue(parsed["has_bias"], "sesgo no se traga: gate es FAIL")
            self.assertGreater(parsed["biased_entry_signals"], 0)
            short = tmpdir / "short.csv"
            short.write_text(header + "\n" + "f.csv,SmaCrossBaseline,False,3,0,0,\n",
                             encoding="utf-8")
            parsed, error = research._parse_lookahead_csv(short)
            self.assertIsNone(error, f"CSV corto parsea: {error}")
            self.assertLess(parsed["total_signals"], 5, "cobertura <5: gate es INCONCLUSIVE")
            parsed, error = research._parse_lookahead_csv(tmpdir / "ausente.csv")
            self.assertIsNone(parsed, "sin CSV no hay parseado que pasar")
            self.assertIsNotNone(error, "sin CSV hay error: gate es INCONCLUSIVE")


class ResearchOwnGateCase(unittest.TestCase):
    def _segmento_largo(self, tmp):
        _require_strategy(self)
        from operations import research

        prices = [100.0] * 500 + [200.0] * 300 + [100.0] * 300
        frame = _make_1h_frame(prices, start="2020-01-01")
        path = Path(tmp) / research._pair_file("BTC/USDT", "1h")
        frame.to_feather(str(path))
        return path

    def test_gate_usa_estrategia_real_y_detecta_recorte_corrupto(self):
        _require_strategy(self)
        from operations import research
        from strategies.baseline.SmaCrossBaseline import SmaCrossBaseline

        with tempfile.TemporaryDirectory() as tmp:
            feather = self._segmento_largo(tmp)
            gate = research._own_sma_gate(feather)
            self.assertEqual(gate.get("verdict"), "PASS", f"control real: {gate}")
            self.assertEqual(gate.get("checked_events"), 2, f"ambos cruces: {gate}")
            real_entry = SmaCrossBaseline.populate_entry_trend

            def corrupta(self, dataframe, metadata):
                out = real_entry(self, dataframe, metadata)
                if len(dataframe) < 1000:
                    out["enter_long"] = 0  # un recorte truncado pierde la entrada
                return out

            with mock.patch.object(SmaCrossBaseline, "populate_entry_trend", corrupta):
                gate = research._own_sma_gate(feather)
            self.assertEqual(gate.get("verdict"), "FAIL", f"recorte corrupto: {gate}")
            self.assertIn("enter_long", str(gate.get("reason", "")))


class ResearchBacktestCase(unittest.TestCase):
    def test_backtest_usa_recortes_congelados_y_rango_efectivo(self):
        from operations import research

        seg = Path("/snap/seg00")
        argv = research._backtest_argv(
            seg, Path("/sess/u"), "20210603T0300-20210604T0000", 0.001, Path("/sess/native"))
        self.assertIn("--datadir", argv)
        self.assertEqual(argv[argv.index("--datadir") + 1], str(seg),
                         "datadir es el recorte congelado, no el staging")
        self.assertEqual(argv[argv.index("--timerange") + 1], "20210603T0300-20210604T0000")
        self.assertEqual(argv[argv.index("--timeframe-detail") + 1], "5m")
        self.assertEqual(argv[argv.index("--fee") + 1], "0.001")
        self.assertEqual(argv[argv.index("--strategy") + 1], "SmaCrossBaseline")
        # warmup 51h: inicio efectivo = inicio + 51h en formato nativo.
        self.assertEqual(
            research._timerange_for_eval(
                datetime(2021, 6, 1, tzinfo=timezone.utc),
                datetime(2021, 6, 3, 3, tzinfo=timezone.utc)),
            "20210601T0000-20210603T0300")

    def test_backtest_sin_elegibles_es_inconclusive_sin_motor(self):
        # cmd_backtest importa pandas al arrancar: runtime autoritativo.
        _require_market(self)
        from operations import research

        with tempfile.TemporaryDirectory() as tmp:
            res = Path(tmp) / "res"
            snaps = res / "snapshots"
            snap_dir = snaps / "snap-corto"
            snap_dir.mkdir(parents=True)
            whole_5m = snap_dir / research._pair_file("BTC/USDT", "5m")
            whole_1h = snap_dir / research._pair_file("BTC/USDT", "1h")
            whole_5m.write_bytes(os.urandom(32))
            whole_1h.write_bytes(os.urandom(32))
            seg_dir = snap_dir / "seg00"
            seg_dir.mkdir(parents=True)
            seg_5m = seg_dir / research._pair_file("BTC/USDT", "5m")
            seg_1h = seg_dir / research._pair_file("BTC/USDT", "1h")
            seg_5m.write_bytes(os.urandom(32))
            seg_1h.write_bytes(os.urandom(32))
            manifest = {
                "kind": research.SNAPSHOT_KIND,
                "status": "FROZEN",
                "snapshot_id": "corto",
                "snapshot_dir": snap_dir.name,
                "whole_5m_sha256": launch.file_hash(whole_5m),
                "whole_1h_sha256": launch.file_hash(whole_1h),
                "segments_meta": [{
                    "index": 0,
                    "length": 30,
                    "start": "2021-06-01T00:00:00+00:00",
                    "end_exclusive": "2021-06-02T00:00:00+00:00",
                    "seg_dir": "seg00",
                    "file_5m_sha256": launch.file_hash(seg_5m),
                    "file_1h_sha256": launch.file_hash(seg_1h),
                }],
            }
            (snaps / "snap-corto.json").write_text(json.dumps(manifest), encoding="utf-8")
            hashes = research._code_hashes(ROOT)
            research_input = {
                "kind": research.INPUT_KIND,
                "status": "PREPARED",
                "image_ref": launch.PINNED_IMAGE,
                "commit": BASELINE_COMMIT,
                "input_id": "in1",
                **hashes,
            }
            input_path = Path(tmp) / "input.json"
            input_path.write_text(json.dumps(research_input), encoding="utf-8")
            with mock.patch.object(research, "CONTAINER_RESEARCH", str(res)), \
                    mock.patch.object(research, "CONTAINER_INPUT", str(input_path)), \
                    mock.patch.object(research, "CONTAINER_CODE", str(ROOT)), \
                    mock.patch.object(research, "_run_streaming",
                                      side_effect=AssertionError("motor no debe llamarse")):
                rc = research.cmd_backtest("snap-corto.json")
            self.assertEqual(rc, 1, "sin elegibles no es exito")
            sessions = list((res / "sessions").glob("backtest-*/session.json"))
            self.assertEqual(len(sessions), 1, f"una sesion: {sessions}")
            session = json.loads(sessions[0].read_text(encoding="utf-8"))
            self.assertEqual(session.get("status"), "INCONCLUSIVE", session)


class ResearchCliCase(unittest.TestCase):
    def test_help_expone_comandos_reales(self):
        from operations import research

        for argv in (["--help"], ["prepare", "--help"], ["download", "--help"],
                     ["snapshot", "--help"], ["backtest", "--help"], ["bias", "--help"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        research.main(argv)
                self.assertEqual(ctx.exception.code, 0, f"{argv} ofrece --help real")


if __name__ == "__main__":
    unittest.main()
