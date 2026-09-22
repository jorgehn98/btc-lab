"""research.campaign: registro cerrado de 72 variantes spot.

72 combinaciones deterministas antes de datos: 3 familias x 4 senales x
2 stops x 3 riesgos. Sin overrides de config, sin lectura de datos,
sin universo libre. IDs estables V000..V071 en orden de tabla.

Orden canonico: familia (trend, breakout, reversion), seed_index 0..3
en orden de tabla PRD, stop (0.02, 0.04), perfil (low, medium, high).
"""

from __future__ import annotations

import copy

TRAIN_YEARS = (2019, 2020, 2021, 2022)
VAL_YEARS = (2023, 2024)
TEST_YEARS = (2025, 2026)
MAX_DRAWDOWN_PCT = 0.15

TREND_SEEDS = ((10, 50), (20, 50), (20, 100), (50, 200))
BREAKOUT_SEEDS = ((24, 12), (48, 24), (96, 48), (168, 84))
REVERSION_SEEDS = ((20, 1.5), (20, 2.0), (40, 1.5), (40, 2.0))
STOPS = (0.02, 0.04)
_RISK_ORDER = (
    ("low", 0.00125, 0.10),
    ("medium", 0.0025, 0.20),
    ("high", 0.005, 0.40),
)
_FAMILIES = ("trend", "breakout", "reversion")
_RESERVE = 0.006


def _build_registry():
    seeds_by_family = {
        "trend": TREND_SEEDS,
        "breakout": BREAKOUT_SEEDS,
        "reversion": REVERSION_SEEDS,
    }
    variants = []
    counter = 0
    for family in _FAMILIES:
        seeds = seeds_by_family[family]
        for seed_index, seed in enumerate(seeds):
            if family == "trend":
                params = {"fast": int(seed[0]), "slow": int(seed[1])}
            elif family == "breakout":
                params = {"n": int(seed[0]), "m": int(seed[1])}
            else:
                params = {"window": int(seed[0]), "k": float(seed[1])}
            for stop in STOPS:
                for profile, risk_pct, exposure in _RISK_ORDER:
                    vid = f"V{counter:03d}"
                    variants.append({
                        "id": vid,
                        "family": family,
                        "seed_index": int(seed_index),
                        "params": dict(params),
                        "stop": float(stop),
                        "risk_profile": profile,
                        "risk_pct": float(risk_pct),
                        "exposure": float(exposure),
                        "class_name": f"SpotCandidateV{counter:03d}",
                    })
                    counter += 1
    return variants


_REGISTRY = _build_registry()
_BY_ID = {v["id"]: v for v in _REGISTRY}


def generate_variants():
    """Devuelve deepcopies de las 72 variantes en orden canonico."""
    return copy.deepcopy(_REGISTRY)


def render_strategy_module(variants=None):
    """Fuente estatica determinista con las 72 clases para el resolver nativo.

    Pura stdlib (sin pandas/freqtrade) para que el host pueda generarla sin
    importar SpotCandidates. Unica funcion renderer: SpotCandidates delega
    aqui, sin clones.
    """
    regs = list(variants) if variants is not None else generate_variants()
    if len(regs) != 72:
        raise ValueError(f"renderer exige 72 variantes, fue {len(regs)}")
    lines = []
    lines.append('"""Modulo estatico generado: 72 candidatas spot para lote nativo."""')
    lines.append("")
    lines.append("from strategies.search.SpotCandidates import (")
    lines.append("    BreakoutSpotBase,")
    lines.append("    ReversionSpotBase,")
    lines.append("    TrendSpotBase,")
    lines.append(")")
    lines.append("")
    for variant in regs:
        name = str(variant["class_name"])
        family = str(variant["family"])
        stop = float(variant["stop"])
        risk_pct = float(variant["risk_pct"])
        exposure = float(variant["exposure"])
        params = variant["params"]
        if family == "trend":
            base_name = "TrendSpotBase"
            extra = f"    _FAST = {int(params['fast'])}\n    _SLOW = {int(params['slow'])}"
        elif family == "breakout":
            base_name = "BreakoutSpotBase"
            extra = f"    _N = {int(params['n'])}\n    _M = {int(params['m'])}"
        else:
            base_name = "ReversionSpotBase"
            extra = f"    _WINDOW = {int(params['window'])}\n    _K = {float(params['k'])!r}"
        lines.append(f"class {name}({base_name}):")
        lines.append(f'    """Candidata {variant["id"]} {family} stop {stop}."""')
        lines.append(f"    stoploss = {-stop!r}")
        lines.append(f"    _STOP = {stop!r}")
        lines.append(f"    _RISK_PCT = {risk_pct!r}")
        lines.append(f"    _EXPOSURE = {exposure!r}")
        lines.append(extra)
        lines.append("")
    return "\n".join(lines) + "\n"


def neighbors(variant_id):
    """IDs vecinos PRD: mismo perfil/familia, otro stop o semilla adyacente."""
    vid = str(variant_id)
    try:
        current = _BY_ID[vid]
    except KeyError:
        raise KeyError(f"variante desconocida: {vid!r}") from None
    family = current["family"]
    profile = current["risk_profile"]
    seed_index = int(current["seed_index"])
    stop = float(current["stop"])
    other_stop = 0.04 if abs(stop - 0.02) < 1e-12 else 0.02
    found = []
    for cand in _REGISTRY:
        nid = cand["id"]
        if nid == vid:
            continue
        if cand["family"] != family:
            continue
        if cand["risk_profile"] != profile:
            continue
        cand_seed = int(cand["seed_index"])
        cand_stop = float(cand["stop"])
        same_seed_other_stop = (
            cand_seed == seed_index and abs(cand_stop - other_stop) < 1e-12
        )
        adjacent_same_stop = (
            abs(cand_stop - stop) < 1e-12 and abs(cand_seed - seed_index) == 1
        )
        if same_seed_other_stop or adjacent_same_stop:
            found.append(nid)
    return sorted(found)
