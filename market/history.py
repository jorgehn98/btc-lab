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
