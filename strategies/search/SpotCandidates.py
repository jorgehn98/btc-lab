"""strategies.search.SpotCandidates: 3 familias spot 1h solo long (T06).

Clases cerradas via make_strategy(variant): valida la variante completa
contra el registro antes de datos, sin nombres libres ni texto arbitrario.
Senales vectorizadas sobre velas cerradas 1h, warmup comun 201 contiguo
finito con close y volume positivos para entradas; las salidas no usan
esos filtros para no bloquear stops. Sizing sin techo absoluto:
cap = min(disponible*exposure, disponible*risk/(stop+0.006), max motor).
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.strategy import IStrategy

from research.campaign import generate_variants as _registered_variants

logger = logging.getLogger(__name__)

WARMUP = 201
SMA_LONG = 200
RESERVE = 0.006
PAIR = "BTC/USDT"

_EXPECTED_KEYS = frozenset({
    "id", "family", "seed_index", "params", "stop",
    "risk_profile", "risk_pct", "exposure", "class_name",
})

_REGISTRY_BY_ID = {v["id"]: v for v in _registered_variants()}


def _finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _resolve_wallet(strategy):
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
        logger.warning("saldo invalido: %r", wallet)
        return None
    return wallet


def _base_cap(wallet, risk_pct, exposure, stop):
    return min(wallet * exposure, wallet * risk_pct / (abs(stop) + RESERVE))


def _window_ok(values, window=WARMUP):
    arr = pd.to_numeric(values, errors="coerce").to_numpy(dtype="float64")
    ok = pd.Series(np.isfinite(arr) & (arr > 0.0), index=values.index)
    return ok.rolling(window, min_periods=window).sum() == window


def _time_valid(dates, window=WARMUP):
    stamps = pd.to_datetime(dates, utc=True, errors="coerce")
    index = dates.index if hasattr(dates, "index") else None
    steps = stamps.diff().dt.total_seconds() == 3600.0
    steps = steps.fillna(False)
    valid = steps.rolling(window - 1, min_periods=window - 1).sum() == (window - 1)
    valid = valid.fillna(False)
    if index is not None:
        valid.index = index
    return valid


def _pair_ok(first, second):
    a = pd.to_numeric(first, errors="coerce").to_numpy(dtype="float64")
    b = pd.to_numeric(second, errors="coerce").to_numpy(dtype="float64")
    now = pd.Series(np.isfinite(a) & np.isfinite(b), index=first.index)
    return now & now.shift(1, fill_value=False)


def _entry_ready(dataframe):
    if len(dataframe) < WARMUP:
        return pd.Series(False, index=dataframe.index)
    if "close" not in dataframe.columns or "volume" not in dataframe.columns:
        return pd.Series(False, index=dataframe.index)
    if "date" not in dataframe.columns:
        return pd.Series(False, index=dataframe.index)
    guards = _window_ok(dataframe["close"]) & _window_ok(dataframe["volume"])
    return guards & _time_valid(dataframe["date"])


def _exit_ready(dataframe):
    if len(dataframe) < WARMUP:
        return pd.Series(False, index=dataframe.index)
    if "date" not in dataframe.columns:
        return pd.Series(False, index=dataframe.index)
    return _time_valid(dataframe["date"])


class _SpotSearchBase(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {}
    stoploss = -0.02
    can_short = False
    position_adjustment_enable = False
    startup_candle_count = WARMUP
    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    _STOP = None
    _RISK_PCT = None
    _EXPOSURE = None

    def _sizing(self):
        stop = _finite_number(getattr(self, "_STOP", None))
        risk = _finite_number(getattr(self, "_RISK_PCT", None))
        exposure = _finite_number(getattr(self, "_EXPOSURE", None))
        if stop is None or stop <= 0.0:
            return None
        if risk is None or risk <= 0.0:
            return None
        if exposure is None or not 0.0 < exposure <= 1.0:
            return None
        return (stop, risk, exposure)

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
        if pair != PAIR:
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
        sizing = self._sizing()
        if sizing is None:
            return 0.0
        stop, risk, exposure = sizing
        cap = min(_base_cap(wallet, risk, exposure, stop), max_allowed)
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
        if pair != PAIR:
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
        sizing = self._sizing()
        if sizing is None:
            return False
        stop, risk, exposure = sizing
        cap = _base_cap(wallet, risk, exposure, stop)
        if not math.isfinite(cap) or cap <= 0.0:
            return False
        return bool(notional <= cap)


class TrendSpotBase(_SpotSearchBase):
    _FAST = None
    _SLOW = None

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        fast_n = getattr(self, "_FAST", None)
        slow_n = getattr(self, "_SLOW", None)
        try:
            fast_n = int(fast_n)
            slow_n = int(slow_n)
        except (TypeError, ValueError):
            fast_n, slow_n = None, None
        if fast_n is None or slow_n is None or fast_n <= 0 or slow_n <= 0:
            dataframe["sma_fast"] = np.nan
            dataframe["sma_slow"] = np.nan
            dataframe["sma200"] = np.nan
            return dataframe
        dataframe["sma_fast"] = dataframe["close"].rolling(window=fast_n, min_periods=fast_n).mean()
        dataframe["sma_slow"] = dataframe["close"].rolling(window=slow_n, min_periods=slow_n).mean()
        dataframe["sma200"] = dataframe["close"].rolling(window=SMA_LONG, min_periods=SMA_LONG).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""
        if len(dataframe) < WARMUP:
            return dataframe
        for col in ("sma_fast", "sma_slow", "sma200", "close"):
            if col not in dataframe.columns:
                return dataframe
        fast = dataframe["sma_fast"]
        slow = dataframe["sma_slow"]
        filt = dataframe["close"] > dataframe["sma200"]
        ready = _entry_ready(dataframe) & _pair_ok(fast, slow)
        filt = filt.fillna(False) & dataframe["sma200"].notna()
        bull = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        signal = (bull.fillna(False) & filt.fillna(False) & ready.fillna(False)).to_numpy()
        dataframe.loc[signal, "enter_long"] = 1
        dataframe.loc[signal, "enter_tag"] = "trend_bull"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""
        if len(dataframe) < WARMUP:
            return dataframe
        for col in ("sma_fast", "sma_slow", "sma200", "close"):
            if col not in dataframe.columns:
                return dataframe
        fast = dataframe["sma_fast"]
        slow = dataframe["sma_slow"]
        ready = _exit_ready(dataframe) & _pair_ok(fast, slow)
        bear = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        lost = (dataframe["close"] < dataframe["sma200"]).fillna(False) & dataframe["sma200"].notna()
        signal = ((bear.fillna(False) | lost) & ready.fillna(False)).to_numpy()
        dataframe.loc[signal, "exit_long"] = 1
        dataframe.loc[signal, "exit_tag"] = "trend_exit"
        return dataframe


class BreakoutSpotBase(_SpotSearchBase):
    _N = None
    _M = None

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        try:
            n = int(getattr(self, "_N", None))
            m = int(getattr(self, "_M", None))
        except (TypeError, ValueError):
            n, m = None, None
        if n is None or m is None or n <= 0 or m <= 0:
            dataframe["prev_high"] = np.nan
            dataframe["prev_low"] = np.nan
            return dataframe
        dataframe["prev_high"] = dataframe["high"].rolling(window=n, min_periods=n).max().shift(1)
        dataframe["prev_low"] = dataframe["low"].rolling(window=m, min_periods=m).min().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""
        if len(dataframe) < WARMUP:
            return dataframe
        for col in ("prev_high", "close"):
            if col not in dataframe.columns:
                return dataframe
        ready = _entry_ready(dataframe) & dataframe["prev_high"].notna()
        cross = dataframe["close"] > dataframe["prev_high"]
        signal = (cross.fillna(False) & ready.fillna(False)).to_numpy()
        dataframe.loc[signal, "enter_long"] = 1
        dataframe.loc[signal, "enter_tag"] = "breakout_high"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""
        if len(dataframe) < WARMUP:
            return dataframe
        for col in ("prev_low", "close"):
            if col not in dataframe.columns:
                return dataframe
        ready = _exit_ready(dataframe) & dataframe["prev_low"].notna()
        under = dataframe["close"] < dataframe["prev_low"]
        signal = (under.fillna(False) & ready.fillna(False)).to_numpy()
        dataframe.loc[signal, "exit_long"] = 1
        dataframe.loc[signal, "exit_tag"] = "breakout_exit"
        return dataframe


class ReversionSpotBase(_SpotSearchBase):
    _WINDOW = None
    _K = None

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        try:
            window = int(getattr(self, "_WINDOW", None))
            mult = float(getattr(self, "_K", None))
        except (TypeError, ValueError):
            window, mult = None, None
        if window is None or mult is None or window <= 0 or mult <= 0:
            dataframe["rev_mean"] = np.nan
            dataframe["rev_lower"] = np.nan
            dataframe["sma200"] = np.nan
            return dataframe
        mean = dataframe["close"].rolling(window=window, min_periods=window).mean()
        std = dataframe["close"].rolling(window=window, min_periods=window).std(ddof=0)
        dataframe["rev_mean"] = mean
        dataframe["rev_lower"] = mean - mult * std
        dataframe["sma200"] = dataframe["close"].rolling(window=SMA_LONG, min_periods=SMA_LONG).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""
        if len(dataframe) < WARMUP:
            return dataframe
        for col in ("rev_mean", "rev_lower", "sma200", "close"):
            if col not in dataframe.columns:
                return dataframe
        above = (dataframe["close"] > dataframe["sma200"]).fillna(False) & dataframe["sma200"].notna()
        below = dataframe["close"] < dataframe["rev_lower"]
        prev_below = dataframe["close"].shift(1) < dataframe["rev_lower"].shift(1)
        cross = below.fillna(False) & (~prev_below.fillna(False))
        band_ok = dataframe["rev_lower"].notna() & dataframe["rev_lower"].shift(1).notna()
        ready = _entry_ready(dataframe) & band_ok
        signal = (cross & above & ready.fillna(False)).to_numpy()
        dataframe.loc[signal, "enter_long"] = 1
        dataframe.loc[signal, "enter_tag"] = "reversion_band"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""
        if len(dataframe) < WARMUP:
            return dataframe
        for col in ("rev_mean", "sma200", "close"):
            if col not in dataframe.columns:
                return dataframe
        ready = _exit_ready(dataframe) & dataframe["rev_mean"].notna()
        back = (dataframe["close"] >= dataframe["rev_mean"]).fillna(False)
        lost = (dataframe["close"] < dataframe["sma200"]).fillna(False) & dataframe["sma200"].notna()
        signal = ((back | lost) & ready.fillna(False)).to_numpy()
        dataframe.loc[signal, "exit_long"] = 1
        dataframe.loc[signal, "exit_tag"] = "reversion_exit"
        return dataframe


_FAMILY_BASES = {
    "trend": TrendSpotBase,
    "breakout": BreakoutSpotBase,
    "reversion": ReversionSpotBase,
}


def _checked_variant(variant):
    if not isinstance(variant, dict):
        raise TypeError(f"variante debe ser dict, no {type(variant).__name__}")
    if set(variant.keys()) != set(_EXPECTED_KEYS):
        raise ValueError(f"claves de variante deben ser {sorted(_EXPECTED_KEYS)}")
    vid = variant.get("id")
    if not isinstance(vid, str) or not vid:
        raise ValueError(f"id de variante ilegible: {vid!r}")
    try:
        registered = _REGISTRY_BY_ID[vid]
    except KeyError:
        raise KeyError(f"variante desconocida: {vid!r}") from None
    for key in _EXPECTED_KEYS:
        got = variant.get(key)
        want = registered.get(key)
        if got != want:
            raise ValueError(f"variante {vid}: campo {key} no coincide con registro")
    params = variant.get("params")
    if not isinstance(params, dict):
        raise ValueError(f"variante {vid}: params debe ser dict")
    family = variant.get("family")
    if family == "trend":
        if set(params.keys()) != {"fast", "slow"}:
            raise ValueError(f"variante {vid}: params trend deben ser fast/slow")
    elif family == "breakout":
        if set(params.keys()) != {"n", "m"}:
            raise ValueError(f"variante {vid}: params breakout deben ser n/m")
    elif family == "reversion":
        if set(params.keys()) != {"window", "k"}:
            raise ValueError(f"variante {vid}: params reversion deben ser window/k")
    else:
        raise ValueError(f"variante {vid}: familia desconocida {family!r}")
    return registered


def make_strategy(variant):
    """Fabrica cerrada: valida la variante y devuelve la clase IStrategy."""
    registered = _checked_variant(variant)
    family = registered["family"]
    try:
        base = _FAMILY_BASES[family]
    except KeyError:
        raise ValueError(f"familia desconocida: {family!r}") from None
    stop = float(registered["stop"])
    risk_pct = float(registered["risk_pct"])
    exposure = float(registered["exposure"])
    params = registered["params"]
    namespace = {
        "__module__": "strategies.search.SpotCandidates",
        "__doc__": f"Candidata spot cerrada {registered['id']} ({family}).",
        "stoploss": -stop,
        "_STOP": stop,
        "_RISK_PCT": risk_pct,
        "_EXPOSURE": exposure,
    }
    if family == "trend":
        namespace["_FAST"] = int(params["fast"])
        namespace["_SLOW"] = int(params["slow"])
    elif family == "breakout":
        namespace["_N"] = int(params["n"])
        namespace["_M"] = int(params["m"])
    else:
        namespace["_WINDOW"] = int(params["window"])
        namespace["_K"] = float(params["k"])
    return type(str(registered["class_name"]), (base,), namespace)


def render_strategy_module():
    """Delega a la unica renderer pura en research.campaign (sin clones)."""
    from research.campaign import render_strategy_module as _pure

    return _pure(list(_registered_variants()))
