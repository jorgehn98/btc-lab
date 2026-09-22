"""Runner cerrado de historico por roles para la campana (PR01: solo TRAIN).

Alcance PR01:
- `prepare` (host): Git limpio + imagen fijada + hashes; solo rol train.
  VAL/TEST quedan fail-closed hasta el protocolo PR02.
- `download` / `snapshot` / `ledger` (contenedor, sin Git): codigo e input
  en solo lectura, datos por rol, snapshots verificados por hash.
- `phase-contract`: imprime el contrato exacto que exigira PR02 para abrir
  VALIDATION/TEST (artefacto de fase verificable, nunca flag libre).

Limite explicito: este modulo NO abre VAL/TEST aunque `market.history`
valide su forma pura. Cualquier intento con rol externo falla con el
contrato de `phase-contract`. El coordinador solo descarga TRAIN en PR01.

Reutiliza market.train (validate/agregacion/segmentos), market.equity
(ledger/reconciliacion) y los helpers probados de operations/research
(streaming con fix wait/fsync, contencion de rutas) con su hash fijado
en el input; lock propio (root control de history, no el global de
research). Legado baseline/research detenido y preservado sin editar.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import timedelta, timezone
from pathlib import Path

from operations import launch
from operations.research import (
    _contained,
    _run_streaming,
    _safe_basename,
    _within_root,
)

PAIR = "BTC/USDT"
TIMEFRAME_5M = "5m"
TIMEFRAME_1H = "1h"
EXCHANGE = "binance"
WARMUP_BARS = 201
MIN_SEGMENT_BARS = WARMUP_BARS + 1
CONFIG_REL = Path("configs/baseline.json")
HISTORY_REL = Path("operations/history.py")
RESEARCH_REL = Path("operations/research.py")
EQUITY_REL = Path("market/equity.py")
PARTITIONS_REL = Path("market/history.py")
TRAIN_HELPER_REL = Path("market/train.py")

CONTAINER_CODE = "/opt/btc-lab"
CONTAINER_HISTORY = "/lab-history"
CONTAINER_INPUT = "/lab-history/input.json"
CONTAINER_CONFIG = "/opt/btc-lab/configs/baseline.json"

INPUT_KIND = "btc-lab-history-input"
DOWNLOAD_KIND = "btc-lab-history-download"
SNAPSHOT_KIND = "btc-lab-history-snapshot"
LEDGER_KIND = "btc-lab-history-ledger"

DOWNLOAD_TIMEOUT_S = 1800


def _utcnow_iso() -> str:
    from datetime import datetime

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _role_bounds(role: str):
    from market.history import partition_bounds

    return partition_bounds(role)


def _timerange_exact(role: str) -> str:
    """Rango exacto en minutos (TRAIN: 20170817T0400-20230101T0000)."""
    start, end = _role_bounds(role)
    return f"{start.strftime('%Y%m%dT%H%M')}-{end.strftime('%Y%m%dT%H%M')}"


def _require_train_only(role: str) -> None:
    if role != "train":
        raise ValueError(
            "rol externo fail-closed en PR01: "
            f"{role!r} exige protocolo PR02 ({phase_artifact_contract()['required']}). "
            "Ver `phase-contract`. Nunca flag JSON libre para abrir TEST."
        )


def phase_artifact_contract() -> dict:
    """Contrato futuro PR02 (interfaz estable; este PR solo lo documenta)."""
    return {
        "train": "sin artefacto: prepare/download/snapshot/ledger directos",
        "validation": (
            "requiere control/finalists.json congelado (1-3 ids, hashes de "
            "codigo+snapshot TRAIN, firma de fase TRAIN) verificado por hash "
            "antes de montar snapshots VALIDATION"
        ),
        "test": (
            "requiere control/test-decision.json (1 candidata elegida solo con "
            "g(2023-2024) a 0.2%, consumo unico registrado) verificado por hash "
            "antes de montar snapshots TEST; no reutilizar TEST consumido"
        ),
        "required": "artefacto de fase verificable por hash",
        "forbidden": "flags libres (force/allow_test/bypass/fechas) para abrir TEST",
    }


def verify_holdout_grant(role, grant, definition_hash, expected_ids,
                         report_sha256) -> dict:
    """Puente PR02: autoriza holdout solo con grant de operations.search.

    No abre VAL/TEST por CLI manual: el llamante debe ser operations.search
    tras verificar state canonico + report seleccionado por SHA + definition
    hash + candidatos esperados. Delega la forma a
    market.history.verify_phase_grant y exige el hash del report correcto.
    Sin campo `force`; metadata con hash refs inmutable. El prepare publico
    con rol externo sin este grant sigue fallando (PR01 intacto).
    """
    if role not in ("validation", "test"):
        raise ValueError(f"holdout solo validation/test, no {role!r}")
    if not isinstance(grant, dict):
        raise ValueError("grant debe ser dict")
    if "force" in grant:
        raise ValueError("campo 'force' prohibido en grant")
    from market.history import verify_phase_grant as _verify_form

    checked = _verify_form(role, grant, definition_hash, expected_ids)
    # El report que autoriza el holdout debe coincidir con el verificado.
    key = "train_report_sha256" if role == "validation" else "validation_report_sha256"
    if str(grant.get(key) or "") != str(report_sha256 or ""):
        raise ValueError(f"grant sin {key} verificado por hash")
    if not report_sha256 or not isinstance(report_sha256, str):
        raise ValueError("report_sha256 inmutable requerido")
    return checked


def _code_hashes(code_root: Path) -> dict:
    code = Path(code_root)
    return {
        "config_hash": launch.file_hash(code / CONFIG_REL),
        "history_hash": launch.file_hash(code / HISTORY_REL),
        "research_hash": launch.file_hash(code / RESEARCH_REL),
        "equity_hash": launch.file_hash(code / EQUITY_REL),
        "partitions_hash": launch.file_hash(code / PARTITIONS_REL),
        "train_helper_hash": launch.file_hash(code / TRAIN_HELPER_REL),
        "launch_hash": launch.file_hash(code / "operations/launch.py"),
        "health_hash": launch.file_hash(code / "operations/health.py"),
    }


def _slug() -> str:
    return f"{_utcnow_iso().replace(':', '').replace('+', '')}-{uuid.uuid4().hex[:8]}"


def _pair_file(pair: str, timeframe: str) -> str:
    cleaned = pair
    for ch in ("/", " ", ".", "@", "$", "+", ":"):
        cleaned = cleaned.replace(ch, "_")
    return f"{cleaned}-{timeframe.replace('M', 'Mo')}.feather"


def _check_input_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("input ilegible: no es un objeto")
    if manifest.get("kind") != INPUT_KIND:
        raise ValueError("input con kind inesperado")
    if manifest.get("status") != "PREPARED":
        raise ValueError("input no esta PREPARED")
    if manifest.get("image_ref") != launch.PINNED_IMAGE:
        raise ValueError("input sin imagen fijada")
    if not launch._COMMIT_RE.fullmatch(str(manifest.get("commit") or "")):
        raise ValueError("input sin commit valido")
    if not manifest.get("input_id"):
        raise ValueError("input sin input_id")
    if "force" in manifest:
        raise ValueError("campo 'force' prohibido en input")
    role = str(manifest.get("role") or "")
    if role == "train":
        start, end = _role_bounds("train")
        if manifest.get("range_start") != start.isoformat():
            raise ValueError("input etiqueta range_start distinto a constantes del rol")
        if manifest.get("range_end") != end.isoformat():
            raise ValueError("input etiqueta range_end distinto a constantes del rol")
        return
    # Roles externos (PR02): solo con grant verificable; sin flags manuales.
    # El prepare publico con rol externo sigue fallando (ver prepare_history):
    # estos inputs solo los acuña prepare_history_with_grant tras verificar
    # el estado canonico + report de seleccion + definition hash.
    if role not in ("validation", "test"):
        raise ValueError(f"input con rol inesperado: {role!r}")
    grant = manifest.get("grant")
    if not isinstance(grant, dict):
        raise ValueError(f"input {role} sin grant verificable")
    expected_ids = manifest.get("expected_ids")
    if not isinstance(expected_ids, list) or not expected_ids:
        raise ValueError(f"input {role} sin expected_ids")
    report_sha = manifest.get("report_sha256")
    if not isinstance(report_sha, str) or not report_sha:
        raise ValueError(f"input {role} sin report_sha256")
    definition_hash = manifest.get("definition_hash")
    if not isinstance(definition_hash, str) or not definition_hash:
        raise ValueError(f"input {role} sin definition_hash")
    verify_holdout_grant(role, grant, definition_hash, expected_ids, report_sha)
    if list(str(v) for v in expected_ids) != _grant_ids(role, grant):
        raise ValueError(f"input {role}: expected_ids difiere del grant")
    start, end = _role_bounds(role)
    if manifest.get("range_start") != start.isoformat():
        raise ValueError("input etiqueta range_start distinto a constantes del rol")
    if manifest.get("range_end") != end.isoformat():
        raise ValueError("input etiqueta range_end distinto a constantes del rol")


def _grant_ids(role: str, grant: dict) -> list:
    if role == "validation":
        return [str(v) for v in (grant.get("validation_ids") or [])]
    return [str(grant.get("candidate_id"))]


def _require_authorized_role(manifest_in: dict) -> str:
    """Rol del input con autorizacion verificada (train directo, resto grant)."""
    role = str(manifest_in.get("role") or "train")
    if role == "train":
        _require_train_only(role)
        return role
    if role not in ("validation", "test"):
        raise ValueError(f"rol inesperado: {role!r}")
    grant = manifest_in.get("grant")
    if not isinstance(grant, dict):
        raise ValueError(f"{role}: sin grant verificable (ver phase-contract)")
    verify_holdout_grant(
        role, grant,
        str(manifest_in.get("definition_hash") or ""),
        list(manifest_in.get("expected_ids") or []),
        str(manifest_in.get("report_sha256") or ""),
    )
    return role


def _load_input() -> dict:
    path = Path(CONTAINER_INPUT)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"input ilegible: {exc}") from exc
    _check_input_manifest(manifest)
    return manifest


def _verify_current_against_input(manifest_in: dict) -> dict:
    code = Path(CONTAINER_CODE)
    try:
        config = json.loads((code / CONFIG_REL).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config ilegible: {exc}") from exc
    launch.validate_baseline_config(config, os.environ)
    current = _code_hashes(code)
    for key, value in current.items():
        if not manifest_in.get(key) or manifest_in.get(key) != value:
            raise ValueError(f"codigo alterado tras prepare ({key})")
    return current


def _locked():
    import fcntl

    control = Path(CONTAINER_HISTORY) / "control"
    control.mkdir(parents=True, exist_ok=True)
    lock_path = control / "history.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)

    class _Guard:
        def __enter__(self):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise RuntimeError("otro job history en curso (lock ocupado)")
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return False

    return _Guard()


def prepare_history(code_root, storage_root, image_ref, role: str = "train") -> str:
    """Preflight HOST (solo train en PR01); devuelve manifiesto unico."""
    if role != "train":
        raise ValueError(f"prepare PR01 solo train, no {role!r} (ver phase-contract)")
    if not isinstance(image_ref, str) or image_ref.strip() != launch.PINNED_IMAGE:
        raise ValueError("image_ref debe ser exactamente la imagen fijada con digest")
    code = Path(code_root)
    store = Path(storage_root)
    if not code.is_dir():
        raise FileNotFoundError(f"codigo no encontrado: {code}")
    try:
        config = json.loads((code / CONFIG_REL).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config baseline ilegible: {exc}") from exc
    launch.validate_baseline_config(config, os.environ)
    for rel in (HISTORY_REL, RESEARCH_REL, EQUITY_REL, PARTITIONS_REL,
                TRAIN_HELPER_REL, CONFIG_REL,
                Path("operations/launch.py"), Path("operations/health.py")):
        if not (code / rel).is_file():
            raise FileNotFoundError(f"fichero hasheado ausente: {rel}")
    start, end = _role_bounds("train")
    commit = launch._git_commit(code)
    launch._git_clean(code)
    image_id = launch._inspect_image(image_ref.strip())
    hashes = _code_hashes(code)
    manifest = {
        "kind": INPUT_KIND,
        "status": "PREPARED",
        "input_id": uuid.uuid4().hex,
        "created_at": _utcnow_iso(),
        "commit": commit,
        "image_ref": image_ref.strip(),
        "image_id": image_id,
        "image_digest": launch.PINNED_IMAGE,
        "role": "train",
        "pair": PAIR,
        "exchange": EXCHANGE,
        "timeframe": TIMEFRAME_1H,
        "timeframe_detail": TIMEFRAME_5M,
        "timerange": _timerange_exact("train"),
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "warmup_bars_1h": WARMUP_BARS,
        **hashes,
    }
    out = store / "history" / "inputs" / f"history-input-{_slug()}.json"
    launch._atomic_create_new(out, manifest)
    return str(out)


def prepare_history_with_grant(code_root, storage_root, image_ref, role: str,
                               grant: dict, expected_ids, report_sha256: str,
                               definition_hash: str) -> str:
    """Preflight HOST para roles externos con grant verificado (PR02).

    Solo la invoca operations.search prepare-phase tras verificar estado
    canonico + report de seleccion por SHA + definition hash + candidatos
    esperados. El prepare publico con rol externo sin grant sigue fallando.
    No acepta flags force/IDs manuales: todo viene del grant verificado.
    """
    if role not in ("validation", "test"):
        raise ValueError(f"grant solo para validation/test, no {role!r}")
    if not isinstance(grant, dict) or "force" in grant:
        raise ValueError("grant invalido o con campo prohibido")
    if not isinstance(expected_ids, (list, tuple)) or not expected_ids:
        raise ValueError("expected_ids debe ser lista no vacia")
    if not isinstance(report_sha256, str) or not report_sha256:
        raise ValueError("report_sha256 inmutable requerido")
    if not isinstance(definition_hash, str) or not definition_hash:
        raise ValueError("definition_hash requerido")
    if not isinstance(image_ref, str) or image_ref.strip() != launch.PINNED_IMAGE:
        raise ValueError("image_ref debe ser exactamente la imagen fijada con digest")
    verify_holdout_grant(role, grant, definition_hash,
                         list(expected_ids), report_sha256)
    if list(str(v) for v in expected_ids) != _grant_ids(role, grant):
        raise ValueError(f"{role}: expected_ids difiere del grant verificado")
    code = Path(code_root)
    store = Path(storage_root)
    if not code.is_dir():
        raise FileNotFoundError(f"codigo no encontrado: {code}")
    try:
        config = json.loads((code / CONFIG_REL).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config baseline ilegible: {exc}") from exc
    launch.validate_baseline_config(config, os.environ)
    for rel in (HISTORY_REL, RESEARCH_REL, EQUITY_REL, PARTITIONS_REL,
                TRAIN_HELPER_REL, CONFIG_REL,
                Path("operations/launch.py"), Path("operations/health.py")):
        if not (code / rel).is_file():
            raise FileNotFoundError(f"fichero hasheado ausente: {rel}")
    start, end = _role_bounds(role)
    commit = launch._git_commit(code)
    launch._git_clean(code)
    image_id = launch._inspect_image(image_ref.strip())
    hashes = _code_hashes(code)
    manifest = {
        "kind": INPUT_KIND,
        "status": "PREPARED",
        "input_id": uuid.uuid4().hex,
        "created_at": _utcnow_iso(),
        "commit": commit,
        "image_ref": image_ref.strip(),
        "image_id": image_id,
        "image_digest": launch.PINNED_IMAGE,
        "role": role,
        "grant": dict(grant),
        "expected_ids": [str(v) for v in expected_ids],
        "report_sha256": str(report_sha256),
        "definition_hash": str(definition_hash),
        "pair": PAIR,
        "exchange": EXCHANGE,
        "timeframe": TIMEFRAME_1H,
        "timeframe_detail": TIMEFRAME_5M,
        "timerange": _timerange_exact(role),
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "warmup_bars_1h": WARMUP_BARS,
        **hashes,
    }
    out = store / "history" / "inputs" / f"history-input-{role}-{_slug()}.json"
    launch._atomic_create_new(out, manifest)
    return str(out)


def history_download_argv(config_path: str, datadir: str, role: str = "train") -> list:
    """Argv cerrado de descarga spot por rol (rango fijo por rol, sin flags)."""
    if role not in ("train", "validation", "test"):
        raise ValueError(f"rol desconocido: {role!r}")
    return [
        "freqtrade", "download-data",
        "--config", str(config_path),
        "--datadir", str(datadir),
        "--pairs", PAIR,
        "--timeframes", TIMEFRAME_5M,
        "--timerange", _timerange_exact(role),
        "--trading-mode", "spot",
        "--candle-types", "spot",
        "--exchange", EXCHANGE,
    ]


def cmd_download() -> int:
    try:
        manifest_in = _load_input()
        current = _verify_current_against_input(manifest_in)
    except Exception as exc:
        print(f"download: input/codigo no valido: {exc}", file=sys.stderr)
        return 2
    try:
        role = _require_authorized_role(manifest_in)
    except ValueError as exc:
        print(f"download: {exc}", file=sys.stderr)
        return 2
    history = Path(CONTAINER_HISTORY)
    try:
        with _locked():
            dl_id = uuid.uuid4().hex
            root = history / "downloads" / f"{role}-dl-{_slug()}-{dl_id[:4]}"
            data_dir = root / "data"
            user_dir = root / "user_data"
            data_dir.mkdir(parents=True, exist_ok=False)
            user_dir.mkdir(parents=True, exist_ok=False)
            manifest_path = history / "downloads" / f"{role}-dl-{_slug()}-{dl_id[:4]}.json"
            argv = history_download_argv(CONTAINER_CONFIG, str(data_dir), role)
            argv = argv + ["--userdir", str(user_dir)]
            start, end = _role_bounds(role)
            base = {
                "kind": DOWNLOAD_KIND,
                "status": "RUNNING",
                "download_id": dl_id,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "commit": manifest_in.get("commit"),
                "image_ref": manifest_in.get("image_ref"),
                "image_id": manifest_in.get("image_id"),
                "role": role,
                "pair": PAIR,
                "timeframe": TIMEFRAME_5M,
                "timerange": _timerange_exact(role),
                "range_start": start.isoformat(),
                "range_end": end.isoformat(),
                "datadir": str(data_dir),
                "userdir": str(user_dir),
                "argv": [str(a) for a in argv],
                **current,
            }
            launch._atomic_create_new(manifest_path, {**base, "exit_code": None})
            log_path = root / "download.log"
            try:
                returncode, timed_out = _run_streaming(argv, DOWNLOAD_TIMEOUT_S, log_path)
            except (FileNotFoundError, OSError) as exc:
                launch._atomic_replace(manifest_path, {**base, "status": "FAILED",
                                                        "finished_at": _utcnow_iso(),
                                                        "error": f"{type(exc).__name__}: {exc}"})
                print(f"download: FAILED {exc}", file=sys.stderr)
                return 1
            if timed_out or returncode is None:
                launch._atomic_replace(manifest_path, {**base, "status": "INCOMPLETE",
                                                        "finished_at": _utcnow_iso(),
                                                        "error": "timeout",
                                                        "logfile": str(log_path)})
                return 1
            status = "SUCCEEDED" if returncode == 0 else "FAILED"
            expected = data_dir / _pair_file(PAIR, TIMEFRAME_5M)
            file_hash = None
            if returncode == 0:
                try:
                    file_hash = launch.file_hash(expected)
                except OSError as exc:
                    status = "FAILED"
                    base["file_error"] = f"{type(exc).__name__}: {exc}"
            launch._atomic_replace(manifest_path, {**base, "status": status,
                                                    "exit_code": returncode,
                                                    "finished_at": _utcnow_iso(),
                                                    "logfile": str(log_path),
                                                    "data_file": str(expected),
                                                    "data_file_sha256": file_hash,
                                                    "download_dir": str(root)})
            print(str(manifest_path))
            return 0 if status == "SUCCEEDED" else 1
    except RuntimeError as exc:
        print(f"download: {exc}", file=sys.stderr)
        return 1


def _load_5m_frame(feather_paths: list):
    import pandas as pd

    frames = [pd.read_feather(str(p)) for p in feather_paths]
    if not frames:
        raise ValueError("sin ficheros 5m")
    return pd.concat(frames, ignore_index=True)


def cmd_snapshot(download_ref: str) -> int:
    try:
        manifest_in = _load_input()
        current = _verify_current_against_input(manifest_in)
    except Exception as exc:
        print(f"snapshot: input/codigo no valido: {exc}", file=sys.stderr)
        return 2
    try:
        base_name = _safe_basename(download_ref)
    except ValueError as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 2
    from market.train import aggregate_hourly, contiguous_segments, gaps_table, validate_ohlcv

    import pandas as pd

    history = Path(CONTAINER_HISTORY)
    downloads = history / "downloads"
    snapshots = history / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    try:
        dl_manifest_path = _contained(downloads, base_name, "download")
    except ValueError as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 2
    try:
        dl_manifest = json.loads(dl_manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"snapshot: download inexistente: {base_name}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"snapshot: download ilegible: {exc}", file=sys.stderr)
        return 2
    if dl_manifest.get("kind") != DOWNLOAD_KIND or dl_manifest.get("status") != "SUCCEEDED":
        print("snapshot: download no esta SUCCEEDED", file=sys.stderr)
        return 1
    try:
        role = _require_authorized_role(manifest_in)
    except ValueError as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 2
    if dl_manifest.get("role") != role or dl_manifest.get("pair") != PAIR:
        print("snapshot: download de otro rol/par", file=sys.stderr)
        return 1
    if dl_manifest.get("timerange") != _timerange_exact(role):
        print("snapshot: download de otro rango", file=sys.stderr)
        return 1
    try:
        data_dir = _within_root(downloads, Path(dl_manifest.get("datadir") or ""), "datadir")
    except ValueError:
        print("snapshot: datadir fuera de downloads", file=sys.stderr)
        return 1
    if not data_dir.is_dir():
        print("snapshot: datadir fuera de downloads", file=sys.stderr)
        return 1
    expected_5m = _contained(data_dir, _pair_file(PAIR, TIMEFRAME_5M), "5m")
    try:
        if not expected_5m.is_file():
            raise FileNotFoundError(f"falta {expected_5m.name} exacto Freqtrade")
        frozen_hash = launch.file_hash(expected_5m)
        if not dl_manifest.get("data_file_sha256") or frozen_hash != dl_manifest.get("data_file_sha256"):
            raise ValueError("feather 5m modificado tras download")
    except (OSError, ValueError) as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 1
    try:
        with _locked():
            snap_id = uuid.uuid4().hex
            manifest_path = snapshots / f"{role}-snap-{_slug()}-{snap_id[:4]}.json"
            snap_dir = snapshots / f"{role}-snap-{snap_id[:8]}"
            start, end = _role_bounds(role)
            base = {
                "kind": SNAPSHOT_KIND,
                "status": "RUNNING",
                "snapshot_id": snap_id,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "download_id": dl_manifest.get("download_id"),
                "download_manifest": str(dl_manifest_path),
                "commit": manifest_in.get("commit"),
                "image_ref": manifest_in.get("image_ref"),
                "role": role,
                "pair": PAIR,
                "timerange": _timerange_exact(role),
                "range_start": start.isoformat(),
                "range_end": end.isoformat(),
                "warmup_bars_1h": WARMUP_BARS,
                **current,
            }
            launch._atomic_create_new(manifest_path, {**base})
            try:
                if launch.file_hash(expected_5m) != dl_manifest.get("data_file_sha256"):
                    raise ValueError("feather 5m modificado tras download")
                frame_5m = _load_5m_frame([str(expected_5m)])
                loaded_hash = launch.file_hash(expected_5m)
                if loaded_hash != dl_manifest.get("data_file_sha256"):
                    raise ValueError("feather 5m modificado durante load")
                validate_ohlcv(frame_5m, start, end, 5)
                hourly = aggregate_hourly(frame_5m)
                segments = contiguous_segments(hourly)
                gaps = gaps_table(hourly)
                snap_dir.mkdir(parents=True, exist_ok=False)
                whole_5m = snap_dir / _pair_file(PAIR, TIMEFRAME_5M)
                whole_1h = snap_dir / _pair_file(PAIR, TIMEFRAME_1H)
                frame_5m.sort_values("date").reset_index(drop=True).to_feather(str(whole_5m))
                hourly.to_feather(str(whole_1h))
                seg_meta = []
                for idx, seg in enumerate(segments):
                    seg_start = pd.to_datetime(seg["date"].iloc[0], utc=True).to_pydatetime()
                    end_last = pd.to_datetime(seg["date"].iloc[-1], utc=True).to_pydatetime()
                    end_excl = end_last + timedelta(hours=1)
                    seg_sub = snap_dir / f"seg{idx:02d}"
                    seg_sub.mkdir(parents=True, exist_ok=False)
                    mask_5 = (pd.to_datetime(frame_5m["date"], utc=True) >= seg_start) & (
                        pd.to_datetime(frame_5m["date"], utc=True) < end_excl)
                    seg_5 = frame_5m.loc[mask_5].sort_values("date").reset_index(drop=True)
                    seg_1 = seg.sort_values("date").reset_index(drop=True)
                    seg_5_path = seg_sub / _pair_file(PAIR, TIMEFRAME_5M)
                    seg_1_path = seg_sub / _pair_file(PAIR, TIMEFRAME_1H)
                    seg_5.to_feather(str(seg_5_path))
                    seg_1.to_feather(str(seg_1_path))
                    # Contexto solo del pasado contiguo del mismo segmento.
                    eval_start = seg_start + timedelta(hours=WARMUP_BARS)
                    seg_meta.append({
                        "index": idx,
                        "length": int(len(seg)),
                        "start": seg_start.astimezone(timezone.utc).isoformat(),
                        "end_last": end_last.astimezone(timezone.utc).isoformat(),
                        "end_exclusive": end_excl.astimezone(timezone.utc).isoformat(),
                        "eval_start": eval_start.astimezone(timezone.utc).isoformat(),
                        "context": "context_only: 201h previas del mismo segmento, fuera de metricas",
                        "eligible": bool(len(seg) >= MIN_SEGMENT_BARS),
                        "seg_dir": f"seg{idx:02d}",
                        "file_5m_sha256": launch.file_hash(seg_5_path),
                        "file_1h_sha256": launch.file_hash(seg_1_path),
                    })
                payload = {
                    **base,
                    "status": "FROZEN",
                    "finished_at": _utcnow_iso(),
                    "rows_5m": int(len(frame_5m)),
                    "rows_1h": int(len(hourly)),
                    "segments": int(len(segments)),
                    "segments_meta": seg_meta,
                    "gaps": gaps,
                    "source_5m_sha256": loaded_hash,
                    "source_download_sha256": dl_manifest.get("data_file_sha256"),
                    "whole_5m_sha256": launch.file_hash(whole_5m),
                    "whole_1h_sha256": launch.file_hash(whole_1h),
                    "snapshot_dir": snap_dir.name,
                    "note": "1h de grupos 12x5m completos; warmup 201h mismo pasado contiguo",
                }
                launch._atomic_replace(manifest_path, payload)
                print(str(manifest_path))
                return 0
            except Exception as exc:
                launch._atomic_replace(manifest_path, {**base, "status": "FAILED",
                                                        "finished_at": _utcnow_iso(),
                                                        "error": f"{type(exc).__name__}: {exc}"})
                print(f"snapshot: FAILED {exc}", file=sys.stderr)
                return 1
    except RuntimeError as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 1


def _load_eval_snapshot(snapshot_ref: str, expected_role: str = "train") -> tuple:
    base_name = _safe_basename(snapshot_ref)
    history = Path(CONTAINER_HISTORY)
    snaps = history / "snapshots"
    manifest_path = _contained(snaps, base_name, "snapshot")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"snapshot ilegible: {exc}") from exc
    if manifest.get("kind") != SNAPSHOT_KIND:
        raise ValueError("snapshot kind inesperado")
    if manifest.get("status") != "FROZEN":
        raise ValueError("snapshot no esta FROZEN")
    if manifest.get("role") != expected_role:
        raise ValueError(f"snapshot de rol {manifest.get('role')!r}, se exige {expected_role!r}")
    snap_dir = _contained(snaps, str(manifest.get("snapshot_dir") or ""), "snapshot_dir")
    if not snap_dir.is_dir():
        raise ValueError("snapshot_dir ausente (evaluacion RO, sin reparar)")
    for label, rel, key in (
        ("5m total", _pair_file(PAIR, TIMEFRAME_5M), "whole_5m_sha256"),
        ("1h total", _pair_file(PAIR, TIMEFRAME_1H), "whole_1h_sha256"),
    ):
        target = _contained(snap_dir, rel, label)
        if not target.is_file() or launch.file_hash(target) != manifest.get(key):
            raise ValueError(f"dato inmutable alterado ({label})")
    for meta in manifest.get("segments_meta") or []:
        seg_dir = _contained(snap_dir, str(meta.get("seg_dir") or ""), "seg_dir")
        for rel, key in (
            (_pair_file(PAIR, TIMEFRAME_5M), "file_5m_sha256"),
            (_pair_file(PAIR, TIMEFRAME_1H), "file_1h_sha256"),
        ):
            target = _contained(seg_dir, rel, f"seg{meta.get('index')}")
            if not target.is_file() or launch.file_hash(target) != meta.get(key):
                raise ValueError(f"dato de segmento alterado (seg{meta.get('index')})")
    return manifest, snap_dir, manifest_path


def _load_trades_file(path: Path) -> tuple:
    """Acepta JSON con lista o {"trades": [...]} (+profit opcional)."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"trades ilegible: {exc}") from exc
    native_profit = None
    if isinstance(payload, list):
        trades = payload
    elif isinstance(payload, dict):
        trades = payload.get("trades", payload.get("trade_list"))
        for key in ("profit_total_abs", "profit_abs", "native_profit_abs"):
            if payload.get(key) is not None:
                try:
                    native_profit = float(payload[key])
                except (TypeError, ValueError):
                    raise ValueError(f"{key} no numerico") from None
                break
        if trades is None:
            raise ValueError("JSON sin lista trades/trade_list")
    else:
        raise ValueError("trades debe ser lista o objeto con trades")
    if not isinstance(trades, list):
        raise ValueError("trades debe ser lista")
    return trades, native_profit


def cmd_ledger(snapshot_ref: str, segment: int, trades_ref: str,
               initial: float = 10000.0, native_profit: float | None = None) -> int:
    """Ledger analitico sobre la ventana eval del segmento (sin backtest).

    Recorta el 5m del segmento a [eval_start, end) antes de construir; los
    trades se pasan enteros para que un open en warmup falle en vez de
    ignorarse. Crea intento RUNNING tras preflight/refs y lo finaliza a
    SUCCEEDED/FAILED; sin profit nativo no hay prueba nativa (reconcile None).
    """
    try:
        manifest_in = _load_input()
        _verify_current_against_input(manifest_in)
        role = _require_authorized_role(manifest_in)
    except Exception as exc:
        print(f"ledger: input/codigo no valido: {exc}", file=sys.stderr)
        return 2
    try:
        snap_manifest, snap_dir, snap_path = _load_eval_snapshot(snapshot_ref, role)
    except FileNotFoundError:
        print(f"ledger: snapshot inexistente: {snapshot_ref}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ledger: snapshot invalido: {exc}", file=sys.stderr)
        return 1
    import pandas as pd

    from market.equity import build_ledger, reconcile_final_ledger

    history = Path(CONTAINER_HISTORY)
    sessions = history / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    try:
        seg_idx = int(segment)
    except (TypeError, ValueError):
        print("ledger: segment debe ser entero", file=sys.stderr)
        return 2
    metas = {int(m.get("index")): m for m in (snap_manifest.get("segments_meta") or [])}
    if seg_idx not in metas:
        print(f"ledger: segmento inexistente: {segment}", file=sys.stderr)
        return 2
    meta = metas[seg_idx]
    # Trades solo en sandbox: nombre simple en sessions o absoluta contenida
    # en sessions/snapshot (nunca el input del perfil ni control).
    raw_ref = str(trades_ref or "")
    if not raw_ref:
        print("ledger: trades ausente", file=sys.stderr)
        return 2
    cand = Path(raw_ref)
    tpath: Path | None = None
    if cand.is_absolute():
        for root in (sessions, snap_dir):
            try:
                tpath = _within_root(root, cand, "trades")
                break
            except ValueError:
                continue
        if tpath is None:
            print("ledger: trades fuera de sessions/snapshot (contencion)", file=sys.stderr)
            return 2
    else:
        try:
            tpath = _contained(sessions, raw_ref, "trades")
        except ValueError as exc:
            print(f"ledger: {exc}", file=sys.stderr)
            return 2
    if not tpath.is_file():
        print(f"ledger: trades no encontrado: {trades_ref}", file=sys.stderr)
        return 2
    try:
        initial_f = float(initial)
    except (TypeError, ValueError):
        print("ledger: initial no numerico", file=sys.stderr)
        return 2
    attempt_path: Path | None = None
    attempt_base: dict | None = None
    try:
        with _locked():
            from datetime import datetime as _dt

            session_id = uuid.uuid4().hex
            manifest_path = sessions / f"ledger-{_slug()}-{session_id[:4]}.json"
            base = {
                "kind": LEDGER_KIND,
                "session_id": session_id,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "snapshot_id": snap_manifest.get("snapshot_id"),
                "snapshot_manifest": str(snap_path),
                "segment_meta": meta,
                "trades_ref": str(tpath),
            }
            launch._atomic_create_new(manifest_path, {**base, "status": "RUNNING"})
            attempt_path = manifest_path
            attempt_base = base

            def _fail(reason) -> int:
                launch._atomic_replace(manifest_path, {**base, "status": "FAILED",
                                                        "finished_at": _utcnow_iso(),
                                                        "error": str(reason)})
                print(f"ledger: FAILED {reason}", file=sys.stderr)
                print(str(manifest_path))
                return 1

            try:
                trades, file_profit = _load_trades_file(tpath)
            except (FileNotFoundError, ValueError) as exc:
                return _fail(exc)
            profit = native_profit if native_profit is not None else file_profit
            if profit is not None:
                try:
                    profit_f: float | None = float(profit)
                except (TypeError, ValueError) as exc:
                    return _fail(f"native profit no numerico: {exc}")
            else:
                profit_f = None
            seg_dir = snap_dir / str(meta.get("seg_dir"))
            seg_5_path = seg_dir / _pair_file(PAIR, TIMEFRAME_5M)
            try:
                if launch.file_hash(seg_5_path) != meta.get("file_5m_sha256"):
                    raise ValueError("segmento alterado (hash)")
                prices = pd.read_feather(str(seg_5_path))
            except (OSError, ValueError) as exc:
                return _fail(exc)
            try:
                eval_start = _dt.fromisoformat(str(meta["eval_start"]).replace("Z", "+00:00"))
                window_end = _dt.fromisoformat(str(meta["end_exclusive"]).replace("Z", "+00:00"))
            except (KeyError, ValueError, TypeError) as exc:
                return _fail(f"meta fechas eval invalidas: {exc}")
            if eval_start.tzinfo is None or window_end.tzinfo is None:
                return _fail("meta fechas eval sin zona UTC")
            # Ventana efectiva: recorta warmup del 5m; trades enteros para que
            # un open en warmup falle en el ledger en vez de ignorarse.
            try:
                stamps = pd.to_datetime(prices["date"], utc=True)
                mask = (stamps >= eval_start) & (stamps < window_end)
                eval_prices = prices.loc[mask].sort_values("date").reset_index(drop=True)
                if len(eval_prices) == 0:
                    raise ValueError("ventana eval sin velas tras recorte warmup")
                curve, summary = build_ledger(eval_prices, trades, initial_f,
                                              eval_start, window_end)
            except (ValueError, KeyError) as exc:
                return _fail(exc)
            entry: dict = {
                "segment": seg_idx,
                "trades": len(trades),
                "initial": initial_f,
                "summary": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                            for k, v in summary.items()},
            }
            if profit_f is not None:
                try:
                    rec = reconcile_final_ledger(summary, profit_f, initial_f)
                except (TypeError, ValueError) as exc:
                    return _fail(f"reconcile invalido: {exc}")
                entry["reconcile"] = rec
                entry["evaluation_status"] = "SUCCEEDED" if rec["ok"] else "FAILED"
            else:
                # Sin prueba nativa el analisis no equivale a resultado nativo:
                # campo separado None, sin PnL/gate inventados (PR02 exige proof).
                entry["reconcile"] = None
                entry["note"] = ("analisis analitico sin profit nativo: no equivale "
                                 "a resultado nativo ni gate")
                entry["evaluation_status"] = "SUCCEEDED"
            status = "SUCCEEDED" if entry["evaluation_status"] == "SUCCEEDED" else "FAILED"
            launch._atomic_replace(manifest_path, {**base, "status": status,
                                                    "finished_at": _utcnow_iso(),
                                                    "result": entry})
            print(str(manifest_path))
            return 0 if status == "SUCCEEDED" else 1
    except Exception as exc:
        # KeyboardInterrupt/SystemExit (BaseException) nunca se convierten:
        # se propagan sin marcar exito ni FAILED inventado.
        if attempt_path is None or attempt_base is None:
            # Preflight/header/hash denegados: sin intento, como establece TDD.
            if isinstance(exc, ValueError):
                print(f"ledger: FAILED {exc}", file=sys.stderr)
                return 1
            if isinstance(exc, RuntimeError):
                print(f"ledger: {exc}", file=sys.stderr)
                return 1
            raise
        try:
            launch._atomic_replace(attempt_path, {**attempt_base, "status": "FAILED",
                                                   "finished_at": _utcnow_iso(),
                                                   "error": f"{type(exc).__name__}: {exc}"})
        except OSError as persist_exc:
            print(f"ledger: FAILED {exc} (persist FAILED fallo: {persist_exc})",
                  file=sys.stderr)
            raise
        print(f"ledger: FAILED {exc}", file=sys.stderr)
        print(str(attempt_path))
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="operations.history")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="preflight host + input congelado (solo train)")
    prep.add_argument("--code-root", default=os.environ.get("LAB_CODE_ROOT", ""))
    prep.add_argument("--storage-root", default=os.environ.get("LAB_STORAGE_ROOT", ""))
    prep.add_argument("--image", default=launch.PINNED_IMAGE)
    prep.add_argument("--role", default="train", help="PR01: solo train")
    sub.add_parser("download", help="descarga TRAIN 5m en raiz unica (contenedor)")
    snap = sub.add_parser("snapshot", help="valida y congela snapshot TRAIN inmutable")
    snap.add_argument("--download", required=True,
                      help="manifiesto .json dentro de downloads (nombre simple)")
    led = sub.add_parser("ledger", help="ledger MTM + reconcile sobre segmento (sin backtest)")
    led.add_argument("--snapshot", required=True,
                     help="manifiesto .json dentro de snapshots (nombre simple)")
    led.add_argument("--segment", required=True, help="indice de segmento (p. ej. 0)")
    led.add_argument("--trades", required=True,
                     help="JSON con lista o {trades, profit_total_abs} (ruta contenedor)")
    led.add_argument("--initial", default="10000.0", help="capital por episodio")
    led.add_argument("--native-profit", default=None,
                     help="profit nativo para reconciliar (o en el JSON)")
    sub.add_parser("phase-contract", help="contrato exacto para abrir VAL/TEST en PR02")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        if not args.code_root or not args.storage_root:
            print("prepare exige --code-root y --storage-root (o LAB_CODE_ROOT/LAB_STORAGE_ROOT)",
                  file=sys.stderr)
            return 2
        try:
            print(prepare_history(args.code_root, args.storage_root, args.image, args.role))
            return 0
        except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
            print(f"prepare: {exc}", file=sys.stderr)
            return 1
    if args.command == "download":
        return cmd_download()
    if args.command == "snapshot":
        return cmd_snapshot(args.download)
    if args.command == "ledger":
        profit = None if args.native_profit is None else float(args.native_profit)
        return cmd_ledger(args.snapshot, args.segment, args.trades, float(args.initial), profit)
    if args.command == "phase-contract":
        print(json.dumps(phase_artifact_contract(), indent=2, sort_keys=True))
        return 0
    parser.error("comando desconocido")
    return 2


if __name__ == "__main__":
    sys.exit(main())
