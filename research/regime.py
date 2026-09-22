"""Closed 24-cell SMA50/200 regime study, independent from the first campaign."""

import copy

CAMPAIGN_ID = "btc-spot-regime-study-pr01"
STOP = 0.02
WARMUP = 249
_RISK_ORDER = (
    ("low", 0.00125, 0.10),
    ("medium", 0.0025, 0.20),
    ("high", 0.005, 0.40),
)


def _make_variants():
    variants = []
    for reentry in (False, True):
        for slope in (False, True):
            for early_exit in (False, True):
                for risk_profile, risk_pct, exposure in _RISK_ORDER:
                    index = len(variants)
                    variants.append({
                        "id": f"R{index:03d}",
                        "class_name": f"RegimeCandidateR{index:03d}",
                        "family": "trend",
                        "risk_profile": risk_profile,
                        "risk_pct": risk_pct,
                        "exposure": exposure,
                        "stop": STOP,
                        "params": {"fast": 50, "slow": 200,
                                   "reentry": reentry, "slope": slope,
                                   "early_exit": early_exit},
                    })
    return variants


_REGISTRY = _make_variants()
_BY_ID = {variant["id"]: variant for variant in _REGISTRY}


def generate_variants():
    return copy.deepcopy(_REGISTRY)


def neighbors(variant_id):
    try:
        current = _BY_ID[str(variant_id)]
    except KeyError:
        raise KeyError(f"variante desconocida: {variant_id!r}") from None
    params = current["params"]
    toggles = ("reentry", "slope", "early_exit")
    return sorted(other["id"] for other in _REGISTRY
                  if other["risk_profile"] == current["risk_profile"]
                  and sum(other["params"][flag] != params[flag]
                          for flag in toggles) == 1)


def render_strategy_module(variants=None):
    regs = generate_variants() if variants is None else list(variants)
    if regs != _REGISTRY:
        raise ValueError("renderer solo acepta las 24 variantes preregistradas")
    lines = [
        '"""Generated fixed SMA50/200 study; no runtime parameters."""',
        "from strategies.regime.SpotRegime import RegimeSpotBase",
        "",
    ]
    for variant in regs:
        lines.extend((
            f'class {variant["class_name"]}(RegimeSpotBase):',
            f'    stoploss = -{STOP!r}',
            f'    _STOP = {STOP!r}',
            f'    _RISK_PCT = {variant["risk_pct"]!r}',
            f'    _EXPOSURE = {variant["exposure"]!r}',
            f'    _REENTRY = {variant["params"]["reentry"]!r}',
            f'    _SLOPE_FILTER = {variant["params"]["slope"]!r}',
            f'    _EARLY_EXIT = {variant["params"]["early_exit"]!r}',
            "",
        ))
    return "\n".join(lines) + "\n"
