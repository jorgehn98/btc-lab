"""Pure TRAIN selection for the closed 48-cell SMA50/200 study."""

import math

from research import selection
from research.regime import generate_variants, neighbors

_REGISTRY = generate_variants()
_KNOWN_IDS = {variant["id"] for variant in _REGISTRY}
_HURDLE = math.log1p(0.0005)
_CALENDAR_DAYS = 1461.0


def _calendar_g(years):
    days = sum(float(rec["days"]) for rec in years)
    if not 0 < days <= _CALENDAR_DAYS:
        raise ValueError("dias observados fuera de 2019-2022")
    return sum(float(rec["g"]) * float(rec["days"]) for rec in years) / _CALENDAR_DAYS


def summarize_train(records):
    """Descriptive rates for every cell and matched trailing/control pairs."""
    recs = selection._validate_all(records, _KNOWN_IDS)
    rows = []
    for variant in _REGISTRY:
        vid = variant["id"]
        years = selection._collect_window(
            recs, vid, "train", selection.TRAIN_SEL, selection.FEE_PRIMARY)
        whole = selection._collect_window(
            recs, vid, "train", selection.TRAIN_ALL, selection.FEE_PRIMARY)
        row = {"id": vid, "risk_profile": variant["risk_profile"],
               "params": dict(variant["params"])}
        if years is None or whole is None:
            row.update({"valid": False, "invalid_years": [
                year for year in selection.TRAIN_ALL
                if (rec := selection._lookup(recs, vid, "train", year,
                                             selection.FEE_PRIMARY)) is None
                or rec["valid"] is not True]})
        else:
            days = sum(float(rec["days"]) for rec in years)
            bh_calendar = sum(float(rec["bh_g"]) * float(rec["days"])
                              for rec in years) / _CALENDAR_DAYS
            row.update({
                "valid": True, "observed_days": days,
                "calendar_days": int(_CALENDAR_DAYS),
                "g_observed": selection._weighted_g(years),
                "g_calendar": _calendar_g(years),
                "bh_g_calendar": bh_calendar,
                "max_drawdown_pct": selection._max_dd(whole),
                "trades_nonforced": selection._sum_trades(years),
                "positive_years": sum(float(rec["g"]) > 0 for rec in years),
                "median_excess": selection._median_excess(years),
                "g_without_positive_forced": selection._weighted_forced(years),
            })
        rows.append(row)
    indexed = {row["id"]: row for row in rows}
    pairs = []
    for variant in _REGISTRY:
        params = variant["params"]
        if params["trailing"]:
            continue
        match = next(candidate for candidate in _REGISTRY
                     if candidate["risk_profile"] == variant["risk_profile"]
                     and candidate["params"] == {**params, "trailing": True})
        off, on = indexed[variant["id"]], indexed[match["id"]]
        pair = {"without": variant["id"], "with": match["id"],
                "valid": off["valid"] and on["valid"]}
        if pair["valid"]:
            pair.update({"delta_g_calendar": on["g_calendar"] - off["g_calendar"],
                         "delta_max_drawdown_pct": (on["max_drawdown_pct"]
                                                    - off["max_drawdown_pct"]),
                         "delta_trades_nonforced": (on["trades_nonforced"]
                                                    - off["trades_nonforced"])})
        pairs.append(pair)
    return {"unit": "daily_log_return", "hurdle": _HURDLE,
            "calendar_days": int(_CALENDAR_DAYS),
            "variants": rows, "trailing_pairs": pairs}


def walk_forward_select(records, evaluation_year):
    recs = selection._validate_all(records, _KNOWN_IDS)
    if type(evaluation_year) is not int or evaluation_year not in (2019, 2020, 2021, 2022):
        raise ValueError("ano walk-forward fuera de TRAIN seleccionado")
    prefix = tuple(year for year in selection.TRAIN_ALL if year < evaluation_year)
    result = {risk: None for risk in ("low", "medium", "high")}
    for risk in result:
        best = None
        for variant in _REGISTRY:
            if variant["risk_profile"] != risk:
                continue
            vid = variant["id"]
            items = selection._collect_window(
                recs, vid, "train", prefix, selection.FEE_PRIMARY)
            if (items is None or selection._sum_trades(items) < 30
                    or selection._max_dd(items) > selection.DD_LIMIT
                    or selection._weighted_g(items) <= 0):
                continue
            key = selection._rank_key(
                vid, selection._weighted_g(items),
                selection._max_dd(items), selection._sum_turnover(items))
            if best is None or key < best[0]:
                best = (key, vid)
        if best is not None:
            result[risk] = best[1]
    return result


def choose_train_finalists(records):
    recs = selection._validate_all(records, _KNOWN_IDS)
    winners = []
    for risk in ("low", "medium", "high"):
        best = None
        for variant in _REGISTRY:
            if variant["risk_profile"] != risk:
                continue
            vid = variant["id"]
            years = selection._collect_window(
                recs, vid, "train", selection.TRAIN_SEL, selection.FEE_PRIMARY)
            whole = selection._collect_window(
                recs, vid, "train", selection.TRAIN_ALL, selection.FEE_PRIMARY)
            if years is None or whole is None:
                continue
            if (selection._sum_trades(years) < 100
                    or sum(float(r["g"]) > 0 for r in years) < 3
                    or selection._median_excess(years) <= 0
                    or selection._max_dd(whole) > selection.DD_LIMIT
                    or selection._weighted_forced(years) < 0
                    or selection._weighted_g(years) < _HURDLE
                    or _calendar_g(years) < _HURDLE):
                continue
            positive_neighbors = sum(
                selection._weighted_g(nyears) > 0
                for neighbor in neighbors(vid)
                if (nyears := selection._collect_window(
                    recs, neighbor, "train", selection.TRAIN_SEL,
                    selection.FEE_PRIMARY)) is not None)
            if positive_neighbors < 2:
                continue
            key = selection._rank_key(
                vid, selection._weighted_g(years),
                selection._max_dd(whole), selection._sum_turnover(years))
            if best is None or key < best[0]:
                best = (key, vid)
        if best is not None:
            winners.append(best[1])
    return sorted(winners)
