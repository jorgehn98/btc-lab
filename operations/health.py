"""Monitor T03: salud del smoke desde el host con stdlib.

Solo vale el heartbeat auténtico `freqtrade.worker ... Bot heartbeat ...
state=RUNNING` con timestamp `YYYY-MM-DD HH:MM:SS,mmm` (UTC), posterior al
StartedAt del contenedor y no futuro. Sin heartbeat fresco no hay healthy:
dato ausente es UNKNOWN (no-cero), nunca healthy. Sin reinicios automáticos.

Salida JSON atómica bajo lock `flock`; lectura acotada del log y timeout
en las consultas a Docker.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"
    r" - freqtrade\.worker - INFO - Bot heartbeat\. PID=\d+.*state=['\"]?RUNNING['\"]?"
)
HEARTBEAT_MAX_AGE_S = 180
STARTUP_GRACE_S = 300
MIN_FREE_BYTES = 5 * 1024**3
LOG_TAIL_BYTES = 256 * 1024
DOCKER_TIMEOUT_S = 15


def _parse_docker_ts(raw: str):
    """StartedAt de `docker inspect` a datetime UTC; None si ilegible."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = re.match(
        r"^(?P<dt>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
        r"(?P<frac>\.\d+)?(?P<tz>[+-]\d{2}:?\d{2})?$", text,
    )
    if not match:
        return None
    frac = (match.group("frac") or "")[1:]
    frac = (frac + "000000")[:6]
    normalized = f"{match.group('dt')}.{frac}{match.group('tz') or '+00:00'}"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_heartbeat_ts(raw: str):
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _latest_heartbeat(log_text, now, started_at):
    """Devuelve (ts|None, problema|None) del heartbeat auténtico más reciente."""
    if not isinstance(log_text, str) or not log_text:
        return None, "sin-heartbeat"
    tail = log_text[-1_048_576:]
    latest = None
    saw_future = False
    saw_before_start = False
    for line in tail.splitlines():
        match = HEARTBEAT_RE.match(line.strip())
        if not match:
            continue
        ts = _parse_heartbeat_ts(match.group("ts"))
        if ts is None:
            continue
        if ts > now:
            saw_future = True
            continue
        if started_at is not None and ts < started_at:
            saw_before_start = True
            continue
        if latest is None or ts > latest:
            latest = ts
    if latest is not None:
        return latest, None
    if saw_before_start:
        return None, "heartbeat-anterior-a-arranque"
    if saw_future:
        return None, "heartbeat-futuro"
    return None, "sin-heartbeat"


def evaluate_health(inspect_data, log_text, free_bytes, now) -> dict:
    """{"status": healthy|unhealthy|unknown, "reasons": [...]}.

    No-cero salvo healthy (lo decide el CLI con el exit code).
    """
    reasons: list = []
    hard = False
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    started_at = None
    if not isinstance(inspect_data, dict):
        reasons.append("inspect-ausente")
    else:
        state = inspect_data.get("State")
        if not isinstance(state, dict):
            reasons.append("inspect-sin-Estado")
            hard = True
        else:
            if state.get("Status") != "running" or state.get("Running") is not True:
                reasons.append(f"contenedor-no-running: Status={state.get('Status')!r}")
                hard = True
            oom = state.get("OOMKilled")
            if oom is True:
                reasons.append("oom-killed")
                hard = True
            elif oom is not False:
                reasons.append(f"oom-desconocido: {oom!r}")
                hard = True
            started_at = _parse_docker_ts(state.get("StartedAt"))
            if started_at is None:
                reasons.append("started-at-ilegible")
        restarts = inspect_data.get("RestartCount")
        if restarts is None:
            reasons.append("restarts-desconocido")
        elif isinstance(restarts, bool) or not isinstance(restarts, int):
            reasons.append(f"restarts-ilegible: {restarts!r}")
            hard = True
        elif restarts != 0:
            reasons.append(f"restarts={restarts}")
            hard = True

    if free_bytes is None:
        reasons.append("espacio-desconocido")
    elif isinstance(free_bytes, bool) or not isinstance(free_bytes, (int, float)):
        reasons.append(f"espacio-ilegible: {free_bytes!r}")
        hard = True
    elif not math.isfinite(free_bytes):
        reasons.append(f"espacio-no-finito: {free_bytes!r}")
        hard = True
    elif free_bytes < MIN_FREE_BYTES:
        reasons.append(f"disco-insuficiente: {free_bytes} B libres")
        hard = True

    heartbeat_at, problem = _latest_heartbeat(log_text, now, started_at)
    if heartbeat_at is not None:
        age = (now - heartbeat_at).total_seconds()
        if age > HEARTBEAT_MAX_AGE_S:
            reasons.append(f"heartbeat-obsoleto: {age:.0f} s")
            hard = True
    elif problem == "heartbeat-anterior-a-arranque":
        reasons.append(problem)
        hard = True
    else:
        if (
            started_at is not None
            and (now - started_at).total_seconds() < STARTUP_GRACE_S
        ):
            reasons.append(f"arranque-sin-heartbeat: {problem}")
        else:
            reasons.append(problem or "sin-heartbeat")

    if not reasons:
        return {"status": "healthy", "reasons": []}
    return {"status": "unhealthy" if hard else "unknown", "reasons": reasons}


def _read_tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _docker_inspect(container: str, timeout: int = DOCKER_TIMEOUT_S):
    try:
        proc = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def main(argv=None) -> int:
    default_storage = os.environ.get("LAB_STORAGE", os.environ.get("LAB_STORAGE_ROOT", ""))
    parser = argparse.ArgumentParser(prog="operations.health")
    parser.add_argument("--container", default="btc-lab-smoke-1")
    parser.add_argument("--logfile", default=str(Path(default_storage) / "runtime/smoke/freqtrade.log") if default_storage else "")
    parser.add_argument("--output", default=str(Path(default_storage) / "health.json") if default_storage else "")
    parser.add_argument("--lock", default=str(Path(default_storage) / "health.lock") if default_storage else "")
    args = parser.parse_args(argv)
    if not args.logfile or not args.output or not args.lock:
        print("health exige --logfile, --output y --lock (o LAB_STORAGE/LAB_STORAGE_ROOT)",
              file=sys.stderr)
        return 2

    output = Path(args.output)
    try:
        lock_fd = os.open(args.lock, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        print(f"health: sin lock: {exc}", file=sys.stderr)
        return 2
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("health: otra ejecución en curso", file=sys.stderr)
            return 1
        try:
            inspect_data = _docker_inspect(args.container)
            log_text = _read_tail(Path(args.logfile))
            try:
                free_bytes = shutil.disk_usage(str(output.parent)).free
            except OSError:
                free_bytes = None
            now = datetime.now(timezone.utc)
            result = evaluate_health(inspect_data, log_text, free_bytes, now)
            _atomic_write_json(output, {
                "checked_at": now.isoformat().replace("+00:00", "Z"),
                "service": args.container,
                **result,
            })
            return 0 if result["status"] == "healthy" else 1
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
