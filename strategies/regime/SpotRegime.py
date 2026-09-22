"""SMA50/200 spot signals for the closed 24-cell regime study."""

import pandas as pd

from strategies.search.SpotCandidates import (
    _SpotSearchBase,
    _exit_ready,
    _pair_ok,
    _time_valid,
    _window_ok,
)

WARMUP = 249


class RegimeSpotBase(_SpotSearchBase):
    startup_candle_count = WARMUP
    trailing_stop = False
    _REENTRY = False
    _SLOPE_FILTER = False
    _EARLY_EXIT = False
    _TRAIL = False

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        close = pd.to_numeric(dataframe["close"], errors="coerce")
        dataframe["sma_fast"] = close.rolling(window=50, min_periods=50).mean()
        dataframe["sma200"] = close.rolling(window=200, min_periods=200).mean()
        dataframe["sma200_prior"] = dataframe["sma200"].shift(48)
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""
        if len(dataframe) < WARMUP:
            return dataframe
        if any(col not in dataframe for col in (
                "date", "close", "volume", "sma_fast", "sma200", "sma200_prior")):
            return dataframe
        fast, slow = dataframe["sma_fast"], dataframe["sma200"]
        close = dataframe["close"]
        ready = (_window_ok(close, WARMUP) & _window_ok(dataframe["volume"], WARMUP)
                 & _time_valid(dataframe["date"], WARMUP) & _pair_ok(fast, slow))
        bullish = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        pullback = ((close > fast) & (close.shift(1) <= fast.shift(1))
                    & (fast > slow))
        signal = bullish | (pullback if self._REENTRY is True else False)
        signal &= close > slow
        if self._SLOPE_FILTER is True:
            signal &= (slow > dataframe["sma200_prior"]) & dataframe["sma200_prior"].notna()
        signal = (signal.fillna(False) & ready.fillna(False)).to_numpy()
        dataframe.loc[signal, "enter_long"] = 1
        dataframe.loc[signal, "enter_tag"] = "regime_bull"
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""
        if len(dataframe) < WARMUP:
            return dataframe
        if any(col not in dataframe for col in ("date", "close", "sma_fast", "sma200")):
            return dataframe
        fast, slow = dataframe["sma_fast"], dataframe["sma200"]
        bearish = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        signal = bearish | (dataframe["close"] < slow)
        if self._EARLY_EXIT is True:
            signal |= dataframe["close"] < fast
        signal = (signal.fillna(False) & _exit_ready(dataframe)
                  & _pair_ok(fast, slow)).to_numpy()
        dataframe.loc[signal, "exit_long"] = 1
        dataframe.loc[signal, "exit_tag"] = "regime_exit"
        return dataframe
