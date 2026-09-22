"""Pure TRAIN selection for the closed 24-cell SMA50/200 study."""

from research import selection
from research.regime import generate_variants, neighbors

_REGISTRY = generate_variants()
_KNOWN_IDS = {variant["id"] for variant in _REGISTRY}


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
                    or selection._weighted_forced(years) < 0):
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
