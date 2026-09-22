"""Runner cerrado de investigación TRAIN para el baseline.

Descarga datos BTC/USDT spot de 5m para el intervalo TRAIN fijado, deriva velas
1h solo de grupos completos y ejecuta las herramientas nativas de backtest y
sesgo por segmento contiguo elegible. La preparación congela la identidad del
código y la imagen; los comandos usan raíces fijas, un lock compartido, logs
acotados y un snapshot de solo lectura verificado por hash antes de evaluar.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from operations import launch

TRAIN_PAIR = "BTC/USDT"
TRAIN_TIMEFRAME_5M = "5m"
TRAIN_TIMEFRAME_1H = "1h"
TRAIN_TIMERANGE = "20180101-20230101"
TRAIN_START = datetime(2018, 1, 1, tzinfo=timezone.utc)
TRAIN_END = datetime(2023, 1, 1, tzinfo=timezone.utc)
TRAIN_EXCHANGE = "binance"
STRATEGY_NAME = "SmaCrossBaseline"
CONFIG_REL = Path("configs/baseline.json")
STRATEGY_FILE_REL = Path("strategies/baseline/SmaCrossBaseline.py")
STRATEGY_DIR_REL = Path("strategies/baseline")
MARKET_REL = Path("market/train.py")
RESEARCH_REL = Path("operations/research.py")

FEES = (0.001, 0.0015, 0.002, 0.003)
BASE_FEE = 0.001
STARTUP_LIST = (51, 100, 200, 400)
MIN_TRADES_LOOKAHEAD = 5
MIN_SEGMENT_BARS = 52
MIN_RECURSIVE_BARS = 1000
RECURSIVE_CONTEXT = 400
SMA_TOL = 1e-10
WARMUP_BARS = 51

CONTAINER_CODE = "/opt/btc-lab"
CONTAINER_RESEARCH = "/lab-research"
CONTAINER_INPUT = "/lab-research/input.json"
CONTAINER_CONFIG = "/opt/btc-lab/configs/baseline.json"
CONTAINER_STRATEGY_PATH = "/opt/btc-lab/strategies/baseline"

INPUT_KIND = "btc-lab-research-input"
DOWNLOAD_KIND = "btc-lab-research-download"
SNAPSHOT_KIND = "btc-lab-research-snapshot"
BACKTEST_KIND = "btc-lab-research-backtest"
BIAS_KIND = "btc-lab-research-bias"

DOWNLOAD_TIMEOUT_S = 1800
BACKTEST_TIMEOUT_S = 1800
BIAS_TIMEOUT_S = 1800
LOG_TAIL_BYTES = 256 * 1024
_RUN_LOG_CAP = 1_048_576


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _code_hashes(code_root: Path) -> dict:
    code = Path(code_root)
    return {
        "config_hash": launch.file_hash(code / CONFIG_REL),
        "strategy_hash": launch.file_hash(code / STRATEGY_FILE_REL),
        "market_hash": launch.file_hash(code / MARKET_REL),
        "launch_hash": launch.file_hash(code / "operations/launch.py"),
        "health_hash": launch.file_hash(code / "operations/health.py"),
        "research_hash": launch.file_hash(code / RESEARCH_REL),
    }


def _slug() -> str:
    return f"{_utcnow_iso().replace(':', '').replace('+', '')}-{uuid.uuid4().hex[:8]}"


def _pair_file(pair: str, timeframe: str) -> str:
    """Nombre exacto de Freqtrade para spot con ``--datadir`` explícito.

    La imagen de runtime usa ``BTC_USDT-5m.feather`` y ``BTC_USDT-1h.feather``
    para TRAIN; el snapshot exige esos nombres exactos.
    """
    cleaned = pair
    for ch in ("/", " ", ".", "@", "$", "+", ":"):
        cleaned = cleaned.replace(ch, "_")
    tf_file = timeframe.replace("M", "Mo")
    return f"{cleaned}-{tf_file}.feather"


def _timerange_fmt(start_dt, end_exclusive_dt) -> str:
    fmt = "%Y%m%dT%H%M"
    return f"{start_dt.strftime(fmt)}-{end_exclusive_dt.strftime(fmt)}"


def _load_5m_frame(feather_paths: list):
    """Concatena archivos 5m sin normalizarlos.

    Los duplicados quedan deliberadamente para que ``validate_ohlcv`` los
    rechace, tanto dentro de un archivo como entre archivos y aunque sean contradictorios.
    """
    import pandas as pd

    frames = [pd.read_feather(str(p)) for p in feather_paths]
    if not frames:
        raise ValueError("sin ficheros 5m")
    return pd.concat(frames, ignore_index=True)


def longest_segment_meta(segments_meta: list) -> dict | None:
    best = None
    for meta in segments_meta:
        if best is None or int(meta.get("length", 0)) > int(best.get("length", 0)):
            best = meta
        elif int(meta.get("length", 0)) == int(best.get("length", 0)) and str(
            meta.get("start", "")
        ) < str(best.get("start", "")):
            best = meta
    return best


def _baseline_cap(wallet: float = 10000.0) -> float:
    return min(1000.0, 0.10 * wallet, (0.0025 * wallet) / 0.026)


def buyhold_for_segment(first_open: float, last_open: float,
                        wallet: float = 10000.0, fee: float = 0.001,
                        tradable_balance_ratio: float = 0.99) -> dict:
    """Benchmark buy-and-hold comparable por friccion.

    Exposicion cap sobre wallet disponible (wallet*ratio, como el motor) con la
    misma formula de riesgo; compra en el open efectivo inicial y venta en el
    open de la ultima vela (el motor cierra lo abierto al ultimo open
    disponible). Fees fuera del principal: qty=cap/first_open y
    pnl=qty*last_open*(1-fee)-cap*(1+fee). Retorno sobre wallet de cuenta.
    """
    import math

    try:
        ratio = float(tradable_balance_ratio)
    except (TypeError, ValueError):
        raise ValueError("tradable_balance_ratio no numerico")
    if not math.isfinite(ratio) or not 0.0 < ratio <= 1.0:
        raise ValueError("tradable_balance_ratio fuera de (0,1]")
    available = float(wallet) * ratio
    cap = _baseline_cap(available)
    for value in (first_open, last_open):
        if not math.isfinite(float(value)) or float(value) <= 0.0:
            raise ValueError("open de segmento no valido para buyhold")
    if not math.isfinite(float(fee)) or float(fee) < 0.0:
        raise ValueError("fee no valida para buyhold")
    first = float(first_open)
    last = float(last_open)
    fee = float(fee)
    qty = cap / first
    end_value = qty * last * (1.0 - fee)
    cost = cap * (1.0 + fee)
    pnl = end_value - cost
    return {
        "cap": cap,
        "wallet": float(wallet),
        "available": available,
        "tradable_balance_ratio": ratio,
        "first_open": first,
        "last_open": last,
        "fee": fee,
        "end_value": end_value,
        "pnl": pnl,
        "return": pnl / float(wallet),
        "position_return": pnl / cap,
        "cash_return": 0.0,
    }


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
    """Exige que el código montado en solo lectura coincida con el input; sin Git aquí."""
    code = Path(CONTAINER_CODE)
    config_path = code / CONFIG_REL
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config ilegible: {exc}") from exc
    launch.validate_baseline_config(config, os.environ)
    current = _code_hashes(code)
    for key, value in current.items():
        expected = manifest_in.get(key)
        if not expected or expected != value:
            raise ValueError(f"codigo alterado tras prepare ({key})")
    return current


def _locked():
    """Un job a la vez sobre control compartido (ambos servicios research)."""
    import fcntl

    control = Path(CONTAINER_RESEARCH) / "control"
    control.mkdir(parents=True, exist_ok=True)
    lock_path = control / "research.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)

    class _Guard:
        def __enter__(self):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise RuntimeError("otro job research en curso (lock ocupado)")
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return False

    return _Guard()


def _within_root(root: Path, candidate: Path, label: str) -> Path:
    """Resuelve symlinks y exige que el destino siga dentro de root."""
    root_res = Path(root).resolve()
    cand = Path(candidate)
    target = (cand if cand.is_absolute() else (Path(root) / cand)).resolve()
    try:
        target.relative_to(root_res)
    except ValueError:
        raise ValueError(f"{label} fuera de la raiz") from None
    return target


def _contained(root: Path, name: str, label: str) -> Path:
    """Une un nombre simple a root con contencion estricta (symlinks incluidos)."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{label} ausente")
    cand = Path(name)
    if cand.is_absolute() or len(cand.parts) != 1 or cand.parts[0] in (".", ".."):
        raise ValueError(f"{label} fuera de la raiz")
    return _within_root(Path(root), cand, label)


def _run_streaming(argv: list, timeout_s: int, log_path: Path) -> tuple:
    """Ejecuta con salida acotada en memoria y fichero (cola 1 MiB + cabecera).

    Lee el pipe por chunks de 4096 (sin ``readline`` que se cuelgue en una linea
    gigante), drena siempre para no bloquear al hijo y descarta lo mas antiguo
    cuando se supera el tope. El fichero final es cabecera + cola acotada.
    """
    import collections
    import threading

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = f"$ {' '.join(str(a) for a in argv)}\n".encode("utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(a) for a in argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    chunks: collections.deque = collections.deque()
    total = [0]
    errors: list = []

    def _pump() -> None:
        try:
            assert proc.stdout is not None
            while True:
                data = proc.stdout.read(4096)
                if not data:
                    break
                chunks.append(data)
                total[0] += len(data)
                while total[0] > _RUN_LOG_CAP and chunks:
                    old = chunks.popleft()
                    total[0] -= len(old)
        except Exception as exc:
            errors.append(exc)
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception as exc:
                errors.append(exc)

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    cleanup_error = None
    try:
        returncode = proc.wait(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=30)
        except Exception as exc:
            cleanup_error = exc
        returncode = None
        timed_out = True
    finally:
        reader.join(timeout=30)

    def _write(tail_bytes: bytes) -> None:
        with open(log_path, "wb") as handle:
            handle.write(header)
            handle.write(tail_bytes)
            if timed_out:
                handle.write(f"\nTIMEOUT after {timeout_s}s\n".encode("utf-8", errors="replace"))
            handle.flush()
            os.fsync(handle.fileno())

    tail = b"".join(chunks)
    if reader.is_alive():
        try:
            _write(tail)
        except OSError:
            pass
        raise OSError("reader sin terminar tras join")
    if errors:
        primary = errors[0]
        try:
            _write(tail)
        except OSError:
            pass
        errno = getattr(primary, "errno", None)
        raise OSError(f"reader: {type(primary).__name__} errno={errno}") from primary
    if cleanup_error is not None:
        try:
            _write(tail)
        except OSError:
            pass
        raise OSError(f"cleanup: {type(cleanup_error).__name__}") from cleanup_error
    _write(tail)
    return returncode, timed_out


def _read_tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _safe_basename(name: str) -> str:
    base = Path(str(name or "")).name
    if not base or base in (".", "..") or "/" in str(name or "") or "\\" in str(name or ""):
        raise ValueError(f"referencia no es un nombre simple dentro de la raiz: {name!r}")
    if not base.endswith(".json"):
        raise ValueError(f"referencia debe ser un manifiesto .json: {name!r}")
    return base


def prepare_research(code_root, storage_root, image_ref) -> str:
    """Preflight HOST: Git limpio + imagen fijada + config baseline cerrada."""
    if not isinstance(image_ref, str) or image_ref.strip() != launch.PINNED_IMAGE:
        raise ValueError("image_ref debe ser exactamente la imagen fijada con digest")
    code = Path(code_root)
    store = Path(storage_root)
    if not code.is_dir():
        raise FileNotFoundError(f"codigo no encontrado: {code}")
    cfg_path = code / CONFIG_REL
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config baseline ilegible: {exc}") from exc
    launch.validate_baseline_config(config, os.environ)
    for rel in (STRATEGY_FILE_REL, MARKET_REL, RESEARCH_REL,
                Path("operations/launch.py"), Path("operations/health.py")):
        if not (code / rel).is_file():
            raise FileNotFoundError(f"fichero hasheado ausente: {rel}")
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
        "strategy": STRATEGY_NAME,
        "config_path": CONTAINER_CONFIG,
        "pair": TRAIN_PAIR,
        "exchange": TRAIN_EXCHANGE,
        "timeframe": TRAIN_TIMEFRAME_1H,
        "timeframe_detail": TRAIN_TIMEFRAME_5M,
        "timerange": TRAIN_TIMERANGE,
        "range_start": TRAIN_START.isoformat(),
        "range_end": TRAIN_END.isoformat(),
        **hashes,
    }
    out = store / "research" / "inputs" / f"research-input-{_slug()}.json"
    launch._atomic_create_new(out, manifest)
    return str(out)


def cmd_download() -> int:
    try:
        manifest_in = _load_input()
        current = _verify_current_against_input(manifest_in)
    except Exception as exc:
        print(f"download: input/codigo no valido: {exc}", file=sys.stderr)
        return 2
    from market.train import train_download_argv

    research = Path(CONTAINER_RESEARCH)
    try:
        with _locked():
            dl_id = uuid.uuid4().hex
            root = research / "downloads" / f"dl-{_slug()}-{dl_id[:4]}"
            data_dir = root / "data"
            user_dir = root / "user_data"
            data_dir.mkdir(parents=True, exist_ok=False)
            user_dir.mkdir(parents=True, exist_ok=False)
            manifest_path = research / "downloads" / f"dl-{_slug()}-{dl_id[:4]}.json"
            argv = train_download_argv(CONTAINER_CONFIG, str(data_dir))
            # userdir separado dentro de la misma raiz unica (aislamiento).
            argv = argv + ["--userdir", str(user_dir)]
            base = {
                "kind": DOWNLOAD_KIND,
                "status": "RUNNING",
                "download_id": dl_id,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "commit": manifest_in.get("commit"),
                "image_ref": manifest_in.get("image_ref"),
                "image_id": manifest_in.get("image_id"),
                "pair": TRAIN_PAIR,
                "timeframe": TRAIN_TIMEFRAME_5M,
                "timerange": TRAIN_TIMERANGE,
                "datadir": str(data_dir),
                "userdir": str(user_dir),
                "argv": [str(a) for a in argv],
                **current,
            }
            launch._atomic_create_new(manifest_path, {**base, "exit_code": None})
            log_path = root / "download.log"
            try:
                returncode, timed_out = _run_streaming(argv, DOWNLOAD_TIMEOUT_S, log_path)
            except FileNotFoundError as exc:
                launch._atomic_replace(manifest_path, {**base, "status": "FAILED",
                                                       "finished_at": _utcnow_iso(),
                                                       "error": f"ejecutable ausente: {exc}"})
                print(f"download: FAILED {exc}", file=sys.stderr)
                return 1
            except OSError as exc:
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
            expected = data_dir / _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_5M)
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

    research = Path(CONTAINER_RESEARCH)
    downloads = research / "downloads"
    snapshots = research / "snapshots"
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
    if dl_manifest.get("pair") != TRAIN_PAIR or dl_manifest.get("timerange") != TRAIN_TIMERANGE:
        print("snapshot: download de otro par/rango", file=sys.stderr)
        return 1
    try:
        data_dir = _within_root(downloads, Path(dl_manifest.get("datadir") or ""), "datadir")
    except ValueError:
        print("snapshot: datadir fuera de downloads", file=sys.stderr)
        return 1
    if not data_dir.is_dir():
        print("snapshot: datadir fuera de downloads", file=sys.stderr)
        return 1
    expected_5m = _contained(data_dir, _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_5M), "5m")
    try:
        if not expected_5m.is_file():
            raise FileNotFoundError(f"falta {expected_5m.name} exacto Freqtrade")
        frozen_hash = launch.file_hash(expected_5m)
        if not dl_manifest.get("data_file_sha256") or frozen_hash != dl_manifest.get("data_file_sha256"):
            raise ValueError("feather 5m modificado tras download")
    except OSError as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 1
    try:
        with _locked():
            snap_id = uuid.uuid4().hex
            manifest_path = snapshots / f"snap-{_slug()}-{snap_id[:4]}.json"
            snap_dir = snapshots / f"snap-{snap_id[:8]}"
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
                "pair": TRAIN_PAIR,
                "timerange": TRAIN_TIMERANGE,
                "range_start": TRAIN_START.isoformat(),
                "range_end": TRAIN_END.isoformat(),
                **current,
            }
            launch._atomic_create_new(manifest_path, {**base})
            try:
                if launch.file_hash(expected_5m) != dl_manifest.get("data_file_sha256"):
                    raise ValueError("feather 5m modificado tras download")
                frame_5m = _load_5m_frame([str(expected_5m)])
                loaded_hash = launch.file_hash(expected_5m)
                if not dl_manifest.get("data_file_sha256") or loaded_hash != dl_manifest.get("data_file_sha256"):
                    raise ValueError("feather 5m modificado durante load")
                validate_ohlcv(frame_5m, TRAIN_START, TRAIN_END, 5)
                hourly = aggregate_hourly(frame_5m)
                segments = contiguous_segments(hourly)
                gaps = gaps_table(hourly)
                snap_dir.mkdir(parents=True, exist_ok=False)
                whole_5m = snap_dir / _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_5M)
                whole_1h = snap_dir / _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_1H)
                frame_5m.sort_values("date").reset_index(drop=True).to_feather(str(whole_5m))
                hourly.to_feather(str(whole_1h))
                seg_meta = []
                for idx, seg in enumerate(segments):
                    start = pd.to_datetime(seg["date"].iloc[0], utc=True).to_pydatetime()
                    end_last = pd.to_datetime(seg["date"].iloc[-1], utc=True).to_pydatetime()
                    end_excl = end_last + timedelta(hours=1)
                    seg_dir = snap_dir / f"seg{idx:02d}"
                    seg_dir.mkdir(parents=True, exist_ok=False)
                    mask_5 = (pd.to_datetime(frame_5m["date"], utc=True) >= start) & (
                        pd.to_datetime(frame_5m["date"], utc=True) < end_excl)
                    seg_5 = frame_5m.loc[mask_5].sort_values("date").reset_index(drop=True)
                    seg_1 = seg.sort_values("date").reset_index(drop=True)
                    seg_5_path = seg_dir / _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_5M)
                    seg_1_path = seg_dir / _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_1H)
                    seg_5.to_feather(str(seg_5_path))
                    seg_1.to_feather(str(seg_1_path))
                    eval_start = start + timedelta(hours=WARMUP_BARS)
                    seg_meta.append({
                        "index": idx,
                        "length": int(len(seg)),
                        "start": start.astimezone(timezone.utc).isoformat(),
                        "end_last": end_last.astimezone(timezone.utc).isoformat(),
                        "end_exclusive": end_excl.astimezone(timezone.utc).isoformat(),
                        "eval_start": eval_start.astimezone(timezone.utc).isoformat(),
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
                    "note": "1h de grupos 12x5m completos; metadata exchange actual no historica",
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


def _load_eval_snapshot(snapshot_ref: str) -> tuple:
    """Carga y verifica por hash un snapshot congelado; evaluar nunca lo repara."""
    base_name = _safe_basename(snapshot_ref)
    research = Path(CONTAINER_RESEARCH)
    snaps = research / "snapshots"
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
    snap_dir = _contained(snaps, str(manifest.get("snapshot_dir") or ""), "snapshot_dir")
    if not snap_dir.is_dir():
        raise ValueError("snapshot_dir ausente (evaluacion RO, sin reparar)")
    # Verifica por hash el snapshot completo y cada segmento antes de leer datos.
    for label, rel, key in (
        ("5m total", _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_5M), "whole_5m_sha256"),
        ("1h total", _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_1H), "whole_1h_sha256"),
    ):
        target = _contained(snap_dir, rel, label)
        if not target.is_file() or launch.file_hash(target) != manifest.get(key):
            raise ValueError(f"dato inmutable alterado ({label})")
    for meta in manifest.get("segments_meta") or []:
        seg_dir = _contained(snap_dir, str(meta.get("seg_dir") or ""), "seg_dir")
        for rel, key in (
            (_pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_5M), "file_5m_sha256"),
            (_pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_1H), "file_1h_sha256"),
        ):
            target = _contained(seg_dir, rel, f"seg{meta.get('index')}")
            if not target.is_file() or launch.file_hash(target) != meta.get(key):
                raise ValueError(f"dato de segmento alterado (seg{meta.get('index')})")
    return manifest, snap_dir, manifest_path


def _backtest_argv(seg_dir: Path, user_dir: Path, timerange: str,
                   fee: float, export_dir: Path) -> list:
    return [
        "freqtrade", "backtesting",
        "--config", CONTAINER_CONFIG,
        "--datadir", str(seg_dir),
        "--userdir", str(user_dir),
        "--strategy", STRATEGY_NAME,
        "--strategy-path", CONTAINER_STRATEGY_PATH,
        "--timeframe", TRAIN_TIMEFRAME_1H,
        "--timeframe-detail", TRAIN_TIMEFRAME_5M,
        "--timerange", str(timerange),
        "--fee", repr(float(fee)),
        "--export", "trades",
        "--export-directory", str(export_dir),
        "--cache", "none",
    ]


def _num_or_none(value):
    import math

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _summarize_native(native_dir: Path) -> tuple:
    """Parsea métricas nativas; archivos o schema requeridos ausentes son errores."""
    import zipfile

    zips = sorted([p for p in Path(native_dir).glob("*.zip") if p.is_file()])
    if not zips:
        return None, "sin ZIP nativo"
    if len(zips) != 1:
        return None, f"ZIP ambiguo ({len(zips)} ficheros, sin elegir)"
    inner_name = None
    payload = None
    try:
        with zipfile.ZipFile(str(zips[0]), "r") as bundle:
            names = bundle.namelist()
            for cand in sorted(names):
                if not cand.endswith(".json") or "config" in cand.lower():
                    continue
                try:
                    raw = bundle.read(cand).decode("utf-8")
                except (KeyError, UnicodeDecodeError):
                    continue
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(data, dict) and (
                    STRATEGY_NAME in data or "strategy" in data
                ):
                    inner_name = cand
                    payload = data
                    break
            if payload is None:
                return None, f"schema sin estrategia ({sorted(names)})"
    except zipfile.BadZipFile as exc:
        return None, f"ZIP ilegible: {exc}"
    strat = payload.get(STRATEGY_NAME)
    if strat is None and isinstance(payload.get("strategy"), dict):
        strat = payload["strategy"].get(STRATEGY_NAME)
    if not isinstance(strat, dict):
        return None, "schema sin metricas de estrategia"
    profit_abs = _num_or_none(strat.get("profit_total_abs"))
    port_return = _num_or_none(strat.get("profit_total"))
    raw_trades = strat.get("total_trades")
    try:
        trades = None
        if isinstance(raw_trades, bool):
            raise ValueError("bool no es conteo")
        if raw_trades is not None:
            trades = int(raw_trades)
            if trades < 0:
                raise ValueError("conteo negativo")
            if float(raw_trades) != float(trades):
                raise ValueError("conteo no entero")
    except (TypeError, ValueError):
        trades = None
    if profit_abs is None or port_return is None or trades is None:
        return None, "schema sin nucleo (profit_total_abs/profit_total/total_trades)"
    drawdown = _num_or_none(strat.get("max_drawdown_abs"))
    if drawdown is None:
        drawdown = _num_or_none(strat.get("max_drawdown_account"))
    # Fees nativas son costos en USDT por trade (long spot cerrado, sin DCA):
    # se prefiere fee_open_cost+fee_close_cost y si no amount*open_rate*fee_open
    # + amount*close_rate*fee_close. Sin componentes completos no se inventa.
    fees = None
    fee_trades = strat.get("trades")
    if isinstance(fee_trades, list):
        try:
            fee_sum = 0.0
            for trade in fee_trades:
                if not isinstance(trade, dict):
                    raise ValueError("trade no es objeto")
                foc = _num_or_none(trade.get("fee_open_cost"))
                fcc = _num_or_none(trade.get("fee_close_cost"))
                if foc is not None or fcc is not None:
                    if foc is None or fcc is None or foc < 0.0 or fcc < 0.0:
                        raise ValueError("costos incompletos")
                    for key in ("fee_open_currency", "fee_close_currency", "fee_currency"):
                        cur = trade.get(key)
                        if cur not in (None, "", "USDT"):
                            raise ValueError("fee en otra moneda")
                    fee_sum += foc + fcc
                    continue
                amount = _num_or_none(trade.get("amount"))
                open_rate = _num_or_none(trade.get("open_rate"))
                close_rate = _num_or_none(trade.get("close_rate"))
                fee_open = _num_or_none(trade.get("fee_open"))
                fee_close = _num_or_none(trade.get("fee_close"))
                if (amount is None or amount <= 0.0 or open_rate is None or open_rate <= 0.0
                        or close_rate is None or close_rate <= 0.0
                        or fee_open is None or fee_open < 0.0
                        or fee_close is None or fee_close < 0.0):
                    raise ValueError("componentes de fee incompletos")
                fee_sum += amount * open_rate * fee_open + amount * close_rate * fee_close
            fees = fee_sum
        except (TypeError, ValueError):
            fees = None
    summary: dict = {
        "profit_abs": profit_abs,
        "portfolio_return": port_return,
        "trades": trades,
        "drawdown": drawdown,
        "fees": fees,
        "exposure": None,
        "zip": zips[0].name,
        "inner": inner_name,
    }
    reasons: dict = {}
    if drawdown is None:
        reasons["drawdown"] = "informe sin drawdown"
    if fees is None:
        reasons["fees"] = "informe sin fees desagregadas"
    reasons["exposure"] = "exposicion no expuesta por el informe nativo"
    if reasons:
        summary["null_reasons"] = reasons
    summary["trade_list"] = list(strat.get("trades") or [])
    return summary, None


def cmd_backtest(snapshot_ref: str) -> int:
    try:
        manifest_in = _load_input()
        current = _verify_current_against_input(manifest_in)
    except Exception as exc:
        print(f"backtest: input/codigo no valido: {exc}", file=sys.stderr)
        return 2
    try:
        snap_manifest, snap_dir, snap_path = _load_eval_snapshot(snapshot_ref)
    except FileNotFoundError:
        print(f"backtest: snapshot inexistente: {snapshot_ref}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"backtest: snapshot invalido: {exc}", file=sys.stderr)
        return 1
    import pandas as pd

    research = Path(CONTAINER_RESEARCH)
    sessions = research / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    try:
        with _locked():
            session_id = uuid.uuid4().hex
            session_dir = sessions / f"backtest-{_slug()}-{session_id[:4]}"
            session_dir.mkdir(parents=True, exist_ok=False)
            manifest_path = session_dir / "session.json"
            base = {
                "kind": BACKTEST_KIND,
                "status": "RUNNING",
                "session_id": session_id,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "snapshot_id": snap_manifest.get("snapshot_id"),
                "snapshot_manifest": str(snap_path),
                "commit": manifest_in.get("commit"),
                "image_ref": manifest_in.get("image_ref"),
                "strategy": STRATEGY_NAME,
                "pair": TRAIN_PAIR,
                "fees": list(FEES),
                **current,
            }
            launch._atomic_create_new(manifest_path, {**base, "exit_code": None})
            segments_meta = snap_manifest.get("segments_meta") or []
            eligible = [m for m in segments_meta
                        if int(m.get("length", 0)) >= MIN_SEGMENT_BARS
                        and m.get("start") and m.get("end_exclusive")]
            results: list = []
            failed = 0
            aborted = False
            if not eligible:
                launch._atomic_replace(manifest_path, {**base, "status": "INCONCLUSIVE",
                                                       "evaluation_status": "INCONCLUSIVE",
                                                       "finished_at": _utcnow_iso(),
                                                       "reason": "sin segmentos evaluables",
                                                       "results": results,
                                                       "session_dir": str(session_dir)})
                print(str(manifest_path))
                return 1
            for meta in eligible:
                start_dt = datetime.fromisoformat(str(meta["start"]).replace("Z", "+00:00"))
                eval_dt = datetime.fromisoformat(
                    str(meta.get("eval_start") or meta["start"]).replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(
                    str(meta["end_exclusive"]).replace("Z", "+00:00"))
                if eval_dt >= end_dt:
                    results.append({"segment": meta.get("index"), "status": "INCONCLUSIVE",
                                    "evaluation_status": "INCONCLUSIVE",
                                    "reason": "warmup 51 cubre todo el segmento"})
                    continue
                timerange = _timerange_for_eval(eval_dt, end_dt)
                seg_dir = snap_dir / str(meta.get("seg_dir"))
                try:
                    seg_frame = pd.read_feather(
                        str(seg_dir / _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_1H)))
                    seg_frame["date"] = pd.to_datetime(seg_frame["date"], utc=True)
                    mask = (seg_frame["date"] >= eval_dt) & (seg_frame["date"] < end_dt)
                    eval_rows = seg_frame.loc[mask].sort_values("date").reset_index(drop=True)
                    eval_error = None if len(eval_rows) else "sin velas en rango efectivo"
                except Exception as exc:
                    eval_rows, eval_error = None, f"{type(exc).__name__}: {exc}"
                for fee in FEES:
                    run_dir = session_dir / f"seg{int(meta['index']):02d}_fee{str(fee).replace('.', 'p')}"
                    run_dir.mkdir(parents=True, exist_ok=False)
                    export_dir = run_dir / "native"
                    export_dir.mkdir(parents=True, exist_ok=False)
                    user_dir = run_dir / "user_data"
                    user_dir.mkdir(parents=True, exist_ok=False)
                    argv = _backtest_argv(seg_dir, user_dir, timerange, float(fee), export_dir)
                    log_path = run_dir / "backtest.log"
                    entry: dict = {"segment": meta.get("index"), "fee": float(fee),
                                   "timerange": timerange, "argv": [str(a) for a in argv],
                                   "run_dir": str(run_dir), "boundary_tag": "frontier-close"}
                    try:
                        returncode, timed_out = _run_streaming(argv, BACKTEST_TIMEOUT_S, log_path)
                    except FileNotFoundError as exc:
                        entry.update({"status": "FAILED", "evaluation_status": "FAILED",
                                      "error": f"ejecutable ausente: {exc}"})
                        failed += 1
                        results.append(entry)
                        continue
                    except OSError as exc:
                        # Sin proceso confirmado terminado no hay replay del
                        # siguiente job: se aborta la sesion completa.
                        entry.update({"status": "FAILED", "evaluation_status": "FAILED",
                                      "error": f"{type(exc).__name__}: {exc}"})
                        failed += 1
                        results.append(entry)
                        aborted = True
                        break
                    entry.update({"exit_code": returncode, "timed_out": timed_out,
                                  "logfile": str(log_path)})
                    if timed_out or returncode is None:
                        entry.update({"status": "FAILED", "evaluation_status": "FAILED",
                                      "error": "timeout"})
                        failed += 1
                        results.append(entry)
                        continue
                    if returncode != 0:
                        entry.update({"status": "FAILED", "evaluation_status": "FAILED",
                                      "error": f"exit {returncode}",
                                      "log_tail": _read_tail(log_path, 8192)})
                        failed += 1
                        results.append(entry)
                        continue
                    summary, parse_error = _summarize_native(export_dir)
                    if parse_error or summary is None:
                        entry.update({"status": "FAILED", "evaluation_status": "FAILED",
                                      "error": parse_error})
                        failed += 1
                        results.append(entry)
                        continue
                    try:
                        if eval_rows is None or not len(eval_rows):
                            raise ValueError(eval_error or "sin velas en rango efectivo")
                        bench = buyhold_for_segment(
                            float(eval_rows["open"].iloc[0]),
                            float(eval_rows["open"].iloc[-1]), 10000.0, float(fee))
                        bench["first_date"] = eval_rows["date"].iloc[0].isoformat()
                        bench["last_date"] = eval_rows["date"].iloc[-1].isoformat()
                        bench["timerange"] = timerange
                        bench["method"] = (
                            "cap sobre wallet disponible (10000*0.99) con misma formula "
                            "de riesgo; open primera eval -> open ultima 1h (cierre "
                            "motor al ultimo open); fees fuera del principal; "
                            "aproximado, motor usa detalle 5m")
                    except Exception as exc:
                        entry.update({"status": "FAILED", "evaluation_status": "FAILED",
                                      "error": f"benchmark: {type(exc).__name__}: {exc}",
                                      "summary": summary})
                        failed += 1
                        results.append(entry)
                        continue
                    entry.update({"status": "SUCCEEDED", "summary": summary,
                                  "benchmark_buyhold": bench,
                                  "benchmark_cash": {"return": 0.0}})
                    if int(summary.get("trades", 0)) > 0:
                        entry["evaluation_status"] = "SUCCEEDED"
                    else:
                        entry["evaluation_status"] = "INCONCLUSIVE"
                        entry["evaluation_reason"] = (
                            "cero trades: nativo completo pero no evaluable, sin exito economico")
                    results.append(entry)
                if aborted:
                    break
            # Estado de ejecucion nativa distinto del gate de evaluacion.
            scoped = [r for r in results if r.get("status") in ("SUCCEEDED", "INCONCLUSIVE")]
            tradeable = [r for r in scoped
                         if r.get("status") == "SUCCEEDED"
                         and int((r.get("summary") or {}).get("trades", 0)) > 0]
            if failed > 0:
                evaluation = "FAILED"
            elif not scoped or not tradeable:
                evaluation = "INCONCLUSIVE"
            elif len(tradeable) == len(scoped):
                evaluation = "SUCCEEDED"
            else:
                evaluation = "PARTIAL"
            status = "SUCCEEDED" if failed == 0 else "FAILED"
            launch._atomic_replace(manifest_path, {**base, "status": status,
                                                   "evaluation_status": evaluation,
                                                   "finished_at": _utcnow_iso(),
                                                   "results": results,
                                                   "session_dir": str(session_dir)})
            print(str(manifest_path))
            return 0 if (failed == 0 and evaluation == "SUCCEEDED") else 1
    except RuntimeError as exc:
        print(f"backtest: {exc}", file=sys.stderr)
        return 1


def _timerange_for_eval(eval_start_dt, end_exclusive_dt) -> str:
    return _timerange_fmt(eval_start_dt, end_exclusive_dt)


def _verdict_status(verdict: str) -> str:
    """Mapeo uniforme veredicto -> estado terminal: PASS/FAIL/resto."""
    return {"PASS": "SUCCEEDED", "FAIL": "FAILED"}.get(verdict, "INCONCLUSIVE")


def _parse_lookahead_csv(csv_path: Path) -> tuple:
    import csv

    try:
        text = csv_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"CSV ilegible: {exc}"
    try:
        rows = list(csv.DictReader(text.splitlines()))
    except Exception as exc:
        return None, f"CSV malformado: {exc}"
    if not rows:
        return None, "CSV sin filas (muestra no analizada)"
    target = None
    for row in rows:
        if str(row.get("strategy") or "") == STRATEGY_NAME:
            target = row
            break
    if target is None:
        return None, "CSV sin fila de SmaCrossBaseline"
    expected = {"filename", "strategy", "has_bias", "total_signals",
                "biased_entry_signals", "biased_exit_signals", "biased_indicators"}
    if not expected.issubset(set(target.keys())):
        return None, f"CSV con schema inesperado ({sorted(target.keys())})"

    def _as_bool(raw) -> bool | None:
        text_value = str(raw or "").strip().lower()
        if text_value in ("true", "1"):
            return True
        if text_value in ("false", "0"):
            return False
        return None

    def _as_int(raw):
        import re

        text_value = str(raw or "").strip()
        if not re.fullmatch(r"\d+", text_value):
            return None
        try:
            return int(text_value)
        except (TypeError, ValueError):
            return None

    has_bias = _as_bool(target.get("has_bias"))
    total = _as_int(target.get("total_signals"))
    biased_entry = _as_int(target.get("biased_entry_signals"))
    biased_exit = _as_int(target.get("biased_exit_signals"))
    indicators_raw = str(target.get("biased_indicators") or "").strip()
    indicators = [s.strip() for s in indicators_raw.split(",") if s.strip()]
    if has_bias is None or total is None or biased_entry is None or biased_exit is None:
        return None, "CSV con tipos inesperados"
    return {
        "has_bias": has_bias,
        "total_signals": total,
        "biased_entry_signals": biased_entry,
        "biased_exit_signals": biased_exit,
        "biased_indicators": indicators,
    }, None


def _ref_trades(native_dir: Path) -> tuple:
    summary, error = _summarize_native(native_dir)
    if error or summary is None:
        return None, error or "sin resumen de referencia"
    trades = summary.get("trade_list") or []
    if not trades:
        return {"count": 0, "analyzable": 0, "excluded": [],
                "has_enter_cross": False, "has_exit_cross": False, "trades": []}, None
    has_enter = False
    has_exit = False
    excluded: list = []
    for trade in trades:
        enter_tag = str(trade.get("enter_tag") or "")
        exit_tag = str(trade.get("exit_tag") or "")
        exit_reason = str(trade.get("exit_reason") or "")
        if exit_reason == "force_exit":
            excluded.append({"exit_reason": exit_reason})
            continue
        if enter_tag == "sma_bull":
            has_enter = True
        if exit_tag == "sma_bear" or exit_reason in ("sma_bear", "exit_signal"):
            has_exit = True
    return {"count": len(trades), "analyzable": len(trades) - len(excluded),
            "excluded": excluded, "has_enter_cross": has_enter,
            "has_exit_cross": has_exit, "trades": trades}, None


def _lookahead_coverage(parsed_count, trades) -> bool:
    """El CSV lookahead cubre la referencia analizable (sin force_exit terminal).

    Solo el exit_reason EXACTO 'force_exit' excluye; el conteo debe ser un
    entero >= 0 exacto (fracciones y negativos rechazan).
    """
    if isinstance(parsed_count, bool):
        return False
    if isinstance(parsed_count, int):
        count = parsed_count
    elif isinstance(parsed_count, float):
        if not parsed_count.is_integer():
            return False
        count = int(parsed_count)
    else:
        return False
    if count < 0:
        return False
    try:
        expected = sum(1 for trade in (trades or [])
                       if str((trade or {}).get("exit_reason") or "") != "force_exit")
    except (TypeError, ValueError):
        return False
    return count == expected


def _own_sma_gate(seg_1h_path: Path) -> dict:
    """Compara la estrategia real en prefijos de 51/100/200/400 velas.

    La referencia y los prefijos usan ``SmaCrossBaseline.populate_*``. En cada
    evento con 400 velas de contexto deben coincidir SMA y señales booleanas.
    """
    import pandas as pd

    from strategies.baseline.SmaCrossBaseline import SmaCrossBaseline

    frame = pd.read_feather(str(seg_1h_path))
    frame = frame.sort_values("date").reset_index(drop=True)

    def _run(piece):
        cfg = {"stake_currency": "USDT", "dry_run_wallet": 10000.0,
               "candle_type_def": "spot", "runmode": "backtest"}
        strat = SmaCrossBaseline(config=dict(cfg))
        out = strat.populate_indicators(piece.copy(), {"pair": TRAIN_PAIR})
        out = strat.populate_entry_trend(out, {"pair": TRAIN_PAIR})
        out = strat.populate_exit_trend(out, {"pair": TRAIN_PAIR})
        return out

    try:
        ref = _run(frame)
    except Exception as exc:
        return {"verdict": "INCONCLUSIVE", "reason": f"estrategia no ejecutable: {exc}"}
    events: list = []
    seen_enter = False
    seen_exit = False
    for pos in range(len(ref)):
        if pos < RECURSIVE_CONTEXT:
            continue
        try:
            enter = int(ref["enter_long"].iloc[pos]) == 1
            exit_ = int(ref["exit_long"].iloc[pos]) == 1
        except Exception:
            return {"verdict": "FAIL", "reason": f"senales ilegibles en {pos}"}
        if enter or exit_:
            events.append(pos)
            seen_enter = seen_enter or enter
            seen_exit = seen_exit or exit_
    if not (seen_enter and seen_exit):
        return {"verdict": "INCONCLUSIVE",
                "reason": "ambos cruces no cubiertos con contexto 400",
                "checked_events": len(events)}
    for pos in events:
        for width in STARTUP_LIST:
            piece = frame.iloc[pos - width + 1:pos + 1].copy().reset_index(drop=True)
            try:
                got = _run(piece)
            except Exception as exc:
                return {"verdict": "FAIL",
                        "reason": f"recorte {width} no ejecutable en {pos}: {exc}"}
            for col in ("sma20", "sma50"):
                for offset, label in ((0, "actual"), (1, "anterior")):
                    try:
                        ref_value = float(ref[col].iloc[pos - offset])
                        got_value = float(got[col].iloc[-1 - offset])
                    except Exception as exc:
                        return {"verdict": "FAIL",
                                "reason": f"SMA ilegible {col} {label} en {pos}: {exc}"}
                    import math

                    if not (math.isfinite(ref_value) and math.isfinite(got_value)):
                        return {"verdict": "FAIL",
                                "reason": f"SMA no finita {col} {label} en {pos}"}
                    tol = SMA_TOL * max(1.0, abs(ref_value))
                    if abs(got_value - ref_value) > tol:
                        return {"verdict": "FAIL",
                                "reason": f"SMA fuera de tolerancia {col} {label} en {pos}",
                                "expected": ref_value, "got": got_value}
            try:
                for col in ("enter_long", "exit_long"):
                    if int(got[col].iloc[-1]) != int(ref[col].iloc[pos]):
                        return {"verdict": "FAIL",
                                "reason": f"senal distinta {col} en {pos} (recorte {width})"}
            except Exception as exc:
                return {"verdict": "FAIL", "reason": f"senal ilegible en {pos}: {exc}"}
    return {"verdict": "PASS",
            "reason": "SMA y senales exactas en eventos con contexto 400",
            "checked_events": len(events)}


def cmd_bias(snapshot_ref: str) -> int:
    try:
        manifest_in = _load_input()
        current = _verify_current_against_input(manifest_in)
    except Exception as exc:
        print(f"bias: input/codigo no valido: {exc}", file=sys.stderr)
        return 2
    try:
        snap_manifest, snap_dir, snap_path = _load_eval_snapshot(snapshot_ref)
    except FileNotFoundError:
        print(f"bias: snapshot inexistente: {snapshot_ref}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"bias: snapshot invalido: {exc}", file=sys.stderr)
        return 1
    segments_meta = snap_manifest.get("segments_meta") or []
    target = longest_segment_meta(segments_meta)
    research = Path(CONTAINER_RESEARCH)
    sessions = research / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    try:
        with _locked():
            session_id = uuid.uuid4().hex
            session_dir = sessions / f"bias-{_slug()}-{session_id[:4]}"
            session_dir.mkdir(parents=True, exist_ok=False)
            manifest_path = session_dir / "session.json"
            gate_path = session_dir / "gate.json"
            base = {
                "kind": BIAS_KIND,
                "status": "RUNNING",
                "session_id": session_id,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "snapshot_id": snap_manifest.get("snapshot_id"),
                "snapshot_manifest": str(snap_path),
                "commit": manifest_in.get("commit"),
                "image_ref": manifest_in.get("image_ref"),
                "target": target,
                **current,
            }
            launch._atomic_create_new(manifest_path, {**base})
            if target is None or not target.get("start") or not target.get("end_exclusive"):
                verdict = {"verdict": "INCONCLUSIVE", "reason": "sin segmento objetivo"}
                launch._atomic_create_new(gate_path, {**base, "status": "INCONCLUSIVE",
                                                      **verdict, "finished_at": _utcnow_iso()})
                launch._atomic_replace(manifest_path, {**base, "status": "INCONCLUSIVE",
                                                       "finished_at": _utcnow_iso(), **verdict})
                return 1
            if int(target.get("length", 0)) < MIN_RECURSIVE_BARS:
                # El análisis recursive exige 1.000 velas; menos es inconcluyente.
                verdict = {"verdict": "INCONCLUSIVE",
                           "reason": f"cobertura insuficiente (<{MIN_RECURSIVE_BARS} velas)"}
                launch._atomic_create_new(gate_path, {**base, "status": "INCONCLUSIVE",
                                                      **verdict, "finished_at": _utcnow_iso()})
                launch._atomic_replace(manifest_path, {**base, "status": "INCONCLUSIVE",
                                                       "finished_at": _utcnow_iso(), **verdict})
                return 1
            start_dt = datetime.fromisoformat(str(target["start"]).replace("Z", "+00:00"))
            eval_dt = datetime.fromisoformat(
                str(target.get("eval_start") or target["start"]).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(
                str(target["end_exclusive"]).replace("Z", "+00:00"))
            timerange = _timerange_fmt(eval_dt, end_dt)
            seg_dir = snap_dir / str(target.get("seg_dir"))
            # 1) Backtest de referencia en el segmento más largo con fee base.
            ref_dir = session_dir / "ref"
            ref_native = ref_dir / "native"
            ref_user = ref_dir / "user_data"
            ref_native.mkdir(parents=True, exist_ok=False)
            ref_user.mkdir(parents=True, exist_ok=False)
            ref_argv = _backtest_argv(seg_dir, ref_user, timerange, BASE_FEE, ref_native)
            ref_log = ref_dir / "backtest.log"
            try:
                ref_rc, ref_timeout = _run_streaming(ref_argv, BIAS_TIMEOUT_S, ref_log)
                ref_exec_error = None
            except OSError as exc:
                ref_rc, ref_timeout = None, False
                ref_exec_error = f"{type(exc).__name__}: {exc}"
            ref_info, ref_error = (None, None)
            if ref_exec_error is None and not ref_timeout and ref_rc == 0:
                ref_info, ref_error = _ref_trades(ref_native)
            if ref_exec_error or ref_timeout or ref_rc != 0 or ref_error or not ref_info:
                reason = ref_exec_error or ref_error or f"referencia no concluyo (rc={ref_rc})"
                verdict = {"verdict": "INCONCLUSIVE", "reason": reason,
                           "evidence": {"ref_argv": [str(a) for a in ref_argv],
                                        "ref_rc": ref_rc, "ref_timeout": ref_timeout}}
                launch._atomic_create_new(gate_path, {**base, "status": "INCONCLUSIVE",
                                                      **verdict, "finished_at": _utcnow_iso()})
                launch._atomic_replace(manifest_path, {**base, "status": "INCONCLUSIVE",
                                                       "finished_at": _utcnow_iso(), **verdict})
                return 1
            if int(ref_info.get("analyzable", ref_info.get("count", 0))) < MIN_TRADES_LOOKAHEAD:
                verdict = {"verdict": "INCONCLUSIVE",
                           "reason": f"solo {ref_info.get('analyzable')} analizables (<5)",
                           "evidence": {"ref_trades": ref_info.get("count"),
                                        "analyzable": ref_info.get("analyzable")}}
                launch._atomic_create_new(gate_path, {**base, "status": "INCONCLUSIVE",
                                                      **verdict, "finished_at": _utcnow_iso()})
                launch._atomic_replace(manifest_path, {**base, "status": "INCONCLUSIVE",
                                                       "finished_at": _utcnow_iso(), **verdict})
                return 1
            if not (ref_info.get("has_enter_cross") and ref_info.get("has_exit_cross")):
                verdict = {"verdict": "INCONCLUSIVE",
                           "reason": "sin cobertura de ambos cruces en trades reales "
                                     "(stop solo no acredita salida)"}
                launch._atomic_create_new(gate_path, {**base, "status": "INCONCLUSIVE",
                                                      **verdict, "finished_at": _utcnow_iso()})
                launch._atomic_replace(manifest_path, {**base, "status": "INCONCLUSIVE",
                                                       "finished_at": _utcnow_iso(), **verdict})
                return 1
            targeted = int(ref_info["count"]) + 1
            # 2) Análisis lookahead nativo sobre el mismo rango efectivo.
            look_dir = session_dir / "lookahead"
            look_dir.mkdir(parents=True, exist_ok=False)
            look_user = look_dir / "user_data"
            look_user.mkdir(parents=True, exist_ok=False)
            look_csv = look_dir / "lookahead.csv"
            look_argv = [
                "freqtrade", "lookahead-analysis",
                "--config", CONTAINER_CONFIG,
                "--datadir", str(seg_dir),
                "--userdir", str(look_user),
                "--strategy", STRATEGY_NAME,
                "--strategy-path", CONTAINER_STRATEGY_PATH,
                "--timeframe", TRAIN_TIMEFRAME_1H,
                "--timeframe-detail", TRAIN_TIMEFRAME_5M,
                "--timerange", timerange,
                "--minimum-trade-amount", str(MIN_TRADES_LOOKAHEAD),
                "--targeted-trade-amount", str(targeted),
                "--lookahead-analysis-exportfilename", str(look_csv),
            ]
            look_log = look_dir / "lookahead.log"
            try:
                look_rc, look_timeout = _run_streaming(look_argv, BIAS_TIMEOUT_S, look_log)
                look_exec_error = None
            except OSError as exc:
                look_rc, look_timeout = None, False
                look_exec_error = f"{type(exc).__name__}: {exc}"
            # 3) Análisis recursive nativo más el gate independiente de estrategia.
            # Si lookahead fallo a nivel ejecucion no se lanza otro nativo que
            # pueda solaparse con un proceso sin terminar confirmado.
            rec_argv: list = []
            rec_log = session_dir / "recursive.log"
            rec_rc, rec_timeout, rec_exec_error = None, False, None
            if look_exec_error is None:
                rec_dir = session_dir / "recursive"
                rec_dir.mkdir(parents=True, exist_ok=False)
                rec_user = rec_dir / "user_data"
                rec_user.mkdir(parents=True, exist_ok=False)
                rec_argv = [
                    "freqtrade", "recursive-analysis",
                    "--config", CONTAINER_CONFIG,
                    "--datadir", str(seg_dir),
                    "--userdir", str(rec_user),
                    "--strategy", STRATEGY_NAME,
                    "--strategy-path", CONTAINER_STRATEGY_PATH,
                    "--timeframe", TRAIN_TIMEFRAME_1H,
                    "--timerange", timerange,
                    "--startup-candle", *[str(s) for s in STARTUP_LIST],
                ]
                rec_log = rec_dir / "recursive.log"
                try:
                    rec_rc, rec_timeout = _run_streaming(rec_argv, BIAS_TIMEOUT_S, rec_log)
                except OSError as exc:
                    rec_exec_error = f"{type(exc).__name__}: {exc}"
            own = _own_sma_gate(seg_dir / _pair_file(TRAIN_PAIR, TRAIN_TIMEFRAME_1H))
            look_parsed, look_error = (None, None)
            if look_exec_error is None and not look_timeout and look_rc == 0:
                look_parsed, look_error = _parse_lookahead_csv(look_csv)
            # Orden formal del gate tras parse CSV: FAIL probado (own y sesgo
            # confirmado) antes que cualquier INCONCLUSIVE; luego errores de
            # ejecucion, timeouts, salidas nativas y minimos de cobertura.
            if own.get("verdict") == "FAIL":
                verdict = {"verdict": "FAIL",
                           "reason": own.get("reason", ""),
                           "evidence": {"own_sma": own}}
            elif look_parsed and (look_parsed["has_bias"]
                    or look_parsed["biased_entry_signals"] > 0
                    or look_parsed["biased_exit_signals"] > 0
                    or look_parsed["biased_indicators"]):
                verdict = {"verdict": "FAIL", "reason": "lookahead marca sesgo",
                           "evidence": {"lookahead": look_parsed,
                                        "analyzable": ref_info.get("analyzable"),
                                        "ref_trades": ref_info.get("count")}}
            elif look_exec_error or rec_exec_error:
                verdict = {"verdict": "INCONCLUSIVE",
                           "reason": look_exec_error or rec_exec_error}
            elif look_timeout or rec_timeout:
                verdict = {"verdict": "INCONCLUSIVE", "reason": "timeout en analisis nativo"}
            elif look_rc != 0:
                tail = _read_tail(look_log, 8192).lower()
                if ("minimum_trade_amount" in tail or "too few" in tail
                        or "insufficient" in tail or "no data" in tail):
                    verdict = {"verdict": "INCONCLUSIVE",
                               "reason": "lookahead no concluyo por cobertura"}
                else:
                    verdict = {"verdict": "FAIL",
                               "reason": f"lookahead fallo (rc={look_rc})"}
            elif look_error or not look_parsed:
                verdict = {"verdict": "INCONCLUSIVE",
                           "reason": look_error or "sin informe lookahead"}
            elif int(look_parsed["total_signals"]) < MIN_TRADES_LOOKAHEAD:
                verdict = {"verdict": "INCONCLUSIVE",
                           "reason": f"lookahead solo {look_parsed['total_signals']} trades"}
            elif not _lookahead_coverage(look_parsed["total_signals"], ref_info.get("trades")):
                verdict = {"verdict": "INCONCLUSIVE",
                           "reason": "lookahead incompleto frente a la referencia analizable",
                           "evidence": {"lookahead_total": look_parsed["total_signals"],
                                        "analyzable": ref_info.get("analyzable")}}
            elif rec_rc != 0:
                tail = _read_tail(rec_log, 8192).lower()
                # Warnings cosméticos no deciden el gate; sí cobertura/errores.
                if ("insufficient" in tail or "too few" in tail or "no data" in tail):
                    verdict = {"verdict": "INCONCLUSIVE",
                               "reason": "recursive no concluyo por cobertura"}
                else:
                    verdict = {"verdict": "FAIL",
                               "reason": f"recursive fallo (rc={rec_rc})"}
            elif own.get("verdict") != "PASS":
                verdict = {"verdict": own.get("verdict", "INCONCLUSIVE"),
                           "reason": own.get("reason", ""),
                           "evidence": {"own_sma": own}}
            else:
                verdict = {"verdict": "PASS",
                           "reason": "lookahead/recursive sin sesgo + SMA exactas"}
            gate = {**base, "status": _verdict_status(verdict["verdict"]), **verdict,
                    "lookahead": {"argv": [str(a) for a in look_argv],
                                  "exit_code": look_rc, "timed_out": look_timeout,
                                  "parsed": look_parsed, "parse_error": look_error},
                    "recursive": {"argv": [str(a) for a in rec_argv],
                                  "exit_code": rec_rc, "timed_out": rec_timeout},
                    "own_sma": own,
                    "reference": {"argv": [str(a) for a in ref_argv],
                                  "trades": ref_info.get("count"),
                                  "analyzable": ref_info.get("analyzable"),
                                  "has_enter_cross": ref_info.get("has_enter_cross"),
                                  "has_exit_cross": ref_info.get("has_exit_cross")},
                    "finished_at": _utcnow_iso()}
            launch._atomic_create_new(gate_path, gate)
            term = _verdict_status(verdict["verdict"])
            launch._atomic_replace(manifest_path, {**base, "status": term,
                                                   "finished_at": _utcnow_iso(), **verdict})
            print(str(gate_path))
            return 0 if verdict["verdict"] == "PASS" else 1
    except RuntimeError as exc:
        print(f"bias: {exc}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="operations.research")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="preflight host + input congelado research")
    prep.add_argument("--code-root", default=os.environ.get("LAB_CODE_ROOT", ""))
    prep.add_argument("--storage-root", default=os.environ.get("LAB_STORAGE_ROOT", ""))
    prep.add_argument("--image", default=launch.PINNED_IMAGE)
    sub.add_parser("download", help="descarga TRAIN 5m en raiz unica (contenedor)")
    snap = sub.add_parser("snapshot", help="valida y congela snapshot inmutable")
    snap.add_argument("--download", required=True,
                      help="manifiesto .json dentro de downloads (nombre simple)")
    back = sub.add_parser("backtest", help="backtests por segmento y friccion")
    back.add_argument("--snapshot", required=True,
                      help="manifiesto .json dentro de snapshots (nombre simple)")
    bias = sub.add_parser("bias", help="lookahead/recursive + gate JSON")
    bias.add_argument("--snapshot", required=True,
                      help="manifiesto .json dentro de snapshots (nombre simple)")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        if not args.code_root or not args.storage_root:
            print("prepare exige --code-root y --storage-root (o LAB_CODE_ROOT/LAB_STORAGE_ROOT)",
                  file=sys.stderr)
            return 2
        print(prepare_research(args.code_root, args.storage_root, args.image))
        return 0
    if args.command == "download":
        return cmd_download()
    if args.command == "snapshot":
        return cmd_snapshot(args.download)
    if args.command == "backtest":
        return cmd_backtest(args.snapshot)
    if args.command == "bias":
        return cmd_bias(args.snapshot)
    parser.error("comando desconocido")
    return 2


if __name__ == "__main__":
    sys.exit(main())
