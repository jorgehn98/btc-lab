"""research.evaluation: calculo analitico de metricas de campana (T06).

Separa calculo/reportes del runner (operations.search). Reutiliza el metodo
parent market.equity (ledger MTM, reconciliacion, BH comparable, g ponderada)
y market.history (bounds fijos). Sin CAGR ni cartera global: agregacion por
ano con interseccion episodio/ano, capital independiente 10000 por episodio.

Esquema de registro exacto research.selection (12 campos):
  variant_id/role/year/fee/valid/days/g/bh_g/max_drawdown_pct/
  trades_nonforced/turnover/g_without_positive_forced
Turnover por ano = notional total/10000 sumado (no tasa diaria).
Forced stress: retira SOLO la contribucion positiva de force_exit del wealth
terminal y recalcula g ponderada; conserva perdidas.
Cero trades con datos reales: g0 valido, no se elimina.
Incompleto/ausente: valid False, nunca descarte silencioso.
"""

from __future__ import annotations

import math
import zipfile
from datetime import datetime, timezone
from pathlib import Path

WARMUP_BARS = 201
INITIAL_BALANCE = 10000.0
AVAILABLE_BALANCE = 9900.0
RESERVE = 0.006
TOL_RECONCILE = 0.01
FEES_ALL = (0.001, 0.0015, 0.002, 0.003)
FEES_SCREEN = (0.001, 0.002)
FEES_STRESS = (0.0015, 0.003)
MIN_BIAS_TRADES = 5
RECURSIVE_WINDOWS = (201, 400, 800, 1200)
MIN_RECURSIVE_BARS = 1201
SMA_TOL = 1e-10

ENTRY_TAGS = {
    "trend": {"trend_bull"},
    "breakout": {"breakout_high"},
    "reversion": {"reversion_band"},
}
EXIT_TAGS = {
    "trend": {"trend_exit"},
    "breakout": {"breakout_exit"},
    "reversion": {"reversion_exit"},
}
# Exit reasons nativos legitimos ademas del tag de senal (stop fijo 2/4%).
# force_exit se excluye del conteo analizable pero conserva perdidas en stress.
ALLOWED_EXIT_REASONS = {
    "trend": {"trend_exit", "exit_signal", "stop_loss", "stoploss"},
    "breakout": {"breakout_exit", "exit_signal", "stop_loss", "stoploss"},
    "reversion": {"reversion_exit", "exit_signal", "stop_loss", "stoploss"},
}


def _utc(dt):
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    text = str(dt).strip().replace("Z", "+00:00")
    out = datetime.fromisoformat(text)
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def role_years(role):
    """Anos cerrados por rol (PRD, sin configuracion CLI)."""
    from research.campaign import TEST_YEARS, TRAIN_YEARS, VAL_YEARS

    if role == "train":
        # Screen cubre todo TRAIN para DD (2017-22); ranking usa 2019-22.
        return (2017, 2018, 2019, 2020, 2021, 2022)
    if role == "train_sel":
        return tuple(TRAIN_YEARS)
    if role == "validation":
        return tuple(VAL_YEARS)
    if role == "test":
        return tuple(TEST_YEARS)
    raise ValueError(f"rol desconocido: {role!r}")


def plan_windows(role, snapshot_manifest):
    """Ventanas efectivas por ano+segmento contiguo.

    start=max(year_start, seg.eval_start), end=min(year_end, seg.end_exclusive),
    warmup 201 del mismo pasado contiguo (context_only, fuera de metricas).
    Misma ventana para todas las candidatas y controles. Cortas sin velas
    se registran short not-evaluable (valid False en agregacion).
    """
    from market.history import partition_bounds

    if role not in ("train", "validation", "test"):
        raise ValueError(f"rol desconocido: {role!r}")
    if not isinstance(snapshot_manifest, dict):
        raise ValueError("snapshot_manifest debe ser dict")
    seg_metas = snapshot_manifest.get("segments_meta") or []
    if not seg_metas:
        raise ValueError("snapshot sin segments_meta")
    rstart, rend = partition_bounds(role)
    # Anos calendario intersectados con el rol (2017 parcial desde 08-17).
    years = sorted({d.year for d in (rstart, rend)} | set(
        y for y in range(rstart.year, rend.year + 1)))
    # Recorta a anos con interseccion real con el rol.
    years = [y for y in years if not (
        datetime(y + 1, 1, 1, tzinfo=timezone.utc) <= rstart
        or datetime(y, 1, 1, tzinfo=timezone.utc) >= rend)]
    windows = []
    for year in years:
        ystart = max(datetime(year, 1, 1, tzinfo=timezone.utc), rstart)
        yend = min(datetime(year + 1, 1, 1, tzinfo=timezone.utc), rend)
        # Sin datos antes de 2017 ni 2016 inventado: el snapshot TRAIN empieza
        # 2017-08-17; anos fuera del snapshot no generan ventanas.
        for meta in seg_metas:
            try:
                seg_start = _utc(meta["start"])
                seg_end = _utc(meta["end_exclusive"])
                seg_eval = _utc(meta.get("eval_start") or meta["start"])
            except Exception as exc:
                raise ValueError(f"meta fechas ilegibles seg{meta.get('index')}: {exc}") from exc
            start = max(ystart, seg_eval)
            end = min(yend, seg_end)
            if start >= end:
                continue
            days = (end - start).total_seconds() / 86400.0
            windows.append({
                "role": role,
                "year": int(year),
                "seg_index": int(meta["index"]),
                "seg_dir": str(meta.get("seg_dir") or f"seg{int(meta['index']):02d}"),
                "seg_start": seg_start.isoformat(),
                "seg_end_exclusive": seg_end.isoformat(),
                "seg_eval_start": seg_eval.isoformat(),
                "start": start.isoformat(),
                "end_exclusive": end.isoformat(),
                "days": float(days),
                "short": bool(days <= 0.0),
            })
    windows.sort(key=lambda w: (w["year"], w["seg_index"]))
    return windows


def timerange_fmt(start_iso, end_iso):
    """Formato nativo YYYYMMDDTHHMM-YYYYMMDDTHHMM."""
    start = _utc(start_iso)
    end = _utc(end_iso)
    fmt = "%Y%m%dT%H%M"
    return f"{start.strftime(fmt)}-{end.strftime(fmt)}"


def turnover_for_trades(trades):
    """Suma notional entrada+salida /10000 (no tasa diaria)."""
    total = 0.0
    for trade in trades or []:
        if not isinstance(trade, dict):
            raise ValueError("trade debe ser objeto")
        try:
            amount = float(trade["amount"])
            open_rate = float(trade["open_rate"])
            close_rate = float(trade["close_rate"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("trade sin amount/open_rate/close_rate numericos") from None
        if not (math.isfinite(amount) and math.isfinite(open_rate)
                and math.isfinite(close_rate)):
            raise ValueError("trade no finito")
        if amount <= 0.0 or open_rate <= 0.0 or close_rate <= 0.0:
            raise ValueError("trade debe ser >0")
        total += amount * open_rate + amount * close_rate
    return float(total) / float(INITIAL_BALANCE)


def nonforced_count(trades):
    """Conteo sin force_exit exacto (frontera conservada aparte)."""
    count = 0
    for trade in trades or []:
        if str((trade or {}).get("exit_reason") or "") != "force_exit":
            count += 1
    return int(count)


def forced_positive_pnl(trades):
    """PnL atribuible a force_exit (costes incluidos); perdidas se conservan."""
    total = 0.0
    for trade in trades or []:
        if str((trade or {}).get("exit_reason") or "") != "force_exit":
            continue
        try:
            amount = float(trade["amount"])
            open_rate = float(trade["open_rate"])
            close_rate = float(trade["close_rate"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("forced sin notionals") from None
        foc = trade.get("fee_open_cost")
        fcc = trade.get("fee_close_cost")
        if foc is not None or fcc is not None:
            try:
                foc_f = float(foc)
                fcc_f = float(fcc)
            except (TypeError, ValueError):
                raise ValueError("forced fee costs no numericos") from None
            if not (math.isfinite(foc_f) and math.isfinite(fcc_f)):
                raise ValueError("forced fee no finita")
        else:
            try:
                fee_o = float(trade["fee_open"])
                fee_c = float(trade["fee_close"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("forced sin fees") from None
            foc_f = amount * open_rate * fee_o
            fcc_f = amount * close_rate * fee_c
        pnl = amount * close_rate - fcc_f - (amount * open_rate + foc_f)
        if not math.isfinite(pnl):
            raise ValueError("forced pnl no finito")
        total += pnl
    return float(total)


def buyhold_comparable_for_window(first_open, last_open, variant, fee):
    """BH comparable mismo perfil/stop, disponible 9900, fees fuera principal."""
    from market.equity import buyhold_comparable

    try:
        risk = float(variant["risk_pct"])
        exposure = float(variant["exposure"])
        stop = float(variant["stop"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("variante sin risk/exposure/stop") from None
    return buyhold_comparable(
        float(first_open), float(last_open),
        AVAILABLE_BALANCE, risk, exposure, stop, float(fee))


def full_btc_context(first_open, last_open, fee, capital=10000.0):
    """Contexto 100% BTC separado: qty=capital/(1+fee)/first, sin rebalanceo."""
    try:
        first = float(first_open)
        last = float(last_open)
        fee_f = float(fee)
        cap = float(capital)
    except (TypeError, ValueError):
        raise ValueError("fullBTC no numerico") from None
    if not (math.isfinite(first) and math.isfinite(last)
            and math.isfinite(fee_f) and math.isfinite(cap)):
        raise ValueError("fullBTC no finito")
    if first <= 0.0 or last <= 0.0 or cap <= 0.0 or fee_f < 0.0:
        raise ValueError("fullBTC fuera de rango")
    qty = cap / ((1.0 + fee_f) * first)
    end_value = qty * last * (1.0 - fee_f)
    pnl = end_value - cap
    return {
        "first_open": first,
        "last_open": last,
        "fee": fee_f,
        "capital": cap,
        "quantity": float(qty),
        "end_value": float(end_value),
        "pnl": float(pnl),
    }


def aggregate_year_record(variant_id, role, year, fee, episodes, expected_windows=None):
    """Agrega episodios intersectados del ano a un registro selection (12 campos).

    episodes: lista de dicts con days/final_equity/initial/max_drawdown_pct/
      trades (lista nativa)/turnover/bh_final/bh_pnl/valid.
    expected_windows: n esperado de ventanas (si falta alguna => valid False,
      nunca silencio). Cero trades con datos reales: g0 valido.
    """
    from market.equity import time_weighted_g

    if not isinstance(variant_id, str) or not variant_id:
        raise ValueError("variant_id ilegible")
    if role not in ("train", "validation", "test"):
        raise ValueError(f"role ilegible: {role!r}")
    if type(year) is bool or not isinstance(year, int):
        raise ValueError("year debe ser int")
    try:
        fee_f = float(fee)
    except (TypeError, ValueError):
        raise ValueError("fee no numerica") from None
    if not math.isfinite(fee_f) or fee_f < 0.0:
        raise ValueError("fee no valida")
    episodes = list(episodes or [])
    if expected_windows is not None and len(episodes) != int(expected_windows):
        # Faltan ventanas: no valido, no se descarta.
        days_fallback = sum(float(e.get("days", 0.0) or 0.0) for e in episodes) or 1.0
        return {
            "variant_id": str(variant_id), "role": str(role), "year": int(year),
            "fee": float(fee_f), "valid": False, "days": float(days_fallback),
            "g": 0.0, "bh_g": 0.0, "max_drawdown_pct": 1.0,
            "trades_nonforced": 0, "turnover": 0.0,
            "g_without_positive_forced": 0.0,
        }
    if not episodes:
        raise ValueError("sin episodios para agregar (ventana ausente, no silencio)")
    # Cualquier episodio invalido contamina el ano (no se elimina).
    invalid = any(e.get("valid") is not True for e in episodes)
    days = sum(float(e["days"]) for e in episodes)
    if not math.isfinite(days) or days <= 0.0:
        raise ValueError("dias agregados deben ser >0")
    max_dd = max(float(e["max_drawdown_pct"]) for e in episodes)
    trades_nf = sum(int(e["trades_nonforced"]) for e in episodes)
    turnover = sum(float(e["turnover"]) for e in episodes)
    # g ponderada por dias (parent, sin CAGR).
    cand_eps = [{"equity_initial": float(e["initial"]),
                 "equity_final": float(e["final_equity"]),
                 "days": float(e["days"])} for e in episodes]
    bh_eps = [{"equity_initial": float(INITIAL_BALANCE),
               "equity_final": float(e["bh_final"]),
               "days": float(e["days"])} for e in episodes]
    try:
        g = float(time_weighted_g(cand_eps))
        bh_g = float(time_weighted_g(bh_eps))
    except ValueError:
        return {
            "variant_id": str(variant_id), "role": str(role), "year": int(year),
            "fee": float(fee_f), "valid": False, "days": float(days),
            "g": 0.0, "bh_g": 0.0, "max_drawdown_pct": float(max_dd),
            "trades_nonforced": int(trades_nf), "turnover": float(turnover),
            "g_without_positive_forced": 0.0,
        }
    # Stress forzados: retira SOLO contribucion positiva del wealth terminal.
    adj_eps = []
    for e in episodes:
        try:
            forced = float(e.get("forced_positive", 0.0) or 0.0)
        except (TypeError, ValueError):
            forced = 0.0
        positive = forced if forced > 0.0 else 0.0
        adj_final = float(e["final_equity"]) - positive
        if not math.isfinite(adj_final) or adj_final <= 0.0:
            invalid = True
            adj_final = float(e["final_equity"])
        adj_eps.append({"equity_initial": float(e["initial"]),
                        "equity_final": float(adj_final),
                        "days": float(e["days"])})
    try:
        g_forced = float(time_weighted_g(adj_eps))
    except ValueError:
        invalid = True
        g_forced = float(g)
    valid = (not invalid) and all(
        math.isfinite(float(e["final_equity"])) for e in episodes)
    # DD fuera de 0..1 o dias invalidos ya filtrados arriba.
    if not 0.0 <= float(max_dd) <= 1.0:
        valid = False
    return {
        "variant_id": str(variant_id), "role": str(role), "year": int(year),
        "fee": float(fee_f), "valid": bool(valid), "days": float(days),
        "g": float(g), "bh_g": float(bh_g),
        "max_drawdown_pct": float(max_dd),
        "trades_nonforced": int(trades_nf), "turnover": float(turnover),
        "g_without_positive_forced": float(g_forced),
    }


def _num_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_native_batch(zip_path, expected_names):
    """Parsea ZIP batch nativo exigiendo TODOS los nombres esperados exactos.

    Ausente/extra/malformado core => (None, error). Desconocido nunca silencio.
    Core por estrategia: profit_total_abs/profit_total finitos no booleanos,
    total_trades entero >=0 no bool, mas lista trades EXPLICITA (sin fallback
    None->[]) con len(trades)==total_trades. El error de conteo lleva prefijo
    estable ``count:`` para que el runner lo registre como causa propia.
    Retorna (dict nombre->resumen, None) o (None, motivo).
    """
    import json

    expected = sorted(str(n) for n in (expected_names or []))
    if not expected or len(set(expected)) != len(expected):
        return None, "nombres esperados duplicados o vacios"
    zpath = Path(zip_path)
    if not zpath.is_file():
        return None, f"ZIP ausente: {zpath.name}"
    try:
        with zipfile.ZipFile(str(zpath), "r") as bundle:
            names = bundle.namelist()
            json_cands = [n for n in names if n.endswith(".json")
                          and "config" not in n.lower()]
            if not json_cands:
                return None, f"ZIP sin JSON de resultados ({sorted(names)})"
            payload = None
            inner = None
            # El ZIP batch tiene un unico JSON con {"strategy": {...}}.
            for cand in sorted(json_cands):
                try:
                    raw = bundle.read(cand).decode("utf-8")
                    data = json.loads(raw)
                except (KeyError, UnicodeDecodeError, ValueError):
                    continue
                if isinstance(data, dict) and isinstance(data.get("strategy"), dict):
                    payload = data
                    inner = cand
                    break
            if payload is None:
                return None, f"schema sin strategy ({sorted(names)})"
    except zipfile.BadZipFile as exc:
        return None, f"ZIP ilegible: {exc}"
    strat_map = payload.get("strategy") or {}
    if not isinstance(strat_map, dict):
        return None, "strategy no es objeto"
    got = sorted(str(k) for k in strat_map.keys())
    if got != expected:
        missing = sorted(set(expected) - set(got))
        extra = sorted(set(got) - set(expected))
        return None, f"nombres batch inexactos (falta {missing}, sobra {extra})"
    out = {}
    for name in expected:
        entry = strat_map.get(name)
        if not isinstance(entry, dict):
            return None, f"{name}: sin metricas de estrategia"
        for core_key in ("profit_total_abs", "profit_total"):
            if isinstance(entry.get(core_key), bool):
                return None, f"{name}: core booleano en {core_key}"
        profit_abs = _num_or_none(entry.get("profit_total_abs"))
        port_return = _num_or_none(entry.get("profit_total"))
        raw_trades = entry.get("total_trades")
        try:
            if isinstance(raw_trades, bool):
                raise ValueError("bool no es conteo")
            trades_n = int(raw_trades)
            if trades_n < 0 or float(raw_trades) != float(trades_n):
                raise ValueError("conteo no entero >=0")
        except (TypeError, ValueError):
            return None, f"{name}: schema sin nucleo (profit_total_abs/profit_total/total_trades)"
        if profit_abs is None or port_return is None:
            return None, f"{name}: schema sin nucleo (profit_total_abs/profit_total/total_trades)"
        if "trades" not in entry or entry.get("trades") is None:
            return None, f"{name}: lista trades ausente (sin fallback)"
        trades = entry.get("trades")
        if not isinstance(trades, list):
            return None, f"{name}: trades no es lista"
        if len(trades) != trades_n:
            return None, f"count:{name} trades len {len(trades)} != total_trades {trades_n}"
        out[name] = {
            "profit_abs": float(profit_abs),
            "portfolio_return": float(port_return),
            "trades_count": int(trades_n),
            "trades": list(trades),
            "zip": zpath.name,
            "inner": inner,
        }
    return out, None


def check_trades_in_window(trades, start_iso, end_iso):
    """Rechaza trades fuera de la ventana efectiva (sin silencio)."""
    start = _utc(start_iso)
    end = _utc(end_iso)
    for trade in trades or []:
        try:
            o = _utc(trade.get("open_date"))
            c = _utc(trade.get("close_date"))
        except Exception as exc:
            raise ValueError(f"trade fechas ilegibles: {exc}") from exc
        # Intrabar o==c==end solo force_exit de frontera (parent equity).
        if o == c == end:
            if str(trade.get("exit_reason") or "") != "force_exit":
                raise ValueError("apertura en end solo intrabar force_exit")
            continue
        if o == c:
            if not (start <= o < end):
                raise ValueError(f"fill intrabar fuera de ventana: {o.isoformat()}")
        else:
            if not (start <= o < end):
                raise ValueError(f"open fuera de ventana: {o.isoformat()}")
            if not (start < c <= end):
                raise ValueError(f"close fuera de ventana: {c.isoformat()}")


def longest_train_segment(snapshot_manifest):
    """Segmento TRAIN mas largo (desempate por start) para gates de sesgo."""
    metas = list(snapshot_manifest.get("segments_meta") or [])
    best = None
    for meta in metas:
        try:
            length = int(meta.get("length", 0))
        except (TypeError, ValueError):
            continue
        if best is None or length > int(best.get("length", 0)):
            best = meta
        elif length == int(best.get("length", 0)) and str(meta.get("start", "")) < str(best.get("start", "")):
            best = meta
    return best


def bias_reference_checks(variant, ref_trades):
    """Minimo 5 analizables y cobertura ambos cruces con tags exactos familia."""
    family = str(variant.get("family") or "")
    if family not in ENTRY_TAGS:
        return False, f"familia desconocida para sesgo: {family!r}"
    entry_ok = ENTRY_TAGS[family]
    exit_ok = EXIT_TAGS[family]
    allowed_exit = ALLOWED_EXIT_REASONS[family] | exit_ok | {"force_exit"}
    analysable = [t for t in (ref_trades or [])
                  if str((t or {}).get("exit_reason") or "") != "force_exit"]
    if len(analysable) < MIN_BIAS_TRADES:
        return False, f"solo {len(analysable)} analizables (<5)"
    has_enter = False
    has_exit = False
    for trade in analysable:
        enter_tag = str(trade.get("enter_tag") or "")
        exit_tag = str(trade.get("exit_tag") or "")
        exit_reason = str(trade.get("exit_reason") or "")
        if enter_tag not in entry_ok:
            return False, f"enter_tag fuera de familia {family}: {enter_tag!r}"
        if exit_tag and exit_tag not in exit_ok:
            # Tags de salida desconocidos no se silencian (stop usa reason).
            if exit_tag not in exit_ok:
                return False, f"exit_tag fuera de familia {family}: {exit_tag!r}"
        if exit_reason not in allowed_exit:
            return False, f"exit_reason fuera de familia {family}: {exit_reason!r}"
        if enter_tag in entry_ok:
            has_enter = True
        if exit_tag in exit_ok or exit_reason in exit_ok:
            has_exit = True
    if not (has_enter and has_exit):
        return False, "sin cobertura de ambos cruces (stop solo no acredita salida)"
    return True, "cobertura ambos cruces con tags exactos"


def parse_lookahead_csv_generic(csv_path, expected_strategy):
    """CSV lookahead nativo para una estrategia cerrada del registry."""
    import csv

    try:
        text = Path(csv_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"CSV ilegible: {exc}"
    try:
        rows = list(csv.DictReader(text.splitlines()))
    except Exception as exc:
        return None, f"CSV malformado: {exc}"
    if not rows:
        return None, "CSV sin filas (muestra no analizada)"
    target = None
    for row in rows:
        if str(row.get("strategy") or "") == str(expected_strategy):
            target = row
            break
    if target is None:
        return None, f"CSV sin fila de {expected_strategy}"
    expected_cols = {"filename", "strategy", "has_bias", "total_signals",
                     "biased_entry_signals", "biased_exit_signals",
                     "biased_indicators"}
    if not expected_cols.issubset(set(target.keys())):
        return None, f"CSV con schema inesperado ({sorted(target.keys())})"

    def _as_bool(raw):
        text_value = str(raw or "").strip().lower()
        if text_value in ("true", "1"):
            return True
        if text_value in ("false", "0"):
            return False
        return None

    def _as_int(raw):
        import re

        text_value = str(raw or "").strip()
        if not re.fullmatch(r"\d+", text_value):
            return None
        try:
            return int(text_value)
        except (TypeError, ValueError):
            return None

    has_bias = _as_bool(target.get("has_bias"))
    total = _as_int(target.get("total_signals"))
    biased_entry = _as_int(target.get("biased_entry_signals"))
    biased_exit = _as_int(target.get("biased_exit_signals"))
    indicators_raw = str(target.get("biased_indicators") or "").strip()
    indicators = [s.strip() for s in indicators_raw.split(",") if s.strip()]
    if has_bias is None or total is None or biased_entry is None or biased_exit is None:
        return None, "CSV con tipos inesperados"
    return {
        "has_bias": has_bias,
        "total_signals": total,
        "biased_entry_signals": biased_entry,
        "biased_exit_signals": biased_exit,
        "biased_indicators": indicators,
    }, None


def lookahead_coverage_ok(parsed_total, ref_trades):
    """CSV debe cubrir exactamente la referencia analizable (sin force_exit)."""
    if isinstance(parsed_total, bool):
        return False
    if isinstance(parsed_total, int):
        count = parsed_total
    elif isinstance(parsed_total, float):
        if not float(parsed_total).is_integer():
            return False
        count = int(parsed_total)
    else:
        return False
    if count < 0:
        return False
    try:
        expected = sum(1 for trade in (ref_trades or [])
                       if str((trade or {}).get("exit_reason") or "") != "force_exit")
    except (TypeError, ValueError):
        return False
    return count == expected


def own_strategy_gate(variant, frame_1h):
    """Estrategia real vs recortes 201/400/800/1200 en eventos (tol 1e-10).

    Usa solo populate_* de la clase cerrada del registry (sin formula manual).
    Requiere >=1201 velas o INCONCLUSIVE. Compara enter/exit exactos y
    columnas indicadoras numericas con tolerancia relativa.
    """
    import pandas as pd

    from strategies.search.SpotCandidates import make_strategy

    if frame_1h is None or len(frame_1h) < MIN_RECURSIVE_BARS:
        return {"verdict": "INCONCLUSIVE",
                "reason": f"cobertura insuficiente (<{MIN_RECURSIVE_BARS} velas)"}
    try:
        cls = make_strategy(dict(variant))
        cfg = {"stake_currency": "USDT", "dry_run_wallet": 10000.0,
               "candle_type_def": "spot", "runmode": "backtest"}
        strat = cls(config=dict(cfg))
    except Exception as exc:
        return {"verdict": "FAIL", "reason": f"estrategia no instanciable: {exc}"}

    def _run(piece):
        out = strat.populate_indicators(piece.copy(), {"pair": "BTC/USDT"})
        out = strat.populate_entry_trend(out, {"pair": "BTC/USDT"})
        out = strat.populate_exit_trend(out, {"pair": "BTC/USDT"})
        return out

    try:
        ref = _run(frame_1h.sort_values("date").reset_index(drop=True))
    except Exception as exc:
        return {"verdict": "INCONCLUSIVE", "reason": f"estrategia no ejecutable: {exc}"}
    events = []
    seen_enter = False
    seen_exit = False
    for pos in range(len(ref)):
        if pos < 400:
            continue
        try:
            enter = int(ref["enter_long"].iloc[pos]) == 1
            exit_ = int(ref["exit_long"].iloc[pos]) == 1
        except Exception:
            return {"verdict": "FAIL", "reason": f"senales ilegibles en {pos}"}
        if enter or exit_:
            events.append(pos)
            seen_enter = seen_enter or enter
            seen_exit = seen_exit or exit_
    if not (seen_enter and seen_exit):
        return {"verdict": "INCONCLUSIVE",
                "reason": "ambos cruces no cubiertos con contexto 400",
                "checked_events": len(events)}
    frame = frame_1h.sort_values("date").reset_index(drop=True)
    for pos in events:
        for width in RECURSIVE_WINDOWS:
            if pos - width + 1 < 0:
                return {"verdict": "INCONCLUSIVE",
                        "reason": f"evento {pos} sin contexto {width}"}
            piece = frame.iloc[pos - width + 1:pos + 1].copy().reset_index(drop=True)
            try:
                got = _run(piece)
            except Exception as exc:
                return {"verdict": "FAIL",
                        "reason": f"recorte {width} no ejecutable en {pos}: {exc}"}
            # Senales exactas.
            try:
                for col in ("enter_long", "exit_long"):
                    if int(got[col].iloc[-1]) != int(ref[col].iloc[pos]):
                        return {"verdict": "FAIL",
                                "reason": f"senal distinta {col} en {pos} (recorte {width})"}
            except Exception as exc:
                return {"verdict": "FAIL", "reason": f"senal ilegible en {pos}: {exc}"}
            # Indicadoras numericas presentes en ambas (tolerancia 1e-10).
            for col in ("sma_fast", "sma_slow", "sma200", "prev_high",
                        "prev_low", "rev_mean", "rev_lower", "sma20", "sma50"):
                if col not in ref.columns or col not in got.columns:
                    continue
                try:
                    ref_v = float(ref[col].iloc[pos])
                    got_v = float(got[col].iloc[-1])
                except (TypeError, ValueError):
                    continue
                if not (math.isfinite(ref_v) and math.isfinite(got_v)):
                    # Ambos NaN (warmup) se consideran iguales; uno solo es FAIL.
                    import math as _m

                    if _m.isnan(ref_v) and _m.isnan(got_v):
                        continue
                    return {"verdict": "FAIL",
                            "reason": f"indicadora no finita {col} en {pos}"}
                tol = SMA_TOL * max(1.0, abs(ref_v))
                if abs(got_v - ref_v) > tol:
                    return {"verdict": "FAIL",
                            "reason": f"indicadora fuera de tolerancia {col} en {pos}",
                            "expected": ref_v, "got": got_v}
    return {"verdict": "PASS",
            "reason": "senales e indicadoras exactas en eventos con contexto",
            "checked_events": len(events)}
