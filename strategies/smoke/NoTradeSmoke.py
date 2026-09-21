"""NoTradeSmoke: estrategia de smoke que nunca opera (T03).

Cumple IStrategy de la imagen fijada y genera siempre enter/exit a cero,
sin short ni ajuste de posición. El stoploss es válido pero decorativo:
`confirm_trade_entry` bloquea cualquier entrada como segunda barrera.
No es un candidato ni evidencia económica.
"""

from pandas import DataFrame

from freqtrade.strategy import IStrategy


class NoTradeSmoke(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    # ROI inalcanzable: el smoke nunca entra, así que nunca sale.
    minimal_roi = {"0": 100.0}
    stoploss = -0.10
    can_short = False
    position_adjustment_enable = False
    startup_candle_count = 20

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        dataframe["exit_tag"] = ""
        return dataframe

    def confirm_trade_entry(self, *args, **kwargs) -> bool:
        return False
