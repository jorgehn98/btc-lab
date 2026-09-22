"""market.train: datos TRAIN BTC/USDT spot 5m -> 1h (solo stdlib + pandas).

Intervalo cerrado TRAIN: semiabierto UTC [2018-01-01, 2023-01-01).
VALIDATION/TEST nunca se descargan aquí.

Funciones puras (testeadas en runtime fijado):
- validate_ohlcv(df, start, end, timeframe_minutes): ValueError si duplicados,
  desorden, desalineación de grid, fuera de límites, OHLC inválido, no finito,
  precio no positivo o volumen negativo. No ordena ni dedup antes de validar.
- aggregate_hourly(df_5m): 1h solo de grupos completos de 12 velas 5m
  alineadas (00,05,...,55), sin rellenar. OHLC agregados y volumen suma.
- contiguous_segments(df_1h): partición por cobertura (saltos != 1h),
  sin inventar ni perder velas; segmentos cortos se conservan visibles.
- train_download_argv: argv cerrado para `freqtrade download-data`.
Snapshot único lo maneja operations.research con estas puras.
"""

from __future__ import annotations

from datetime import datetime, timezone

TRAIN_START = datetime(2018, 1, 1, tzinfo=timezone.utc)
TRAIN_END = datetime(2023, 1, 1, tzinfo=timezone.utc)
TRAIN_PAIR = "BTC/USDT"
TRAIN_TIMEFRAME = "5m"
TRAIN_TIMERANGE = "20180101-20230101"
TRAIN_CANDLE_TYPE = "spot"
TRAIN_EXCHANGE = "binance"

REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def _ensure_frame(df):
    if df is None or len(df) == 0:
        raise ValueError("dataframe vacío")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"columnas ausentes: {missing}")
    return df


def validate_ohlcv(df, start, end, timeframe_minutes=5) -> None:
    """Valida OHLCV dentro de [start, end); ValueError si incumple."""
    import numpy as np
    import pandas as pd

    frame = _ensure_frame(df)
    try:
        timeframe = int(timeframe_minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"timeframe inválido: {timeframe_minutes!r}") from exc
    if timeframe <= 0:
        raise ValueError(f"timeframe debe ser positivo: {timeframe!r}")

    raw_conv = pd.to_datetime(frame["date"], errors="coerce", utc=False)
    if bool(raw_conv.isna().any()):
        raise ValueError("timestamps ilegibles")
    if getattr(raw_conv.dtype, "tz", None) is None:
        raise ValueError("timestamp sin zona UTC")
    stamps = raw_conv.dt.tz_convert("UTC")

    if not bool(stamps.is_unique):
        raise ValueError("timestamp duplicado debe rechazarse")
    if not bool(stamps.is_monotonic_increasing):
        raise ValueError("timestamps desordenados deben rechazarse")
    floored = stamps.dt.floor(f"{timeframe}min")
    if not bool((floored == stamps).all()):
        raise ValueError("timestamp desalineado del grid debe rechazarse")

    start_ts = pd.Timestamp(start)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    else:
        start_ts = start_ts.tz_convert("UTC")
    end_ts = pd.Timestamp(end)
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    else:
        end_ts = end_ts.tz_convert("UTC")
    if not bool(((stamps >= start_ts) & (stamps < end_ts)).all()):
        raise ValueError("timestamp fuera de rango")

    cols = {}
    for col in ("open", "high", "low", "close", "volume"):
        numeric = pd.to_numeric(frame[col], errors="coerce")
        if bool(numeric.isna().any()):
            raise ValueError(f"{col} no numérico o NaN")
        arr = numeric.to_numpy(dtype="float64")
        if not bool(np.all(np.isfinite(arr))):
            raise ValueError(f"{col} no finito")
        cols[col] = arr
    ohlc = (cols["open"], cols["high"], cols["low"], cols["close"])
    for label, arr in (("open", cols["open"]), ("high", cols["high"]),
                       ("low", cols["low"]), ("close", cols["close"])):
        if bool(np.any(arr <= 0.0)):
            raise ValueError(f"precio no positivo {label}")
    if bool(np.any(cols["volume"] < 0.0)):
        raise ValueError("volumen negativo")
    o, h, lo, c = cols["open"], cols["high"], cols["low"], cols["close"]
    if not bool(np.all(h >= lo)):
        raise ValueError("high<low")
    if not bool(np.all((h >= o) & (h >= c))):
        raise ValueError("high no cubre open/close")
    if not bool(np.all((lo <= o) & (lo <= c))):
        raise ValueError("low no cubre open/close")


def aggregate_hourly(df_5m):
    """Agrega 5m a 1h solo con 12 velas alineadas; grupos malformados se omiten."""
    import numpy as np
    import pandas as pd

    if df_5m is None or len(df_5m) == 0:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
    missing = [c for c in REQUIRED_COLUMNS if c not in df_5m.columns]
    if missing:
        raise ValueError(f"columnas ausentes: {missing}")
    try:
        stamps = pd.to_datetime(df_5m["date"], utc=True, errors="coerce")
    except Exception as exc:
        raise ValueError(f"fechas ilegibles para agregación: {exc}") from exc
    if bool(stamps.isna().any()):
        raise ValueError("fechas ilegibles para agregación")
    work = pd.DataFrame({
        "stamp": stamps.to_numpy(),
        "open": pd.to_numeric(df_5m["open"], errors="coerce").to_numpy(dtype="float64"),
        "high": pd.to_numeric(df_5m["high"], errors="coerce").to_numpy(dtype="float64"),
        "low": pd.to_numeric(df_5m["low"], errors="coerce").to_numpy(dtype="float64"),
        "close": pd.to_numeric(df_5m["close"], errors="coerce").to_numpy(dtype="float64"),
        "volume": pd.to_numeric(df_5m["volume"], errors="coerce").to_numpy(dtype="float64"),
    })
    work = work.sort_values("stamp").reset_index(drop=True)
    work["stamp"] = pd.to_datetime(work["stamp"], utc=True)
    work["hour"] = work["stamp"].dt.floor("h")
    work["aligned"] = work["stamp"].dt.floor("5min") == work["stamp"]
    work["offset"] = (work["stamp"] - work["hour"]).dt.total_seconds() / 60.0
    indexed = work.set_index("stamp").sort_index()
    resampled = indexed.resample("h", on=None).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    counts = indexed.resample("h").size()
    aligned_all = indexed.groupby(pd.Grouper(freq="h"))["aligned"].all()
    nunique = indexed.groupby(pd.Grouper(freq="h"))["offset"].nunique()
    mask = (counts == 12) & (aligned_all.fillna(False)) & (nunique.fillna(0) == 12)
    kept = resampled.loc[mask.fillna(False)].copy()
    if kept.empty:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
    for col in ("open", "high", "low", "close", "volume"):
        arr = kept[col].to_numpy(dtype="float64")
        kept = kept[np.isfinite(arr)]
        if kept.empty:
            return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
    kept = kept.reset_index().rename(columns={"stamp": "date", "index": "date"})
    if "date" not in kept.columns:
        kept = kept.rename(columns={kept.columns[0]: "date"})
    kept["date"] = pd.to_datetime(kept["date"], utc=True)
    return kept[["date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True)


def contiguous_segments(df_1h):
    """Parte 1h en segmentos contiguos (saltos != 1h); conserva todo."""
    import pandas as pd

    if df_1h is None or len(df_1h) == 0:
        return []
    missing = [c for c in REQUIRED_COLUMNS if c not in df_1h.columns]
    if missing:
        raise ValueError(f"columnas ausentes: {missing}")
    ordered = df_1h.sort_values("date").reset_index(drop=True)
    try:
        stamps = pd.to_datetime(ordered["date"], utc=True, errors="coerce")
    except Exception as exc:
        raise ValueError(f"fechas 1h ilegibles: {exc}") from exc
    if bool(stamps.isna().any()):
        raise ValueError("fechas 1h ilegibles")
    diffs = stamps.diff().dt.total_seconds()
    new_seg = (diffs != 3600.0).fillna(True)
    seg_id = new_seg.cumsum()
    return [group.reset_index(drop=True) for _, group in ordered.groupby(seg_id, sort=False)]


def gaps_table(df_1h):
    """Tabla de huecos entre velas 1h contiguas esperadas (para informe)."""
    import pandas as pd

    if df_1h is None or len(df_1h) < 2:
        return []
    ordered = df_1h.sort_values("date").reset_index(drop=True)
    stamps = pd.to_datetime(ordered["date"], utc=True)
    gaps = []
    for pos in range(1, len(ordered)):
        delta_h = (stamps.iloc[pos] - stamps.iloc[pos - 1]).total_seconds() / 3600.0
        if delta_h != 1.0:
            gaps.append({
                "prev": stamps.iloc[pos - 1].isoformat(),
                "next": stamps.iloc[pos].isoformat(),
                "missing_hours": int(round(delta_h - 1.0)),
            })
    return gaps


def train_download_argv(config_path: str, datadir: str) -> list:
    """Argv cerrado para descargar TRAIN 5m (sin flags libres)."""
    return [
        "freqtrade", "download-data",
        "--config", str(config_path),
        "--datadir", str(datadir),
        "--pairs", TRAIN_PAIR,
        "--timeframes", TRAIN_TIMEFRAME,
        "--timerange", TRAIN_TIMERANGE,
        "--trading-mode", "spot",
        "--candle-types", TRAIN_CANDLE_TYPE,
        "--exchange", TRAIN_EXCHANGE,
    ]
