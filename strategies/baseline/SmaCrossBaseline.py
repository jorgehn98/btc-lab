"""SmaCrossBaseline: cruce SMA20/50 spot 1h, solo largo (experimental v1).

Reglas preregistradas: velas cerradas 1h UTC, SMA20/50 con rolling de pandas,
ventana válida de 51 velas consecutivas finitas con volumen positivo para
entradas, cruce alcista entra, cruce bajista sale, stop -2%, ROI desactivado,
una posición long, sin DCA/trailing. Órdenes market simuladas.

Riesgo: cap = min(1000, 10% capital, 0.25% capital / 0.026, max motor).
`custom_stake_amount` devuelve 0.0 ante cualquier error/dato inválido sin
lanzar (evita fallback del motor a proposed_stake). `confirm_trade_entry`
revalida notional <= cap y solo long, False ante cualquier duda.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.strategy import IStrategy

logger = logging.getLogger(__name__)

VALID_WINDOW = 51
SMA_FAST = 20
SMA_SLOW = 50
STOPLOSS = -0.02
CAP_ABSOLUTE = 1000.0
CAP_PCT = 0.10
RISK_PCT = 0.0025
RISK_DIVISOR = 0.026


def _finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _resolve_wallet(strategy) -> float | None:
    wallets = getattr(strategy, "wallets", None)
    getter = getattr(wallets, "get_available_stake_amount", None)
    if wallets is None or not callable(getter):
        logger.warning("saldo no disponible: wallets sin get_available_stake_amount")
        return None
    try:
        wallet = float(getter())
    except Exception as exc:
        logger.warning("saldo inaccesible: %s: %s", type(exc).__name__, exc)
        return None
    if not math.isfinite(wallet) or wallet <= 0.0:
        logger.warning("saldo inválido: %r", wallet)
        return None
    return wallet


def _base_cap(wallet: float) -> float:
    return min(CAP_ABSOLUTE, CAP_PCT * wallet, (RISK_PCT * wallet) / RISK_DIVISOR)


def _time_valid_51(dates) -> pd.Series:
    stamps = pd.to_datetime(dates, utc=True, errors="coerce")
    index = dates.index if hasattr(dates, "index") else None
    steps = stamps.diff().dt.total_seconds() == 3600.0
    steps = steps.fillna(False)
    valid = steps.rolling(VALID_WINDOW - 1, min_periods=VALID_WINDOW - 1).sum() == (VALID_WINDOW - 1)
    valid = valid.fillna(False)
    if index is not None:
        valid.index = index
    return valid


def _window_ok(values, window: int = VALID_WINDOW) -> pd.Series:
    arr = pd.to_numeric(values, errors="coerce").to_numpy(dtype="float64")
    ok = pd.Series(np.isfinite(arr) & (arr > 0.0), index=values.index)
    return ok.rolling(window, min_periods=window).sum() == window


def _sma_pair_ok(fast, slow) -> pd.Series:
    fast_arr = pd.to_numeric(fast, errors="coerce").to_numpy(dtype="float64")
    slow_arr = pd.to_numeric(slow, errors="coerce").to_numpy(dtype="float64")
    now = pd.Series(np.isfinite(fast_arr) & np.isfinite(slow_arr), index=fast.index)
    return now & now.shift(1, fill_value=False)


class SmaCrossBaseline(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {}
    stoploss = STOPLOSS
    can_short = False
    position_adjustment_enable = False
    startup_candle_count = VALID_WINDOW
    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma20"] = dataframe["close"].rolling(window=SMA_FAST, min_periods=SMA_FAST).mean()
        dataframe["sma50"] = dataframe["close"].rolling(window=SMA_SLOW, min_periods=SMA_SLOW).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""
        if len(dataframe) < VALID_WINDOW:
            return dataframe
        if "close" not in dataframe.columns or "volume" not in dataframe.columns:
            return dataframe
        if "date" not in dataframe.columns:
            return dataframe
        fast = dataframe["sma20"] if "sma20" in dataframe.columns else dataframe["close"].rolling(window=SMA_FAST, min_periods=SMA_FAST).mean()
        slow = dataframe["sma50"] if "sma50" in dataframe.columns else dataframe["close"].rolling(window=SMA_SLOW, min_periods=SMA_SLOW).mean()
        window_ok = _window_ok(dataframe["close"]) & _window_ok(dataframe["volume"])
        ready = window_ok & _time_valid_51(dataframe["date"]) & _sma_pair_ok(fast, slow)
        bull = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        signal = (bull.fillna(False) & ready.fillna(False)).to_numpy()
        dataframe.loc[signal, "enter_long"] = 1
        dataframe.loc[signal, "enter_tag"] = "sma_bull"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""
        if len(dataframe) < VALID_WINDOW:
            return dataframe
        if "date" not in dataframe.columns:
            return dataframe
        fast = dataframe["sma20"] if "sma20" in dataframe.columns else dataframe["close"].rolling(window=SMA_FAST, min_periods=SMA_FAST).mean()
        slow = dataframe["sma50"] if "sma50" in dataframe.columns else dataframe["close"].rolling(window=SMA_SLOW, min_periods=SMA_SLOW).mean()
        ready = _time_valid_51(dataframe["date"]) & _sma_pair_ok(fast, slow)
        bear = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        signal = (bear.fillna(False) & ready.fillna(False)).to_numpy()
        dataframe.loc[signal, "exit_long"] = 1
        dataframe.loc[signal, "exit_tag"] = "sma_bear"
        return dataframe

    def custom_stake_amount(
        self,
        pair: str,
        current_time,
        current_rate: float,
        proposed_stake: float,
        min_stake,
        max_stake: float,
        leverage: float,
        entry_tag,
        side: str,
        **kwargs,
    ) -> float:
        if pair != "BTC/USDT":
            return 0.0
        if side != "long":
            return 0.0
        rate = _finite_number(current_rate)
        if rate is None or rate <= 0.0:
            return 0.0
        proposed = _finite_number(proposed_stake)
        if proposed is None or proposed <= 0.0:
            return 0.0
        max_allowed = _finite_number(max_stake)
        if max_allowed is None or max_allowed <= 0.0:
            return 0.0
        lev = _finite_number(leverage)
        if lev is None or lev != 1.0:
            return 0.0
        minimum = None
        if min_stake is not None:
            minimum = _finite_number(min_stake)
            if minimum is None or minimum < 0.0:
                return 0.0
        wallet = _resolve_wallet(self)
        if wallet is None:
            return 0.0
        cap = min(_base_cap(wallet), max_allowed)
        if not math.isfinite(cap) or cap <= 0.0:
            return 0.0
        if minimum is not None and minimum > cap:
            return 0.0
        return float(cap)

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time,
        entry_tag,
        side: str,
        **kwargs,
    ) -> bool:
        if pair != "BTC/USDT":
            return False
        if side != "long":
            return False
        qty = _finite_number(amount)
        price = _finite_number(rate)
        if qty is None or price is None or qty <= 0.0 or price <= 0.0:
            return False
        notional = qty * price
        if not math.isfinite(notional) or notional <= 0.0:
            return False
        wallet = _resolve_wallet(self)
        if wallet is None:
            return False
        cap = _base_cap(wallet)
        if not math.isfinite(cap) or cap <= 0.0:
            return False
        return bool(notional <= cap)
