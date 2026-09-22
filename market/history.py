"""market.history: particiones cerradas TRAIN/VALIDATION/TEST (solo stdlib).

Intervalos semiabiertos UTC (PRD aprobado):

- train:      [2017-08-17 04:00, 2023-01-01)
- validation: [2023-01-01, 2025-01-01)
- test:       [2025-01-01, 2026-09-22 00:00)

No hay fechas configurables ni flags de bypass. La autorizacion pura valida
forma (conteos, duplicados, extras) pero NO sustituye la autorizacion
persistente de runtime: VAL/TEST exigen artefacto de fase verificable
(protocolo PR02). En PR01 el runner solo abre TRAIN; roles externos quedan
fail-closed hasta que ese protocolo exista.
"""

from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc

_TRAIN_START = datetime(2017, 8, 17, 4, 0, tzinfo=UTC)
_TRAIN_END = datetime(2023, 1, 1, tzinfo=UTC)
_VAL_START = datetime(2023, 1, 1, tzinfo=UTC)
_VAL_END = datetime(2025, 1, 1, tzinfo=UTC)
_TEST_START = datetime(2025, 1, 1, tzinfo=UTC)
_TEST_END = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)

_BOUNDS = {
    "train": (_TRAIN_START, _TRAIN_END),
    "validation": (_VAL_START, _VAL_END),
    "test": (_TEST_START, _TEST_END),
}


def partition_bounds(role):
    """Devuelve (start, end) UTC semiabierto para un rol cerrado."""
    if not isinstance(role, str):
        raise TypeError(f"rol debe ser str, no {type(role).__name__}")
    try:
        start, end = _BOUNDS[role]
    except KeyError:
        raise ValueError(f"rol desconocido: {role!r}") from None
    return (start, end)


def _reject_extra(selection, allowed: set, role: str) -> None:
    extra = set(selection) - allowed
    if extra:
        raise ValueError(f"{role}: claves no permitidas: {sorted(extra)}")


def authorize_partition(role, selection):
    """Autoriza forma pura por rol; sin artefacto persistente (ver modulo)."""
    if not isinstance(role, str):
        raise TypeError(f"rol debe ser str, no {type(role).__name__}")
    if role not in _BOUNDS:
        raise ValueError(f"rol desconocido: {role!r}")
    start, end = _BOUNDS[role]

    if role == "train":
        if selection is None:
            selection = {}
        if not isinstance(selection, dict):
            raise ValueError("train: seleccion debe ser None o {}")
        if selection != {}:
            raise ValueError(f"train: no admite holdouts ni fechas: {sorted(selection)}")
        return {"role": role, "start": start, "end": end}

    if role == "validation":
        if not isinstance(selection, dict):
            raise ValueError("validation: seleccion debe ser dict con finalists")
        _reject_extra(selection, {"finalists"}, "validation")
        if "finalists" not in selection:
            raise ValueError("validation: falta finalists")
        finalists = selection["finalists"]
        if not isinstance(finalists, list):
            raise ValueError("validation: finalists debe ser lista")
        if not 1 <= len(finalists) <= 3:
            raise ValueError("validation: 1-3 finalistas, sin mas ni menos")
        for name in finalists:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"validation: finalista invalido: {name!r}")
        if len(set(finalists)) != len(finalists):
            raise ValueError("validation: finalistas duplicados")
        return {
            "role": role,
            "start": start,
            "end": end,
            "finalists": list(finalists),
        }

    # role == "test": consumo unico, sin bypass.
    if not isinstance(selection, dict):
        raise ValueError("test: seleccion debe ser dict con candidate+consumed")
    _reject_extra(selection, {"candidate", "consumed"}, "test")
    if "candidate" not in selection or "consumed" not in selection:
        raise ValueError("test: exige candidate y consumed")
    candidate = selection["candidate"]
    consumed = selection["consumed"]
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"test: candidate invalida: {candidate!r}")
    if not isinstance(consumed, bool):
        raise ValueError("test: consumed debe ser bool")
    if consumed is not False:
        raise ValueError("test: TEST consumido no se reutiliza")
    return {
        "role": role,
        "start": start,
        "end": end,
        "candidate": candidate,
        "consumed": False,
    }


def verify_phase_grant(role, grant, definition_hash, expected_ids) -> dict:
    """Puente PR02: verifica grant inmutable de holdout (forma, no autoridad).

    La autoridad real vive en operations.search (state + reports verificados
    + definition hash + candidatos esperados) y en operations.history
    (verify_holdout_grant). Este helper puro solo valida forma cerrada para
    que un JSON manual no abra holdout: sin campo `force`, con grant_id,
    campaign_id, definition_hash y candidatos con formato V000..V071.
    No cambia authorize_partition (PR01 intacto).
    """
    if role not in ("validation", "test"):
        raise ValueError(f"grant solo para validation/test, no {role!r}")
    if not isinstance(grant, dict):
        raise ValueError("grant debe ser dict")
    if "force" in grant:
        raise ValueError("campo 'force' prohibido en grant")
    for key in ("campaign_id", "grant_id", "definition_hash"):
        value = grant.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"grant sin {key} valido")
    if str(grant.get("definition_hash")) != str(definition_hash):
        raise ValueError("grant con definition_hash distinto (inmutable)")
    if role == "validation":
        allowed = {"campaign_id", "grant_id", "definition_hash", "phase",
                   "validation_ids", "train_report_sha256"}
        extra = set(grant) - allowed
        if extra:
            raise ValueError(f"validation grant con claves no permitidas: {sorted(extra)}")
        if grant.get("phase") != "validation":
            raise ValueError("grant phase debe ser validation")
        ids = grant.get("validation_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 3:
            raise ValueError("validation grant exige 1..3 validation_ids")
        for vid in ids:
            if not isinstance(vid, str) or len(vid) != 4 or not vid.startswith("V"):
                raise ValueError(f"validation_id con formato invalido: {vid!r}")
            try:
                num = int(vid[1:])
            except ValueError:
                raise ValueError(f"validation_id invalida: {vid!r}") from None
            if not 0 <= num <= 71:
                raise ValueError(f"validation_id fuera de V000..V071: {vid!r}")
        if set(str(v) for v in ids) != set(str(v) for v in (expected_ids or [])):
            raise ValueError("validation grant fuera de candidatos esperados")
        report = grant.get("train_report_sha256")
        if not isinstance(report, str) or not report:
            raise ValueError("grant sin train_report_sha256")
        return {"role": role, "validation_ids": list(ids),
                "grant_id": str(grant.get("grant_id")),
                "definition_hash": str(definition_hash)}
    allowed = {"campaign_id", "grant_id", "definition_hash", "phase",
               "candidate_id", "validation_report_sha256"}
    extra = set(grant) - allowed
    if extra:
        raise ValueError(f"test grant con claves no permitidas: {sorted(extra)}")
    if grant.get("phase") != "test":
        raise ValueError("grant phase debe ser test")
    vid = grant.get("candidate_id")
    if not isinstance(vid, str) or len(vid) != 4 or not vid.startswith("V"):
        raise ValueError(f"candidate_id con formato invalido: {vid!r}")
    try:
        num = int(str(vid)[1:])
    except ValueError:
        raise ValueError(f"candidate_id invalida: {vid!r}") from None
    if not 0 <= num <= 71:
        raise ValueError(f"candidate_id fuera de V000..V071: {vid!r}")
    if list(expected_ids or []) != [str(vid)]:
        raise ValueError("test grant fuera de candidata esperada")
    report = grant.get("validation_report_sha256")
    if not isinstance(report, str) or not report:
        raise ValueError("grant sin validation_report_sha256")
    return {"role": role, "candidate_id": str(vid),
            "grant_id": str(grant.get("grant_id")),
            "definition_hash": str(definition_hash)}
