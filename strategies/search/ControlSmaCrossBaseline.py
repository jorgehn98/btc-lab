"""strategies.search.ControlSmaCrossBaseline: control equitativo para la campana.

Envuelve la estrategia original SmaCrossBaseline sin alterar su logica ni su
cap de 1000 USDT: solo eleva startup_candle_count a 201 para la misma ventana
efectiva que las 72 candidatas (warmup comun 201 del mismo pasado contiguo).
El sizing, stops y senales son los originales; el hash de este modulo mas el
de SmaCrossBaseline fijan la identidad del control.
"""

from __future__ import annotations

from strategies.baseline.SmaCrossBaseline import SmaCrossBaseline


class ControlSmaCrossBaseline(SmaCrossBaseline):
    """Control baseline con ventana efectiva comun 201 (cap 1000 intacto)."""

    startup_candle_count = 201
