"""market.equity: ledger analitico long spot + benchmark y metricas.

Contabilidad analitica, no motor: Freqtrade decide fills; aqui se valoran
fills nativos contra velas 5m contiguas.

Contrato:
- Ventana UTC [start, end). Velas 5m con cobertura exacta: primera == start
  y ultima + 5min == end, ordenadas, unicas, sin huecos. Fills en grid 5m y
  con vela presente (< end); fuera de grid o sin cobertura rechaza.
- Cada trade liquida caja a precio fill nativo; MTM con close 5m.
  open == close intrabar es legitimo (stop nativo misma vela).
  Cierre == end agrega fila EVENTO terminal (sin quote futura;
  close = fill) con caja/fees/qty/DD; summary final == ultima fila.
- Mismo timestamp: closes estrictos antes que opens (independiente de la
  lista); intrabar open antes que su close. Max 1 posicion abierta;
  solape rechaza. Cash nunca negativo (sin margen).
- Solo long spot leverage 1: side long, is_short falsy, tasas/costes >= 0
  en USDT. DD abs y pct son maximos independientes (el pct manda para el
  gate aunque el abs maximo llegue despues); peak = high-water del max abs
  (incluye inicial; sin DD es el maximo global).
- Episodios aislados, capital independiente. g(E) pondera por dias;
  mediana de exceso exige exactamente los mismos anos.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

_TOL_CASH = 1e-9
_TOL_QTY = 1e-9


def _parse_ts(value, label: str):
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{label} ilegible: {value!r}") from None
    else:
        raise ValueError(f"{label} debe ser datetime o ISO, no {type(value).__name__}")
    if ts.tzinfo is None:
        raise ValueError(f"{label} sin zona UTC")
    return ts.astimezone(timezone.utc)


def _check_window(start, end):
    start = _parse_ts(start, "window_start")
    end = _parse_ts(end, "window_end")
    if not start < end:
        raise ValueError("ventana vacia o invertida")
    return start, end


def _is_5m_aligned(dt) -> bool:
    return dt.second == 0 and dt.microsecond == 0 and dt.minute % 5 == 0


def _trade_amounts(trade: dict):
    if not isinstance(trade, dict):
        raise ValueError("trade debe ser objeto")
    for key in ("amount", "open_rate", "close_rate"):
        val = trade.get(key)
        try:
            num = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{key} no numerico: {val!r}") from None
        if not math.isfinite(num) or num <= 0.0:
            raise ValueError(f"{key} debe ser finito > 0: {val!r}")
    amount = float(trade["amount"])
    open_rate = float(trade["open_rate"])
    close_rate = float(trade["close_rate"])

    if trade.get("is_short"):
        raise ValueError("solo long spot (is_short)")
    side = trade.get("side", "long")
    if side != "long":
        raise ValueError(f"solo long spot, no {side!r}")
    lev = trade.get("leverage", 1.0)
    try:
        lev_f = float(lev)
    except (TypeError, ValueError):
        raise ValueError(f"leverage ilegible: {lev!r}") from None
    if not math.isfinite(lev_f) or lev_f != 1.0:
        raise ValueError(f"solo leverage 1 spot, no {lev!r}")

    foc = trade.get("fee_open_cost")
    fcc = trade.get("fee_close_cost")
    if foc is not None or fcc is not None:
        if foc is None or fcc is None:
            raise ValueError("costes de fee incompletos (ambos o ninguno)")
        try:
            foc_f = float(foc)
            fcc_f = float(fcc)
        except (TypeError, ValueError):
            raise ValueError("fee_*_cost no numericos") from None
        if not math.isfinite(foc_f) or not math.isfinite(fcc_f) or foc_f < 0.0 or fcc_f < 0.0:
            raise ValueError("fee_*_cost deben ser finitos >= 0")
        for key in ("fee_open_currency", "fee_close_currency", "fee_currency"):
            cur = trade.get(key)
            if cur not in (None, "", "USDT"):
                raise ValueError(f"fee en otra moneda: {key}={cur!r}")
        fee_open_cost, fee_close_cost = foc_f, fcc_f
    else:
        for key in ("fee_open", "fee_close"):
            if key not in trade:
                raise ValueError(f"falta {key} (tasa o *_cost exigidos, sin invento)")
            try:
                rate = float(trade[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} no numerico") from None
            if not math.isfinite(rate) or rate < 0.0:
                raise ValueError(f"{key} debe ser finito >= 0")
        fee_open_cost = amount * open_rate * float(trade["fee_open"])
        fee_close_cost = amount * close_rate * float(trade["fee_close"])
        for key in ("fee_open_currency", "fee_close_currency", "fee_currency"):
            cur = trade.get(key)
            if cur not in (None, "", "USDT"):
                raise ValueError(f"fee en otra moneda: {key}={cur!r}")

    open_dt = _parse_ts(trade.get("open_date"), "open_date")
    close_dt = _parse_ts(trade.get("close_date"), "close_date")
    if close_dt < open_dt:
        raise ValueError("close_date anterior a open_date")
    if not _is_5m_aligned(open_dt):
        raise ValueError(f"open fuera de grid 5m: {open_dt.isoformat()}")
    if not _is_5m_aligned(close_dt):
        raise ValueError(f"close fuera de grid 5m: {close_dt.isoformat()}")
    return {
        "amount": amount,
        "open_rate": open_rate,
        "close_rate": close_rate,
        "fee_open_cost": fee_open_cost,
        "fee_close_cost": fee_close_cost,
        "open_date": open_dt,
        "close_date": close_dt,
        "exit_reason": trade.get("exit_reason"),
    }


def build_ledger(prices_5m, trades, initial_balance, window_start, window_end):
    """Construye curva MTM 5m (+ terminal) y resumen; ValueError si invalido."""
    import numpy as np
    import pandas as pd

    try:
        initial = float(initial_balance)
    except (TypeError, ValueError):
        raise ValueError("initial_balance no numerico") from None
    if not math.isfinite(initial) or initial <= 0.0:
        raise ValueError("initial_balance debe ser finito > 0")
    start, end = _check_window(window_start, window_end)

    if trades is None:
        raise ValueError("trades debe ser lista")
    if not isinstance(trades, (list, tuple)):
        raise ValueError("trades debe ser lista")
    raws = list(trades)
    parsed = [_trade_amounts(t) for t in raws]
    for p, raw in zip(parsed, raws):
        o, c = p["open_date"], p["close_date"]
        if o == c == end:
            if raw.get("exit_reason") != "force_exit":
                raise ValueError("apertura en end solo intrabar force_exit de frontera")
        elif o == c:
            if not (start <= o < end):
                raise ValueError(f"fill intrabar fuera de ventana: {o.isoformat()}")
        else:
            if not (start <= o < end):
                raise ValueError(f"open fuera de ventana: {o.isoformat()}")
            if not (start < c <= end):
                raise ValueError(f"close fuera de ventana: {c.isoformat()}")

    from market.train import validate_ohlcv

    validate_ohlcv(prices_5m, start, end, 5)
    stamps = pd.to_datetime(prices_5m["date"], utc=True)
    if stamps.iloc[0] != pd.Timestamp(start):
        raise ValueError("cobertura: primera vela != start (sin invento)")
    if stamps.iloc[-1] + pd.Timedelta(minutes=5) != pd.Timestamp(end):
        raise ValueError("cobertura: ultima vela + 5m != end (sin invento)")
    diffs = stamps.diff().dt.total_seconds().iloc[1:]
    if len(diffs) and bool((diffs != 300.0).any()):
        raise ValueError("hueco 5m: sin interpolacion ni rebasing")
    closes = pd.to_numeric(prices_5m["close"], errors="coerce").to_numpy(dtype="float64")
    n = len(stamps)
    if n == 0:
        raise ValueError("sin velas en ventana")

    pos_of = {int(ts.value): i for i, ts in enumerate(stamps)}

    # Orden canonico por timestamp: closes estrictos (0) antes que opens (1);
    # intrabar open (1) antes que su close (2). Independiente de la lista.
    events = []
    for seq, p in enumerate(parsed):
        o, c = p["open_date"], p["close_date"]
        if o == c:
            events.append((o, 1, seq, "open", p))
            events.append((c, 2, seq, "close", p))
        else:
            events.append((o, 1, seq, "open", p))
            events.append((c, 0, seq, "close", p))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    # Valida en orden (cash, max1) y agrega deltas por posicion de vela.
    cash_delta = np.zeros(n, dtype="float64")
    qty_delta = np.zeros(n, dtype="float64")
    fee_delta = np.zeros(n, dtype="float64")
    cash = initial
    qty = 0.0
    fees = 0.0
    open_count = 0
    has_terminal = False
    term_close_rate = None
    for ts, _, _, kind, p in events:
        if ts == end:
            has_terminal = True
            if kind == "open":
                if open_count >= 1:
                    raise ValueError("solape: max 1 posicion abierta")
                cost = p["amount"] * p["open_rate"] + p["fee_open_cost"]
                if cost > cash + _TOL_CASH:
                    raise ValueError("caja negativa: coste supera disponible (sin margen)")
                cash -= cost
                qty += p["amount"]
                fees += p["fee_open_cost"]
                open_count += 1
            else:
                if open_count < 1:
                    raise ValueError("cierre en frontera sin apertura")
                if qty + _TOL_QTY < p["amount"]:
                    raise ValueError("cierre en frontera supera cantidad abierta")
                proceeds = p["amount"] * p["close_rate"] - p["fee_close_cost"]
                cash += proceeds
                qty -= p["amount"]
                fees += p["fee_close_cost"]
                open_count -= 1
                if abs(qty) < _TOL_QTY:
                    qty = 0.0
                term_close_rate = p["close_rate"]
            continue
        pos = pos_of.get(int(pd.Timestamp(ts).value))
        if pos is None:
            raise ValueError(f"fill sin vela en grid: {ts.isoformat()} (sin invento)")
        if kind == "open":
            if open_count >= 1:
                raise ValueError("solape: max 1 posicion abierta")
            cost = p["amount"] * p["open_rate"] + p["fee_open_cost"]
            if cost > cash + _TOL_CASH:
                raise ValueError("caja negativa: coste supera disponible (sin margen)")
            cash -= cost
            qty += p["amount"]
            fees += p["fee_open_cost"]
            open_count += 1
            cash_delta[pos] -= cost
            qty_delta[pos] += p["amount"]
            fee_delta[pos] += p["fee_open_cost"]
        else:
            if open_count < 1:
                raise ValueError("cierre sin apertura (solape u orden)")
            if qty + _TOL_QTY < p["amount"]:
                raise ValueError("cierre supera cantidad abierta")
            proceeds = p["amount"] * p["close_rate"] - p["fee_close_cost"]
            cash += proceeds
            qty -= p["amount"]
            fees += p["fee_close_cost"]
            open_count -= 1
            if abs(qty) < _TOL_QTY:
                qty = 0.0
            cash_delta[pos] += proceeds
            qty_delta[pos] -= p["amount"]
            fee_delta[pos] += p["fee_close_cost"]

    cash_s = initial + np.cumsum(cash_delta)
    qty_s = np.cumsum(qty_delta)
    fee_s = np.cumsum(fee_delta)
    eq = cash_s + qty_s * closes

    dates = list(stamps)
    close_l = [float(v) for v in closes]
    cash_l = [float(v) for v in cash_s]
    qty_l = [float(v) for v in qty_s]
    fee_l = [float(v) for v in fee_s]
    eq_l = [float(v) for v in eq]
    if has_terminal:
        if abs(qty) < _TOL_QTY:
            qty = 0.0
        final_cash = float(cash)
        final_qty = float(qty)
        final_fees = float(fees)
        final_equity = final_cash if final_qty == 0.0 else final_cash + final_qty * float(closes[-1])
        dates.append(pd.Timestamp(end))
        close_l.append(float(term_close_rate) if term_close_rate is not None else float(closes[-1]))
        cash_l.append(final_cash)
        qty_l.append(final_qty)
        fee_l.append(final_fees)
        eq_l.append(float(final_equity))
    else:
        if abs(qty) < _TOL_QTY:
            qty = 0.0
        final_cash = float(cash_s[-1])
        final_qty = float(qty_s[-1])
        final_fees = float(fee_s[-1])
        final_equity = float(eq[-1])

    eq_arr = np.array(eq_l, dtype="float64")
    run = np.maximum.accumulate(np.concatenate(([initial], eq_arr)))[1:]
    dd_u = run - eq_arr
    dd_p = np.divide(dd_u, run, out=np.zeros_like(dd_u), where=run > 0)
    i_abs = int(np.argmax(dd_u))
    max_abs = float(dd_u[i_abs])
    peak_abs = float(run[i_abs])
    max_pct = float(np.max(dd_p))
    peak = peak_abs if max_abs > 1e-12 else float(max(initial, float(np.max(eq_arr))))

    curve = pd.DataFrame({
        "date": dates,
        "close": close_l,
        "cash": cash_l,
        "quantity": qty_l,
        "equity": eq_l,
        "exposure": [float((q * c / e)) if e > 0 else 0.0 for q, c, e in zip(qty_l, close_l, eq_l)],
        "fees_cum": fee_l,
        "drawdown_usdt": [float(v) for v in dd_u],
        "drawdown_pct": [float(v) for v in dd_p],
    })

    days = (end - start).total_seconds() / 86400.0
    summary = {
        "initial_balance": initial,
        "final_cash": float(final_cash),
        "final_quantity": float(final_qty),
        "final_equity": float(final_equity),
        "fees": float(final_fees),
        "fees_currency": "USDT",
        "peak": float(peak),
        "max_drawdown_usdt": float(max_abs),
        "max_drawdown_pct": float(max_pct),
        "n_trades": int(len(parsed)),
        "days_observed": float(days),
        "window_start": start,
        "window_end": end,
    }
    return curve, summary


def buyhold_comparable(first_open, last_open, available, risk, exposure, stop, fee):
    """Benchmark comparable: mismo perfil/stop, teorico sin redondeo de lote."""
    for label, val in (("first_open", first_open), ("last_open", last_open),
                       ("available", available), ("risk", risk),
                       ("exposure", exposure), ("stop", stop), ("fee", fee)):
        try:
            num = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{label} no numerico: {val!r}") from None
        if not math.isfinite(num):
            raise ValueError(f"{label} no finito")
    first_open = float(first_open)
    last_open = float(last_open)
    available = float(available)
    risk = float(risk)
    exposure = float(exposure)
    stop = float(stop)
    fee = float(fee)
    if first_open <= 0.0 or last_open <= 0.0:
        raise ValueError("opens deben ser > 0")
    if available <= 0.0:
        raise ValueError("available debe ser > 0")
    if not risk > 0.0:
        raise ValueError("risk debe ser > 0")
    if not 0.0 < exposure <= 1.0:
        raise ValueError("exposure debe estar en (0,1]")
    if not stop > 0.0:
        raise ValueError("stop debe ser > 0")
    if not fee >= 0.0:
        raise ValueError("fee debe ser >= 0")
    denom = stop + 0.006
    if denom <= 0.0:
        raise ValueError("stop+reserva debe ser > 0")
    notional = min(available * exposure, available * risk / denom)
    if not math.isfinite(notional) or notional <= 0.0:
        raise ValueError("notional invalido")
    quantity = notional / first_open
    fee_cost = notional * fee + quantity * last_open * fee
    end_value = quantity * last_open * (1.0 - fee)
    cost = notional * (1.0 + fee)
    pnl = end_value - cost
    return {
        "first_open": first_open,
        "last_open": last_open,
        "available": available,
        "notional": float(notional),
        "quantity": float(quantity),
        "exposure": float(exposure),
        "fee": float(fee),
        "fees": float(fee_cost),
        "fees_currency": "USDT",
        "end_value": float(end_value),
        "cost": float(cost),
        "pnl": float(pnl),
    }


def time_weighted_g(episodes):
    """g(E)=sum(log(final/inicial))/sum(dias); pondera por tiempo, no episodios."""
    if not isinstance(episodes, (list, tuple)) or not episodes:
        raise ValueError("episodes debe ser lista no vacia")
    num = 0.0
    den = 0.0
    for ep in episodes:
        if not isinstance(ep, dict):
            raise ValueError("episodio debe ser objeto")
        try:
            ini = float(ep["equity_initial"])
            fin = float(ep["equity_final"])
            days = float(ep["days"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("episodio exige equity_initial/equity_final/days numericos") from None
        if not math.isfinite(ini) or not math.isfinite(fin) or not math.isfinite(days):
            raise ValueError("episodio no finito")
        if ini <= 0.0 or fin <= 0.0:
            raise ValueError("equities deben ser > 0 para log")
        if days <= 0.0:
            raise ValueError("days debe ser > 0")
        num += math.log(fin / ini)
        den += days
    if den <= 0.0:
        raise ValueError("dias observados deben ser > 0")
    return num / den


def median_excess_by_year(candidate_by_year, benchmark_by_year):
    """Mediana de d_y=g_y(cand)-g_y(BH); exige exactamente los mismos anos."""
    if not isinstance(candidate_by_year, dict) or not isinstance(benchmark_by_year, dict):
        raise ValueError("entradas deben ser dicts ano->g_y")
    if not candidate_by_year or not benchmark_by_year:
        raise ValueError("entradas vacias")
    if set(candidate_by_year) != set(benchmark_by_year):
        raise ValueError("anos distintos: sin interseccion silenciosa")
    excess = []
    for year in sorted(candidate_by_year):
        try:
            c = float(candidate_by_year[year])
            b = float(benchmark_by_year[year])
        except (TypeError, ValueError):
            raise ValueError(f"g_y no numerico en {year!r}") from None
        if not math.isfinite(c) or not math.isfinite(b):
            raise ValueError(f"g_y no finito en {year!r}")
        excess.append(c - b)
    ordered = sorted(excess)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def reconcile_final_ledger(summary, native_profit_abs, initial_balance, tol=0.01):
    """Compara PnL del ledger contra profit nativo; no relanza backtest.

    Retorna {"diff","ok","tol",...}. `ok` exige diff<=tol (default 0.01 USDT).
    """
    if not isinstance(summary, dict):
        raise ValueError("summary debe ser objeto")
    try:
        final_equity = float(summary["final_equity"])
        tol_f = float(tol)
        native = float(native_profit_abs)
        initial = float(initial_balance)
    except (KeyError, TypeError, ValueError):
        raise ValueError("summary/nativo/inicial invalidos para reconciliar") from None
    for val, label in ((final_equity, "final_equity"), (native, "native"),
                       (initial, "initial"), (tol_f, "tol")):
        if not math.isfinite(val):
            raise ValueError(f"{label} no finito")
    if tol_f < 0.0:
        raise ValueError("tol debe ser >= 0")
    pnl = final_equity - initial
    diff = abs(pnl - native)
    return {
        "ledger_pnl": float(pnl),
        "native_profit_abs": float(native),
        "initial_balance": float(initial),
        "diff": float(diff),
        "tol": float(tol_f),
        "tol_currency": "USDT",
        "ok": bool(diff <= tol_f + 1e-12),
    }
