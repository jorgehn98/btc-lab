"""Independent, closed TRAIN-only SMA50/200 study; no holdout commands."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from operations import launch, search
from research.regime import CAMPAIGN_ID, WARMUP, generate_variants, render_strategy_module
from research.regime_selection import (
    _CALENDAR_DAYS, _HURDLE, choose_train_finalists, summarize_train,
    walk_forward_select,
)
from research.state import new_state

STORE_NAME = "regime"
INPUT_KIND = "btc-lab-regime-input"
REPORT_KIND = "btc-lab-regime-screen"
GENERATED_FILE = "RegimeCandidatesGenerated.py"
ROOT = Path("/lab-regime")
CODE = Path("/opt/btc-lab")
INPUT = ROOT / "input.json"
TRAIN = ROOT / "snapshots/train"
TRAIN_MANIFEST = ROOT / "snapshots/train-manifest.json"

_EXTRA_CODE = {
    "runner_hash": Path("operations/regime.py"),
    "regime_registry_hash": Path("research/regime.py"),
    "regime_selection_hash": Path("research/regime_selection.py"),
    "regime_base_hash": Path("strategies/regime/SpotRegime.py"),
}


def _code_hashes(code: Path) -> dict:
    hashes = search._code_hashes(code)
    hashes["legacy_candidates_hash"] = hashes["candidates_hash"]
    hashes["candidates_hash"] = launch.file_hash(code / _EXTRA_CODE["regime_base_hash"])
    for key, rel in _EXTRA_CODE.items():
        hashes[key] = launch.file_hash(code / rel)
    return hashes


@contextmanager
def _locked(control: Path):
    control.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(control / "regime.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _snapshot_ref(manifest_path: Path, manifest: dict, manifest_name=None) -> dict:
    return {
        "manifest": search._safe_manifest_basename(manifest_name or manifest_path.name),
        "manifest_sha256": launch.file_hash(manifest_path),
        "snapshot_id": str(manifest["snapshot_id"]),
        "snapshot_dir": str(manifest["snapshot_dir"]),
        "whole_5m_sha256": str(manifest["whole_5m_sha256"]),
        "whole_1h_sha256": str(manifest["whole_1h_sha256"]),
        "range_start": str(manifest["range_start"]),
        "range_end": str(manifest["range_end"]),
        "rows_5m": int(manifest.get("rows_5m", 0)),
        "rows_1h": int(manifest.get("rows_1h", 0)),
        "segments": int(manifest.get("segments", 0)),
    }


def _definition(snapshot_ref, code_hashes, generated_sha):
    from market.history import partition_bounds

    start, end = partition_bounds("train")
    return {
        "campaign_id": CAMPAIGN_ID,
        "variants": generate_variants(),
        "code_hashes": code_hashes,
        "generated_source_sha256": generated_sha,
        "snapshot_train_ref": snapshot_ref,
        "train_bounds": [start.isoformat(), end.isoformat()],
        "config_hash": code_hashes["config_hash"],
        "fees": list(search.FEES_SCREEN),
        "budget_seconds": search.BUDGET_SECONDS,
        "warmup_1h": WARMUP,
        "drawdown_pct": search.DD_LIMIT,
        "max_finalists": 3,
        "target": {"minimum_daily_log_observed": _HURDLE,
                   "minimum_daily_log_calendar": _HURDLE,
                   "calendar_days": int(_CALENDAR_DAYS)},
    }


def _check_snapshot(manifest_path: Path, snapshot_root: Path) -> dict:
    manifest = search._load_snapshot_manifest_file(manifest_path)
    search._verify_snapshot_scope(manifest, "train")
    search._verify_snapshot_data_files(snapshot_root, manifest)
    return manifest


def effective_windows(snapshot: dict) -> list:
    from research.evaluation import plan_windows

    segment_metas = []
    for meta in snapshot["segments_meta"]:
        start = datetime.fromisoformat(str(meta["start"]).replace("Z", "+00:00"))
        current = datetime.fromisoformat(
            str(meta.get("eval_start") or meta["start"]).replace("Z", "+00:00"))
        if start.tzinfo is None or current.tzinfo is None:
            raise ValueError("ventana TRAIN sin zona UTC")
        common = max(current, start + timedelta(hours=WARMUP))
        segment_metas.append({**meta, "eval_start": common.isoformat()})
    return plan_windows("train", {**snapshot, "segments_meta": segment_metas})


def prepare_regime(code_root, storage_root, image_ref, train_snapshot):
    if image_ref != launch.PINNED_IMAGE:
        raise ValueError("solo imagen oficial fijada")
    code, storage = Path(code_root), Path(storage_root)
    basename = search._safe_manifest_basename(train_snapshot)
    if not code.is_dir():
        raise FileNotFoundError(f"codigo no encontrado: {code}")
    config = json.loads((code / search.SEARCH_CONFIG_REL).read_text(encoding="utf-8"))
    search.validate_search_config(config, os.environ)
    hashes = _code_hashes(code)
    snapshot_path = storage / "history/snapshots" / basename
    snapshot = search._load_snapshot_manifest_file(snapshot_path)
    search._verify_snapshot_scope(snapshot, "train")
    from operations.research import _within_root

    snap_dir = _within_root(snapshot_path.parent, Path(snapshot["snapshot_dir"]),
                            "snapshot TRAIN")
    _check_snapshot(snapshot_path, snap_dir)
    ref = _snapshot_ref(snapshot_path, snapshot)
    generated = render_strategy_module(generate_variants())
    generated_sha = search._sha256_bytes(generated.encode("utf-8"))
    definition = _definition(ref, hashes, generated_sha)
    definition_hash = search.definition_hash_for(definition)
    commit = launch._git_commit(code)
    launch._git_clean(code)
    image_id = launch._inspect_image(image_ref)
    root = storage / STORE_NAME
    with _locked(root / "control"):
        if hashes != _code_hashes(code):
            raise ValueError("codigo alterado durante prepare")
        state_path = root / "control" / f"campaign-{CAMPAIGN_ID}.json"
        if state_path.exists():
            raise ValueError("campana ya preparada: usar input unico original, sin reset")
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        generated_path = root / "generated" / GENERATED_FILE
        if generated_path.exists():
            if launch.file_hash(generated_path) != generated_sha:
                raise ValueError("fuente generada distinta; no overwrite")
        else:
            generated_path.parent.mkdir(parents=True, exist_ok=True)
            temp = generated_path.with_name(f".{GENERATED_FILE}.{uuid.uuid4().hex}.tmp")
            try:
                temp.write_text(generated, encoding="utf-8")
                os.link(temp, generated_path)
            finally:
                temp.unlink(missing_ok=True)
        created = datetime.now(timezone.utc).isoformat()
        input_id = uuid.uuid4().hex
        record = {
            "kind": INPUT_KIND, "status": "PREPARED",
            "campaign_id": CAMPAIGN_ID, "definition_hash": definition_hash,
            "definition": definition, "input_id": input_id,
            "commit": commit, "created_at": created,
            "image_ref": image_ref, "image_id": image_id,
            "generated_source_sha256": generated_sha,
            **hashes,
        }
        input_path = root / "inputs" / f"regime-input-{input_id}.json"
        state = new_state(CAMPAIGN_ID, definition_hash)
        state["input_id"] = input_id
        launch._atomic_create_new(input_path, record)
        launch._atomic_create_new(state_path, state)
    return str(input_path)


def _verified():
    manifest = json.loads(INPUT.read_text(encoding="utf-8"))
    if (manifest.get("kind") != INPUT_KIND or manifest.get("status") != "PREPARED"
            or manifest.get("campaign_id") != CAMPAIGN_ID
            or manifest.get("image_ref") != launch.PINNED_IMAGE
            or "force" in manifest):
        raise ValueError("input fuera del estudio cerrado")
    config = json.loads((CODE / search.SEARCH_CONFIG_REL).read_text(encoding="utf-8"))
    search.validate_search_config(config, os.environ)
    hashes = _code_hashes(CODE)
    if any(manifest.get(key) != value for key, value in hashes.items()):
        raise ValueError("codigo distinto del input congelado")
    generated = render_strategy_module(generate_variants())
    generated_sha = search._sha256_bytes(generated.encode("utf-8"))
    source_path = ROOT / "generated" / GENERATED_FILE
    if (manifest.get("generated_source_sha256") != generated_sha
            or launch.file_hash(source_path) != generated_sha):
        raise ValueError("fuente generada distinta del input")
    snapshot = _check_snapshot(TRAIN_MANIFEST, TRAIN)
    ref = _snapshot_ref(
        TRAIN_MANIFEST, snapshot,
        manifest["definition"]["snapshot_train_ref"]["manifest"])
    expected = _definition(ref, hashes, generated_sha)
    if (manifest.get("definition") != expected
            or manifest.get("definition_hash") != search.definition_hash_for(expected)):
        raise ValueError("definicion/cobertura distinta del input")
    state_path = ROOT / "control" / f"campaign-{CAMPAIGN_ID}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if (state.get("campaign_id") != CAMPAIGN_ID
            or state.get("definition_hash") != manifest["definition_hash"]
            or state.get("input_id") != manifest.get("input_id")
            or state.get("grants") != {} or state.get("test_consumed") is not False
            or "force" in state):
        raise ValueError("estado de otra campana o fase externa no autorizada")
    return manifest, state, state_path, snapshot


def _finish(state, state_path, report_path, payload):
    launch._atomic_replace(report_path, payload)
    state.setdefault("phase_reports", {})["screen"] = {
        "path": str(report_path.relative_to(ROOT)),
        "sha256": launch.file_hash(report_path),
        "status": payload["status"], "verdict": payload["verdict"],
    }
    search._save_state_atomic(state_path, state)


def screen_regime():
    report_path = None
    try:
        manifest, _prelock, _state_path, snapshot = _verified()
        with _locked(ROOT / "control"):
            manifest, state, state_path, snapshot = _verified()
            prior = (state.get("phase_reports") or {}).get("screen")
            if isinstance(prior, dict) and prior.get("status") == "SUCCEEDED":
                report_path = search._contained_under(ROOT, prior["path"], "report")
                if launch.file_hash(report_path) != prior["sha256"]:
                    raise ValueError("reporte previo alterado; no resume")
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if (report.get("definition_hash") != manifest["definition_hash"]
                        or report.get("status") != prior["status"]
                        or report.get("verdict") != prior["verdict"]):
                    raise ValueError("reporte terminal inconsistente")
                print(str(report_path))
                return 0 if prior["verdict"] == "PRELIMINARY" else 1
            variants = generate_variants()
            by_id = {v["id"]: v for v in variants}
            windows = effective_windows(snapshot)
            if not windows:
                raise ValueError("TRAIN sin ventanas")
            groups = [{
                "names": chunk, "use_list": True,
                "spath": str(ROOT / "generated"),
                "params": {name: next(v["params"] for v in variants
                                 if v["class_name"] == name) for name in chunk},
                "suffix": f"batch{idx:02d}",
            } for idx, chunk in enumerate(search._batch_names(
                [v["class_name"] for v in variants]))]
            groups.append({
                "names": [search.CONTROL_STRATEGY], "use_list": False,
                "spath": search.CONTAINER_STRATEGY_CODE_PATH,
                "params": {search.CONTROL_STRATEGY: {"control": True}},
                "suffix": "control",
            })
            sessions = ROOT / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            session = sessions / f"screen-{search._slug()}"
            session.mkdir(parents=True, exist_ok=False)
            batch_dir = session / "batches"
            batch_dir.mkdir(parents=True, exist_ok=False)
            report_path = session / "report.json"
            launch._atomic_create_new(report_path, {
                "kind": REPORT_KIND, "status": "RUNNING",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "definition_hash": manifest["definition_hash"],
                "input_id": manifest["input_id"],
            })
            results, stats = search._execute_native_matrix(
                phase="screen", role="train", windows=windows,
                fees=list(search.FEES_SCREEN), groups=groups,
                snap_root=str(TRAIN), session_dir=session, batch_dir=batch_dir,
                state=state, state_path=state_path, manifest_in=manifest,
                code_hashes=_code_hashes(CODE),
                seg_meta_by_dir={str(m["seg_dir"]): m
                                 for m in snapshot["segments_meta"]},
                by_class_params={v["class_name"]: v["params"] for v in variants},
                search_root=ROOT)
            if stats["failed"]:
                raise ValueError(f"{stats['failed']}/{stats['total']} lotes fallidos")
            analysis_started = time.monotonic()
            episodes = search._candidate_episodes(
                results, windows, list(search.FEES_SCREEN), sorted(by_id),
                by_id, str(TRAIN))
            control = search._control_coverage(
                results, windows, list(search.FEES_SCREEN), str(TRAIN))
            records = search._aggregate_role_records(
                "train", episodes, windows, list(search.FEES_SCREEN), by_id)
            wf = {str(year): walk_forward_select(records, year)
                  for year in (2019, 2020, 2021, 2022)}
            metrics = summarize_train(records)
            top3 = choose_train_finalists(records)
            if top3:
                state["preselected_ids"] = list(top3)
                state["stage"] = "TRAIN_PRESELECTED"
            verdict = "PRELIMINARY" if top3 else "NO_CANDIDATE"
            _finish(state, state_path, report_path, {
                "kind": REPORT_KIND, "status": "SUCCEEDED",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "verdict": verdict, "preliminary_ids": list(top3),
                "records": records, "metrics": metrics,
                "analysis_elapsed_s": time.monotonic() - analysis_started,
                "native_jobs": stats,
                "wf_descriptive": wf, "control_coverage": control,
                "definition_hash": manifest["definition_hash"],
                "input_id": manifest["input_id"],
                "consumed": state["consumed"],
                "invalid_primary_records": sum(not r["valid"] for r in records
                                               if r["fee"] == search.FEES_SCREEN[-1]),
            })
            print(str(report_path))
            return 0 if top3 else 1
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        if report_path is not None:
            try:
                with _locked(ROOT / "control"):
                    previous = json.loads(report_path.read_text(encoding="utf-8"))
                    if previous.get("status") == "RUNNING":
                        launch._atomic_replace(report_path, {
                            **previous, "status": "FAILED", "verdict": "FAILED",
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                            "error": f"{type(exc).__name__}: {exc}",
                        })
            except (FileNotFoundError, ValueError, OSError, RuntimeError):
                print("regime screen: reporte de fallo no persistido", file=sys.stderr)
        print(f"regime screen: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def status_regime():
    try:
        manifest, state, _, _ = _verified()
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"regime status: input invalido: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "campaign_id": CAMPAIGN_ID, "definition_hash": manifest["definition_hash"],
        "stage": state["stage"], "consumed": state["consumed"],
        "budget_limit": state["budget_limit"],
        "preselected_ids": state.get("preselected_ids"),
        "train_ids": state["train_ids"],
        "phase_reports": state["phase_reports"], "grants": state["grants"],
        "test_consumed": state["test_consumed"],
    }, indent=2, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="operations.regime")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--code-root", default=os.environ.get("LAB_CODE_ROOT", "."))
    prepare.add_argument("--storage-root", default=os.environ.get("LAB_STORAGE_ROOT", "storage"))
    prepare.add_argument("--image", default=launch.PINNED_IMAGE)
    prepare.add_argument("--train-snapshot", required=True)
    commands.add_parser("screen")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        try:
            print(prepare_regime(args.code_root, args.storage_root, args.image,
                                 args.train_snapshot))
            return 0
        except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
            print(f"regime prepare: FAILED {exc}", file=sys.stderr)
            return 1
    if args.command == "screen":
        return screen_regime()
    return status_regime()


if __name__ == "__main__":
    sys.exit(main())
