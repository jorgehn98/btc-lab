"""research.selection: seleccion pura WF/final/validacion/test (solo stdlib).

Fuente PRD + tasks/06: 72 spot, DD 15% MTM pct, presupuesto 12h, TEST unico.
Funciones puras sobre registros sinteticos con ledger ya calculado; no IO,
no reimplementa vecinos (usa research.campaign.neighbors), no CAGR ni
cartera falsa. Esquema canonico 12 campos por registro.
"""

from __future__ import annotations

import math

from research.campaign import (
    MAX_DRAWDOWN_PCT,
    TEST_YEARS as CAMPAIGN_TEST_YEARS,
    TRAIN_YEARS as CAMPAIGN_TRAIN_SEL,
    VAL_YEARS as CAMPAIGN_VAL_YEARS,
    generate_variants,
    neighbors,
)

FEE_PRIMARY = 0.002
FEE_STRESS = 0.003
DD_LIMIT = float(MAX_DRAWDOWN_PCT)
TRAIN_ALL = (2017, 2018, 2019, 2020, 2021, 2022)
TRAIN_SEL = tuple(CAMPAIGN_TRAIN_SEL)
VAL_YEARS = tuple(CAMPAIGN_VAL_YEARS)
TEST_YEARS = tuple(CAMPAIGN_TEST_YEARS)

RECORD_FIELDS = (
    "variant_id", "role", "year", "fee", "valid", "days", "g", "bh_g",
    "max_drawdown_pct", "trades_nonforced", "turnover",
    "g_without_positive_forced",
)

_REGISTRY = generate_variants()
_KNOWN_IDS = {str(v.get("id")) for v in _REGISTRY}
_BY_ID = {str(v.get("id")): v for v in _REGISTRY}
_FEE_TOL = 1e-12


def _fee_match(value, target) -> bool:
    try:
        return abs(float(value) - float(target)) <= _FEE_TOL
    except (TypeError, ValueError):
        return False


def _validate_record(rec, known_ids=None) -> None:
    if not isinstance(rec, dict):
        raise ValueError("registro debe ser dict")
    for field in RECORD_FIELDS:
        if field not in rec:
            raise ValueError(f"registro sin campo requerido: {field}")
    vid = rec.get("variant_id")
    if not isinstance(vid, str) or not vid:
        raise ValueError(f"variant_id ilegible: {vid!r}")
    if str(vid) not in (_KNOWN_IDS if known_ids is None else known_ids):
        raise ValueError(f"variant_id desconocida: {vid!r}")
    role = rec.get("role")
    if role not in ("train", "validation", "test"):
        raise ValueError(f"role ilegible: {role!r}")
    year = rec.get("year")
    if type(year) is bool or not isinstance(year, int):
        raise ValueError(f"year debe ser int no bool: {year!r}")
    fee = rec.get("fee")
    if type(fee) is bool or not isinstance(fee, (int, float)):
        raise ValueError(f"fee debe ser numerico no bool: {fee!r}")
    if not math.isfinite(float(fee)) or float(fee) < 0.0:
        raise ValueError(f"fee no finito >=0: {fee!r}")
    valid = rec.get("valid")
    if type(valid) is not bool:
        raise ValueError(f"valid debe ser bool: {valid!r}")
    days = rec.get("days")
    if type(days) is bool or not isinstance(days, (int, float)):
        raise ValueError(f"days debe ser numerico no bool: {days!r}")
    if not math.isfinite(float(days)) or float(days) <= 0.0:
        raise ValueError(f"days debe ser finito >0: {days!r}")
    for field in ("g", "bh_g", "g_without_positive_forced"):
        val = rec.get(field)
        if type(val) is bool or not isinstance(val, (int, float)):
            raise ValueError(f"{field} debe ser numerico no bool: {val!r}")
        if not math.isfinite(float(val)):
            raise ValueError(f"{field} no finito: {val!r}")
    dd = rec.get("max_drawdown_pct")
    if type(dd) is bool or not isinstance(dd, (int, float)):
        raise ValueError(f"max_drawdown_pct no numerico/no bool: {dd!r}")
    if not math.isfinite(float(dd)):
        raise ValueError("max_drawdown_pct no finito")
    if not 0.0 <= float(dd) <= 1.0:
        raise ValueError(f"max_drawdown_pct fuera 0..1: {dd!r}")
    trades = rec.get("trades_nonforced")
    if type(trades) is bool or not isinstance(trades, int):
        raise ValueError(f"trades_nonforced debe ser int no bool: {trades!r}")
    if int(trades) < 0:
        raise ValueError("trades_nonforced debe ser >=0")
    turnover = rec.get("turnover")
    if type(turnover) is bool or not isinstance(turnover, (int, float)):
        raise ValueError(f"turnover no numerico/no bool: {turnover!r}")
    if not math.isfinite(float(turnover)) or float(turnover) < 0.0:
        raise ValueError(f"turnover debe ser finito >=0: {turnover!r}")


def _validate_all(records, known_ids=None) -> list:
    if not isinstance(records, (list, tuple)):
        raise ValueError("records debe ser lista")
    out = list(records)
    seen = set()
    for rec in out:
        _validate_record(rec, known_ids)
        key = (
            str(rec.get("variant_id")),
            str(rec.get("role")),
            int(rec.get("year")),
            round(float(rec.get("fee")), 9),
        )
        if key in seen:
            raise ValueError(f"registro duplicado por clave: {key!r}")
        seen.add(key)
    return out


def _median(values) -> float:
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if n == 0:
        raise ValueError("mediana vacia")
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def _lookup(records, variant_id, role, year, fee):
    for rec in records:
        if (str(rec.get("variant_id")) == str(variant_id)
                and rec.get("role") == role
                and int(rec.get("year")) == int(year)
                and _fee_match(rec.get("fee"), fee)):
            return rec
    return None


def _weighted_g(items) -> float:
    num = 0.0
    den = 0.0
    for rec in items:
        g = float(rec["g"])
        days = float(rec["days"])
        num += g * days
        den += days
    if den <= 0.0:
        raise ValueError("dias observados deben ser >0")
    return num / den


def _weighted_forced(items) -> float:
    num = 0.0
    den = 0.0
    for rec in items:
        g = float(rec["g_without_positive_forced"])
        days = float(rec["days"])
        num += g * days
        den += days
    if den <= 0.0:
        raise ValueError("dias observados deben ser >0")
    return num / den


def _max_dd(items) -> float:
    return max(float(r["max_drawdown_pct"]) for r in items)


def _sum_trades(items) -> int:
    return sum(int(r["trades_nonforced"]) for r in items)


def _sum_turnover(items) -> float:
    return sum(float(r["turnover"]) for r in items)


def _median_excess(items) -> float:
    excess = [float(r["g"]) - float(r["bh_g"]) for r in items]
    return _median(excess)


def _collect_window(records, variant_id, role, years, fee):
    """Devuelve lista ordenada por ano o None si falta/invalido alguno."""
    items = []
    for year in years:
        rec = _lookup(records, variant_id, role, year, fee)
        if rec is None:
            return None
        if rec.get("valid") is not True:
            return None
        items.append(rec)
    return items


def _rank_key(variant_id, weighted, maxdd, turnover):
    return (-float(weighted), float(maxdd), float(turnover), str(variant_id))


# --- Walk-forward descriptivo por perfil (solo pasado, sin futuro) ---

def walk_forward_select(records, evaluation_year):
    recs = _validate_all(records)
    if type(evaluation_year) is bool or not isinstance(evaluation_year, int):
        raise ValueError("evaluation_year debe ser int")
    prefix = tuple(y for y in TRAIN_ALL if int(y) < int(evaluation_year))
    result = {"low": None, "medium": None, "high": None}
    if not prefix:
        return result
    for risk in ("low", "medium", "high"):
        candidates = [v["id"] for v in _REGISTRY if v.get("risk_profile") == risk]
        best = None
        best_key = None
        for vid in candidates:
            items = _collect_window(recs, vid, "train", prefix, FEE_PRIMARY)
            if items is None:
                continue
            # Minimo 30 acumulado en el prefijo (PRD: TOTAL, sin rescatar).
            if _sum_trades(items) < 30:
                continue
            if _max_dd(items) > DD_LIMIT:
                continue
            weighted = _weighted_g(items)
            # Cero no es positivo; no se descarta en silencio, se rechaza.
            if not weighted > 0.0:
                continue
            key = _rank_key(vid, weighted, _max_dd(items), _sum_turnover(items))
            if best_key is None or key < best_key:
                best_key = key
                best = vid
        result[risk] = best
    return result


# --- Finalistas TRAIN: 9 maximo, uno por familia/riesgo, solo TRAIN 2019-22 ---

def _train_eligible(records, variant_id):
    sel = _collect_window(records, variant_id, "train", TRAIN_SEL, FEE_PRIMARY)
    if sel is None:
        return None
    if _sum_trades(sel) < 100:
        return None
    positive_years = sum(1 for r in sel if float(r["g"]) > 0.0)
    if positive_years < 3:
        return None
    if not _median_excess(sel) > 0.0:
        return None
    # DD sobre todo TRAIN disponible (2017-22) a coste primario.
    all_train = _collect_window(records, variant_id, "train", TRAIN_ALL, FEE_PRIMARY)
    if all_train is None:
        return None
    if _max_dd(all_train) > DD_LIMIT:
        return None
    if not _weighted_forced(sel) >= 0.0:
        return None
    # Vecinos: al menos 2 con g ponderada >0 en la misma ventana primaria.
    try:
        neigh = neighbors(str(variant_id))
    except (KeyError, ValueError):
        raise ValueError(f"variante desconocida: {variant_id!r}")
    good = 0
    for nid in neigh:
        nsel = _collect_window(records, nid, "train", TRAIN_SEL, FEE_PRIMARY)
        if nsel is None:
            continue
        if _weighted_g(nsel) > 0.0:
            good += 1
    if good < 2:
        return None
    weighted = _weighted_g(sel)
    return {
        "weighted": weighted,
        "maxdd": _max_dd(all_train),
        "turnover": _sum_turnover(sel),
    }


def choose_train_finalists(records):
    recs = _validate_all(records)
    winners = []
    for family in ("trend", "breakout", "reversion"):
        for risk in ("low", "medium", "high"):
            group = [v["id"] for v in _REGISTRY
                     if v.get("family") == family and v.get("risk_profile") == risk]
            best = None
            best_key = None
            for vid in group:
                info = _train_eligible(recs, vid)
                if info is None:
                    continue
                key = _rank_key(vid, info["weighted"], info["maxdd"], info["turnover"])
                if best_key is None or key < best_key:
                    best_key = key
                    best = vid
            if best is not None:
                winners.append(best)
    return sorted(winners)


# --- Hasta 3 para VALIDATION: uno por perfil con TRAIN, luego stress/bias ---

def choose_validation_candidates(records, train_ids, bias_verdicts):
    recs = _validate_all(records)
    if not isinstance(train_ids, (list, tuple)):
        raise ValueError("train_ids debe ser lista")
    tids = list(train_ids)
    if len(tids) != len(set(str(x) for x in tids)):
        raise ValueError("train_ids duplicados")
    if len(tids) > 9 or len(tids) == 0:
        raise ValueError("train_ids debe tener 1..9")
    for vid in tids:
        if not isinstance(vid, str) or str(vid) not in _KNOWN_IDS:
            raise ValueError(f"train_id desconocida: {vid!r}")
    if not isinstance(bias_verdicts, dict):
        raise ValueError("bias_verdicts debe ser dict")
    # Mejor por perfil usando solo TRAIN 2019-22 primario (sin backfill).
    picked = {}
    for risk in ("low", "medium", "high"):
        group = [vid for vid in tids if _BY_ID[str(vid)].get("risk_profile") == risk]
        if not group:
            continue
        best = None
        best_key = None
        for vid in group:
            sel = _collect_window(recs, vid, "train", TRAIN_SEL, FEE_PRIMARY)
            if sel is None:
                continue
            key = _rank_key(vid, _weighted_g(sel), _max_dd(sel), _sum_turnover(sel))
            if best_key is None or key < best_key:
                best_key = key
                best = vid
        if best is not None:
            picked[risk] = best
    # Filtro stress 0.3 + sesgo exacto True; si falla, slot vacio sin sustituir.
    out = []
    for risk, vid in picked.items():
        stress = _collect_window(recs, vid, "train", TRAIN_ALL, FEE_STRESS)
        if stress is None:
            continue
        if _max_dd(stress) > DD_LIMIT:
            continue
        # Tambien DD primario completo ya exigido en TRAIN, pero se revalida.
        primary_all = _collect_window(recs, vid, "train", TRAIN_ALL, FEE_PRIMARY)
        if primary_all is None or _max_dd(primary_all) > DD_LIMIT:
            continue
        if bias_verdicts.get(str(vid)) is not True:
            continue
        out.append(vid)
    # Uno por perfil ya garantizado por picked; sin backfill fuera de tabla.
    risks = [_BY_ID[str(v)].get("risk_profile") for v in out]
    if len(set(risks)) != len(risks):
        raise ValueError("mas de uno por perfil")
    return sorted(out)


# --- Una para TEST: solo VAL 2023-24 primario, mismos desempates ---

def _validation_eligible(records, variant_id):
    sel = _collect_window(records, variant_id, "validation", VAL_YEARS, FEE_PRIMARY)
    if sel is None:
        return None
    if _sum_trades(sel) < 30:
        return None
    if any(not float(r["g"]) > 0.0 for r in sel):
        return None
    if not _median_excess(sel) > 0.0:
        return None
    stress = _collect_window(records, variant_id, "validation", VAL_YEARS, FEE_STRESS)
    if stress is None:
        return None
    if _max_dd(sel) > DD_LIMIT:
        return None
    if _max_dd(stress) > DD_LIMIT:
        return None
    weighted = _weighted_g(sel)
    return {
        "weighted": weighted,
        "maxdd": _max_dd(sel),
        "turnover": _sum_turnover(sel),
    }


def choose_test_candidate(validation_records, validation_ids):
    recs = _validate_all(validation_records)
    if not isinstance(validation_ids, (list, tuple)):
        raise ValueError("validation_ids debe ser lista")
    vids = list(validation_ids)
    if len(vids) != len(set(str(x) for x in vids)):
        raise ValueError("validation_ids duplicados")
    if len(vids) > 3 or len(vids) == 0:
        raise ValueError("validation_ids debe tener 1..3")
    for vid in vids:
        if not isinstance(vid, str) or str(vid) not in _KNOWN_IDS:
            raise ValueError(f"validation_id desconocida: {vid!r}")
    best = None
    best_key = None
    for vid in vids:
        info = _validation_eligible(recs, str(vid))
        if info is None:
            continue
        key = _rank_key(vid, info["weighted"], info["maxdd"], info["turnover"])
        if best_key is None or key < best_key:
            best_key = key
            best = str(vid)
    return best


# --- Dictamen TEST: PASS/FAIL/INCONCLUSIVE sobre la elegida ---

def test_verdict(records, candidate_id):
    recs = _validate_all(records)
    if not isinstance(candidate_id, str) or str(candidate_id) not in _KNOWN_IDS:
        raise ValueError(f"candidate_id desconocida: {candidate_id!r}")
    vid = str(candidate_id)
    reasons = []
    # Recolecta TEST por fee; ausencia/invalidez no se intersecta en silencio.
    primary = {}
    stress = {}
    missing = False
    for year in TEST_YEARS:
        prec = _lookup(recs, vid, "test", year, FEE_PRIMARY)
        srec = _lookup(recs, vid, "test", year, FEE_STRESS)
        if prec is None or srec is None:
            missing = True
            reasons.append(f"ano requerido ausente: {year}")
            continue
        if prec.get("valid") is not True or srec.get("valid") is not True:
            missing = True
            reasons.append(f"registro invalido en {year}")
            continue
        primary[int(year)] = prec
        stress[int(year)] = srec
    # Fallos verificables dominan sobre INCONCLUSIVE (sin fingir PASS).
    fail_reasons = []
    # DD en cualquier registro presente y valido (ambos fees).
    for year, rec in list(primary.items()) + list(stress.items()):
        if float(rec["max_drawdown_pct"]) > DD_LIMIT:
            fail_reasons.append(f"DD supero 15% en {year} fee {rec['fee']}")
    # g por ano primario: cero no es positivo, no se elimina.
    for year in sorted(primary):
        if not float(primary[year]["g"]) > 0.0:
            fail_reasons.append(f"g no positivo en {year} a 0.2%")
    # Mediana de exceso solo con ambos anos; sin parcial.
    if len(primary) == len(TEST_YEARS):
        items = [primary[y] for y in sorted(primary)]
        if not _median_excess(items) > 0.0:
            fail_reasons.append("mediana de exceso frente BH no positiva en TEST")
        # Media ponderada primaria y conteo total.
        if not _weighted_g(items) > 0.0:
            fail_reasons.append("media ponderada TEST no positiva a 0.2%")
        if _sum_trades(items) < 30 and not fail_reasons:
            # Pocos trades sin fallo explicito: INCONCLUSIVE, no candidata apta.
            pass
        if not _weighted_forced(items) >= 0.0:
            fail_reasons.append("depende solo de forzados positivos en TEST")
    else:
        # Sin ambos anos no hay mediana parcial valida.
        pass
    # Stress: ponderada >=0 y DD ya cubierto arriba.
    if len(stress) == len(TEST_YEARS):
        sitems = [stress[y] for y in sorted(stress)]
        if not _weighted_g(sitems) >= 0.0:
            fail_reasons.append("ponderada TEST no >=0 a 0.3%")
        if not _weighted_forced([primary[y] for y in sorted(primary)] if len(primary) == len(TEST_YEARS) else sitems) >= 0.0:
            # Forzado primario ya evaluado arriba cuando completo; aqui solo
            # conserva el gate si hay datos suficientes.
            pass
    if fail_reasons:
        return {"verdict": "FAIL", "reasons": fail_reasons + ([r for r in reasons if r not in fail_reasons])}
    if missing:
        if not reasons:
            reasons.append("cobertura TEST incompleta")
        return {"verdict": "INCONCLUSIVE", "reasons": reasons}
    # Completos: exige trades totales y resto de gates.
    items = [primary[y] for y in sorted(primary)]
    if _sum_trades(items) < 30:
        return {"verdict": "INCONCLUSIVE", "reasons": ["pocos trades en TEST (<30)"]}
    # Si llego aqui sin fallos y con cobertura completa, es PASS.
    return {"verdict": "PASS", "reasons": []}
