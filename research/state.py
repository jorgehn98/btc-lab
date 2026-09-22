"""research.state: estado puro de campana (solo stdlib, sin framework).

Dict simple mutable; presupuesto 12h acumulado con fallidos; congelados
no redefinibles; TEST de un solo uso con grant inmutable.
"""

from __future__ import annotations

import copy
import math
import uuid

BUDGET_SECONDS = 12 * 3600

_STAGES = ("REGISTERED", "TRAIN_FROZEN", "VALIDATION_FROZEN", "TEST_RESERVED")

_KNOWN_IDS_CACHE = None


def _known_ids():
    global _KNOWN_IDS_CACHE
    if _KNOWN_IDS_CACHE is None:
        from research.campaign import generate_variants

        _KNOWN_IDS_CACHE = {str(v.get("id")) for v in generate_variants()}
    return _KNOWN_IDS_CACHE


def _require_nonempty_str(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} debe ser str no vacia")
    return value


def new_state(campaign_id, definition_hash):
    _require_nonempty_str(campaign_id, "campaign_id")
    _require_nonempty_str(definition_hash, "definition_hash")
    return {
        "campaign_id": str(campaign_id),
        "definition_hash": str(definition_hash),
        "budget_limit": int(BUDGET_SECONDS),
        "consumed": 0,
        "stage": "REGISTERED",
        "native_runs": {},
        "attempts": [],
        "train_ids": None,
        "validation_ids": None,
        "test_candidate": None,
        "test_grant_id": None,
        "test_grant": None,
        "test_consumed": False,
    }


def _validate_attempt_args(state, run_key, elapsed_seconds, status, evidence_hash):
    if not isinstance(state, dict):
        raise ValueError("state debe ser dict")
    _require_nonempty_str(run_key, "run_key")
    if type(elapsed_seconds) is bool or not isinstance(elapsed_seconds, (int, float)):
        raise ValueError("elapsed_seconds debe ser numerico no bool")
    if not math.isfinite(float(elapsed_seconds)) or float(elapsed_seconds) < 0.0:
        raise ValueError("elapsed_seconds debe ser finito >=0")
    if status not in ("SUCCEEDED", "FAILED"):
        raise ValueError(f"status ilegible: {status!r}")
    _require_nonempty_str(evidence_hash, "evidence_hash")
    for key in ("budget_limit", "consumed", "native_runs", "attempts"):
        if key not in state:
            raise ValueError(f"state sin {key}")


def record_attempt(state, run_key, elapsed_seconds, status, evidence_hash):
    _validate_attempt_args(state, run_key, elapsed_seconds, status, evidence_hash)
    elapsed = float(elapsed_seconds)
    entry = {
        "run_key": str(run_key),
        "elapsed_seconds": float(elapsed),
        "status": str(status),
        "evidence_hash": str(evidence_hash),
    }
    # Registra consumo e historial antes de decidir tope: no se descarta
    # tiempo gastado; el llamante persiste aunque se supere el presupuesto.
    if not isinstance(state.get("native_runs"), dict):
        raise ValueError("native_runs debe ser dict")
    if not isinstance(state.get("attempts"), list):
        raise ValueError("attempts debe ser lista")
    state["attempts"].append(dict(entry))
    state["native_runs"][str(run_key)] = dict(entry)
    try:
        consumed = float(state.get("consumed", 0))
    except (TypeError, ValueError):
        raise ValueError("consumed ilegible")
    new_consumed = consumed + elapsed
    # Conserva int cuando es entero para comparacion exacta en tests.
    state["consumed"] = int(new_consumed) if float(new_consumed).is_integer() else float(new_consumed)
    try:
        limit = float(state.get("budget_limit", BUDGET_SECONDS))
    except (TypeError, ValueError):
        raise ValueError("budget_limit ilegible")
    if new_consumed > limit:
        raise ValueError("presupuesto 12h agotado")
    return None


def can_reuse_run(state, key, evidence_hash):
    if not isinstance(state, dict):
        raise ValueError("state debe ser dict")
    runs = state.get("native_runs")
    if not isinstance(runs, dict):
        return False
    if not isinstance(key, str) or not key:
        return False
    if not isinstance(evidence_hash, str) or not evidence_hash:
        return False
    entry = runs.get(str(key))
    if not isinstance(entry, dict):
        return False
    if entry.get("status") != "SUCCEEDED":
        return False
    return entry.get("evidence_hash") == str(evidence_hash)


def _validate_id_list(ids, lo, hi, label):
    if not isinstance(ids, (list, tuple)):
        raise ValueError(f"{label} debe ser lista")
    out = list(ids)
    if not (lo <= len(out) <= hi):
        raise ValueError(f"{label} debe tener {lo}..{hi}, fue {len(out)}")
    seen = set()
    for vid in out:
        if not isinstance(vid, str) or not vid:
            raise ValueError(f"{label} con ID ilegible: {vid!r}")
        if str(vid) not in _known_ids():
            raise ValueError(f"{label} con ID desconocida: {vid!r}")
        if str(vid) in seen:
            raise ValueError(f"{label} duplicada: {vid!r}")
        seen.add(str(vid))
    return [str(v) for v in out]


def freeze_train_selection(state, ids):
    if not isinstance(state, dict):
        raise ValueError("state debe ser dict")
    if state.get("train_ids") is not None:
        raise ValueError("seleccion TRAIN ya congelada")
    clean = _validate_id_list(ids, 1, 9, "train_ids")
    state["train_ids"] = sorted(clean)
    state["stage"] = "TRAIN_FROZEN"
    return None


def freeze_validation_selection(state, ids):
    if not isinstance(state, dict):
        raise ValueError("state debe ser dict")
    if state.get("train_ids") is None:
        raise ValueError("TRAIN debe congelarse antes que VALIDATION")
    if state.get("validation_ids") is not None:
        raise ValueError("seleccion VALIDATION ya congelada")
    clean = _validate_id_list(ids, 1, 3, "validation_ids")
    train = set(str(x) for x in (state.get("train_ids") or []))
    for vid in clean:
        if str(vid) not in train:
            raise ValueError(f"validation_id fuera de tabla TRAIN: {vid!r}")
    state["validation_ids"] = sorted(clean)
    state["stage"] = "VALIDATION_FROZEN"
    return None


def reserve_test(state, candidate_id):
    if not isinstance(state, dict):
        raise ValueError("state debe ser dict")
    _require_nonempty_str(candidate_id, "candidate_id")
    vid = str(candidate_id)
    if vid not in _known_ids():
        raise ValueError(f"candidata desconocida: {vid!r}")
    if state.get("train_ids") is None or state.get("validation_ids") is None:
        raise ValueError("TEST exige TRAIN y VALIDATION congelados")
    valid = set(str(x) for x in (state.get("validation_ids") or []))
    if vid not in valid:
        raise ValueError("candidata fuera de VALIDATION congelada")
    if state.get("test_consumed") is True or state.get("stage") == "TEST_RESERVED":
        raise ValueError("TEST ya consumido")
    if state.get("test_candidate") is not None or state.get("test_grant_id") is not None:
        raise ValueError("TEST ya consumido")
    grant_id = uuid.uuid4().hex
    grant = {
        "campaign_id": str(state.get("campaign_id")),
        "candidate_id": vid,
        "grant_id": str(grant_id),
        "definition_hash": str(state.get("definition_hash")),
    }
    # Consumo marcado sincrono antes de cualquier lectura posterior.
    state["test_candidate"] = vid
    state["test_grant_id"] = str(grant_id)
    state["test_grant"] = copy.deepcopy(grant)
    state["test_consumed"] = True
    state["stage"] = "TEST_RESERVED"
    return copy.deepcopy(grant)


def authorize_test_resume(state, candidate_id, grant_id, definition_hash):
    if not isinstance(state, dict):
        return False
    if not isinstance(candidate_id, str) or not candidate_id:
        return False
    if not isinstance(grant_id, str) or not grant_id:
        return False
    if not isinstance(definition_hash, str) or not definition_hash:
        return False
    if state.get("test_consumed") is not True:
        return False
    if str(state.get("test_candidate")) != str(candidate_id):
        return False
    if str(state.get("test_grant_id")) != str(grant_id):
        return False
    if str(state.get("definition_hash")) != str(definition_hash):
        return False
    return True
