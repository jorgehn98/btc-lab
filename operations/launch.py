"""Launcher T03: subcomandos cerrados `version` / `prepare-smoke` / `smoke`.

`prepare-smoke` corre en el host: preflight con worktree Git limpio real,
`docker image inspect` real (digest fijado) y hashes de config/estrategia/
launcher/health; devuelve la ruta del manifiesto único (sin alias mutable).
`version` y `smoke` corren en el contenedor. `smoke` no acepta roots por
flag ni entorno: usa siempre CONTAINER_CODE/CONTAINER_STORAGE y `run_smoke`
rechaza cualquier root que no resuelva a esas constantes antes de leer
inputs. Lee el manifiesto explícito que Compose monta en
`/lab-storage/active-input.json` desde la ruta única seleccionada por
`LAB_SMOKE_INPUT`; esa ruta se fija antes del `up` y el fichero se monta
en solo lectura.

Solo stdlib, sin `shell=True`, sin flags arbitrarios ni segunda config.
Las señales SIGTERM/SIGINT se propagan al hijo (sin Freqtrade huérfano).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PINNED_IMAGE = (
    "freqtradeorg/freqtrade:2026.8@sha256:"
    "4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4"
)

CONTAINER_CODE = "/opt/btc-lab"
CONTAINER_STORAGE = "/lab-storage"
CONTAINER_USERDATA = "/freqtrade/user_data"

STRATEGY_NAME = "NoTradeSmoke"
CONFIG_REL = Path("configs/smoke.json")
STRATEGY_REL = Path("strategies/smoke/NoTradeSmoke.py")
STRATEGY_DIR_REL = Path("strategies/smoke")
INPUTS_SUBDIR = Path("runs/inputs")
CONTAINER_ACTIVE_INPUT = "/lab-storage/active-input.json"
CONTAINER_ACTIVE_BASELINE_INPUT = "/lab-storage/active-baseline-input.json"
RUNS_SUBDIR = Path("runs")

BASELINE_STRATEGY_NAME = "SmaCrossBaseline"
BASELINE_CONFIG_REL = Path("configs/baseline.json")
BASELINE_STRATEGY_REL = Path("strategies/baseline/SmaCrossBaseline.py")
BASELINE_STRATEGY_DIR_REL = Path("strategies/baseline")
BASELINE_MARKET_REL = Path("market/train.py")
BASELINE_RESEARCH_REL = Path("operations/research.py")
BASELINE_INPUT_KIND = "btc-lab-baseline-input"
BASELINE_RUN_KIND = "btc-lab-baseline-run"
BASELINE_PROFILE = "baseline"
BASELINE_DB_FILENAME = "tradesv3.baseline.dryrun.sqlite"

TOP_REQUIRED = frozenset({
    "exchange", "pairlists", "trading_mode", "dry_run", "dry_run_wallet",
    "stake_currency", "stake_amount", "max_open_trades", "timeframe",
    "strategy", "api_server", "telegram", "fee",
})
# El validador local permite omitir estas opciones porque los fixtures de test
# no las traen; el schema de la imagen fijada solo se comprueba en su runtime.
TOP_ALLOWED = TOP_REQUIRED | {"entry_pricing", "exit_pricing", "initial_state", "internals"}
PRICING_PIN = {
    "price_side": "same",
    "use_order_book": False,
    "order_book_top": 1,
    "price_last_balance": 0.0,
}
BASELINE_PRICING_PIN = {
    "price_side": "other",
    "use_order_book": False,
    "order_book_top": 1,
    "price_last_balance": 0.0,
}
INITIAL_STATE_PIN = "running"
INTERNALS_PIN = {"heartbeat_interval": 60}
# El schema de la imagen fijada exige estas claves aunque el servicio esté
# desactivado; se fijan vacías/cerradas (sin credenciales, sin escucha). La
# forma corta {"enabled": False} solo se permite para fixtures RED y no
# demuestra que la configuración pase el schema real del runtime.
API_PIN = {
    "enabled": False,
    "listen_ip_address": "127.0.0.1",
    "listen_port": 8080,
    "username": "",
    "password": "",
    # Placeholder inoperativo y público (servidor desactivado, sin puertos):
    # el schema exige minLength 32. No es un secreto; validate_config lo
    # fija exacto y rechaza cualquier enabled=true.
    "jwt_secret_key": "DISABLED-NO-API-SERVER-PLACEHOLDER-0000",
}
TELEGRAM_PIN = {"enabled": False, "token": "", "chat_id": ""}
_SERVICE_FIXTURE = {"enabled": False}
EXCHANGE_ALLOWED = frozenset({
    "name", "key", "secret", "password", "api_key", "api_secret",
    "pair_whitelist", "pair_blacklist",
})
EXCHANGE_SECRETS = ("key", "api_key", "secret", "api_secret", "password")

GIT_TIMEOUT_S = 30
DOCKER_TIMEOUT_S = 60

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _validate_common(config: dict, environ) -> None:
    """Checks compartidos smoke/baseline; ValueError si no."""
    for key in environ:
        if key.startswith("FREQTRADE__"):
            raise ValueError(f"override de entorno no permitido: {key}")
    if not isinstance(config, dict):
        raise ValueError("la configuración debe ser un objeto JSON")
    extra = set(config) - TOP_ALLOWED
    if extra:
        raise ValueError(f"claves adicionales no permitidas: {sorted(extra)}")
    missing = TOP_REQUIRED - set(config)
    if missing:
        raise ValueError(f"claves obligatorias ausentes: {sorted(missing)}")
    if "initial_state" in config and config["initial_state"] != INITIAL_STATE_PIN:
        raise ValueError(f"initial_state debe ser {INITIAL_STATE_PIN}")
    if "internals" in config and config["internals"] != INTERNALS_PIN:
        raise ValueError(f"internals debe ser exactamente {INTERNALS_PIN}")

    exchange = config["exchange"]
    if not isinstance(exchange, dict):
        raise ValueError("exchange debe ser un objeto")
    extra_ex = set(exchange) - EXCHANGE_ALLOWED
    if extra_ex:
        raise ValueError(f"claves de exchange no permitidas: {sorted(extra_ex)}")
    if exchange.get("name") != "binance":
        raise ValueError("solo se permite exchange binance")
    if exchange.get("pair_whitelist") != ["BTC/USDT"]:
        raise ValueError("pair_whitelist debe ser exactamente [BTC/USDT]")
    if exchange.get("pair_blacklist") != []:
        raise ValueError("pair_blacklist debe ser []")
    for alias in EXCHANGE_SECRETS:
        if alias in exchange and exchange[alias] != "":
            raise ValueError(f"credencial no permitida en {alias!r}: debe ser vacía")

    if config["pairlists"] != [{"method": "StaticPairList"}]:
        raise ValueError("pairlists debe ser exactamente [StaticPairList]")
    if config["trading_mode"] != "spot":
        raise ValueError("trading_mode debe ser spot")
    if config["dry_run"] is not True:
        raise ValueError("dry_run debe ser true booleano")
    if config["dry_run_wallet"] != 10000:
        raise ValueError("dry_run_wallet debe ser 10000")
    if config["stake_currency"] != "USDT":
        raise ValueError("stake_currency debe ser USDT")
    if config["stake_amount"] != 1000:
        raise ValueError("stake_amount debe ser 1000")
    if config["max_open_trades"] != 1:
        raise ValueError("max_open_trades debe ser 1")
    if config["api_server"] not in (_SERVICE_FIXTURE, API_PIN):
        raise ValueError("api_server debe estar desactivado con valores fijados")
    if config["telegram"] not in (_SERVICE_FIXTURE, TELEGRAM_PIN):
        raise ValueError("telegram debe estar desactivado con valores fijados")
    if config["fee"] != 0.001:
        raise ValueError("fee debe ser 0.001")


def validate_config(config: dict, environ) -> None:
    """Acepta solo la config canónica BTC spot dry-run; ValueError si no."""
    _validate_common(config, environ)
    for pricing in ("entry_pricing", "exit_pricing"):
        if pricing in config and config[pricing] != PRICING_PIN:
            raise ValueError(f"{pricing} debe ser exactamente {PRICING_PIN}")
    if config["timeframe"] != "5m":
        raise ValueError("timeframe debe ser 5m")
    if config["strategy"] != STRATEGY_NAME:
        raise ValueError(f"strategy debe ser {STRATEGY_NAME}")


def file_hash(path) -> str:
    """SHA256 hex del fichero; FileNotFoundError si no existe."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"no encontrado: {target}")
    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def container_command() -> list:
    """Única fuente del argv (rutas reales de contenedor, sin replaces)."""
    userdir = CONTAINER_USERDATA
    return [
        "freqtrade", "trade",
        "--config", f"{CONTAINER_CODE}/{CONFIG_REL.as_posix()}",
        "--strategy", STRATEGY_NAME,
        "--strategy-path", f"{CONTAINER_CODE}/{STRATEGY_DIR_REL.as_posix()}",
        "--userdir", userdir,
        "--db-url", "sqlite:///" + userdir + "/tradesv3.dryrun.sqlite",
        "--logfile", userdir + "/freqtrade.log",
    ]


def baseline_command() -> list:
    """Argv cerrado del baseline (DB propia, sin reutilizar la de smoke)."""
    userdir = CONTAINER_USERDATA
    return [
        "freqtrade", "trade",
        "--config", f"{CONTAINER_CODE}/{BASELINE_CONFIG_REL.as_posix()}",
        "--strategy", BASELINE_STRATEGY_NAME,
        "--strategy-path", f"{CONTAINER_CODE}/{BASELINE_STRATEGY_DIR_REL.as_posix()}",
        "--userdir", userdir,
        "--db-url", "sqlite:///" + userdir + "/" + BASELINE_DB_FILENAME,
        "--logfile", userdir + "/freqtrade.log",
    ]


def validate_baseline_config(config: dict, environ) -> None:
    """Acepta solo la config canónica baseline BTC spot 1h dry-run."""
    _validate_common(config, environ)
    for pricing in ("entry_pricing", "exit_pricing"):
        if pricing in config and config[pricing] != BASELINE_PRICING_PIN:
            raise ValueError(f"{pricing} debe ser exactamente {BASELINE_PRICING_PIN}")
    if config["timeframe"] != "1h":
        raise ValueError("timeframe debe ser 1h")
    if config["strategy"] != BASELINE_STRATEGY_NAME:
        raise ValueError(f"strategy debe ser {BASELINE_STRATEGY_NAME}")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_config(code_root: Path) -> tuple[dict, Path]:
    cfg_path = code_root / CONFIG_REL
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config ilegible: {cfg_path}: {exc}") from exc
    return config, cfg_path


def _load_baseline_config(code_root: Path) -> tuple[dict, Path]:
    cfg_path = code_root / BASELINE_CONFIG_REL
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config baseline ilegible: {cfg_path}: {exc}") from exc
    return config, cfg_path


def _module_hash(filename: str) -> str:
    """SHA256 del módulo propio; sin fallback: si falta, FileNotFoundError."""
    return file_hash(Path(__file__).with_name(filename))


# Políticas cerradas inmutables: solo dos perfiles, sin kwargs genéricos.
# Cada política fija kind/profile/db/strategy/config y hashes exigidos.
# Solo datos; el dispatch llama a los wrappers públicos por nombre para
# preservar mocks (p. ej. container_command) en tests.
_SMOKE_POLICY = {
    "profile": "smoke",
    "kind": "btc-lab-smoke-input",
    "run_kind": "btc-lab-smoke-run",
    "strategy": STRATEGY_NAME,
    "config_rel": CONFIG_REL,
    "strategy_rel": STRATEGY_REL,
    "extra_rels": (),
    "input_prefix": "input-",
    "run_prefix": "run-",
}
_BASELINE_POLICY = {
    "profile": "baseline",
    "kind": BASELINE_INPUT_KIND,
    "run_kind": BASELINE_RUN_KIND,
    "strategy": BASELINE_STRATEGY_NAME,
    "config_rel": BASELINE_CONFIG_REL,
    "strategy_rel": BASELINE_STRATEGY_REL,
    "extra_rels": (BASELINE_MARKET_REL, BASELINE_RESEARCH_REL),
    "input_prefix": "baseline-input-",
    "run_prefix": "baseline-run-",
}
_POLICIES = {
    "smoke": _SMOKE_POLICY,
    "baseline": _BASELINE_POLICY,
}


def _git_commit(code_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(code_root),
        capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
    )
    proc.check_returncode()
    commit = proc.stdout.strip()
    if not _COMMIT_RE.fullmatch(commit):
        raise RuntimeError(f"commit Git inesperado: {commit!r}")
    return commit


def _git_clean(code_root: Path) -> None:
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(code_root),
        capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
    )
    proc.check_returncode()
    if proc.stdout.strip():
        raise ValueError(f"worktree con cambios sin commit:\n{proc.stdout.strip()}")


def _inspect_image(image_ref: str) -> str:
    proc = subprocess.run(
        ["docker", "image", "inspect", image_ref],
        capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S,
    )
    proc.check_returncode()
    try:
        entries = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"docker image inspect ilegible: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("docker image inspect sin entradas")
    pinned_digest = PINNED_IMAGE.split("@", 1)[1]
    for entry in entries:
        digests = entry.get("RepoDigests") or []
        if any(isinstance(d, str) and d.split("@", 1)[-1] == pinned_digest for d in digests):
            image_id = entry.get("Id")
            if not image_id:
                raise RuntimeError("imagen sin Id")
            return image_id
    raise ValueError(f"imagen sin digest fijado {PINNED_IMAGE}: {image_ref}")


def _atomic_replace(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _atomic_create_new(path: Path, payload: dict) -> None:
    """Crea sin overwrite y visible solo cuando está completo (temp + link).

    `open(path, "x")` expone contenido parcial a lectores concurrentes;
    con link(2) el nombre aparece de golpe o no aparece. Si ya existe,
    FileExistsError y el previo queda intacto.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _validate_real_schema(config: dict) -> None:
    """Valida el schema disponible; sin la librería local se omite.

    El gate autoritativo corre en el contenedor con la imagen fijada, donde la
    librería está disponible; una validación en el host no sustituye ese gate.
    """
    try:
        from freqtrade.configuration.config_validation import validate_config_schema
    except ImportError:
        return
    try:
        validate_config_schema(config)
    except Exception as exc:
        raise ValueError(f"schema Freqtrade rechaza la config: {exc}") from exc


def _prepare(profile, code_root, storage_root, image_ref) -> str:
    """Preflight host común; `profile` solo "smoke"|"baseline"."""
    if profile not in _POLICIES:
        raise ValueError(f"perfil desconocido: {profile!r}")
    policy = _POLICIES[profile]
    if not isinstance(image_ref, str) or image_ref.strip() != PINNED_IMAGE:
        raise ValueError("image_ref debe ser exactamente la imagen fijada con digest")
    code = Path(code_root)
    store = Path(storage_root)
    if not code.is_dir():
        raise FileNotFoundError(f"código no encontrado: {code}")

    if profile == "smoke":
        config, cfg_path = _load_config(code)
    else:
        config, cfg_path = _load_baseline_config(code)
    strat_path = code / policy["strategy_rel"]
    if not strat_path.is_file():
        raise FileNotFoundError(f"estrategia no encontrada: {strat_path}")
    extra_paths = []
    for rel in policy["extra_rels"]:
        target = code / rel
        if not target.is_file():
            raise FileNotFoundError(f"módulo exigido no encontrado: {target}")
        extra_paths.append(target)
    if profile == "smoke":
        validate_config(config, os.environ)
    else:
        validate_baseline_config(config, os.environ)

    commit = _git_commit(code)
    _git_clean(code)
    image_id = _inspect_image(image_ref.strip())

    if profile == "smoke":
        expected_command = container_command()
    else:
        expected_command = baseline_command()
    manifest = {
        "kind": policy["kind"],
        "status": "PREPARED",
        "input_id": uuid.uuid4().hex,
        "created_at": _utcnow_iso(),
        "commit": commit,
        "image_ref": image_ref.strip(),
        "image_id": image_id,
        "image_digest": PINNED_IMAGE,
        "strategy": policy["strategy"],
        "config_path": f"{CONTAINER_CODE}/{policy['config_rel'].as_posix()}",
        "config_hash": file_hash(cfg_path),
        "strategy_hash": file_hash(strat_path),
        "launch_hash": _module_hash("launch.py"),
        "health_hash": _module_hash("health.py"),
        "expected_command": expected_command,
    }
    if profile == "baseline":
        manifest["profile"] = _BASELINE_POLICY["profile"]
        manifest["market_hash"] = file_hash(code / BASELINE_MARKET_REL)
        manifest["research_hash"] = file_hash(code / BASELINE_RESEARCH_REL)
    name = f"{policy['input_prefix']}{manifest['created_at'].replace(':', '').replace('+', '')}-{manifest['input_id'][:8]}.json"
    out = store / INPUTS_SUBDIR / name
    _atomic_create_new(out, manifest)
    return str(out)


def prepare_smoke(code_root, storage_root, image_ref) -> str:
    """Preflight host: verifica identidad y publica el input congelado."""
    return _prepare("smoke", code_root, storage_root, image_ref)


def _run_forwarded(argv: list, env: dict) -> tuple:
    """Ejecuta al hijo propagando SIGTERM/SIGINT.

    Devuelve (exit_code, signum): signum es la señal recibida por el
    launcher (y reenviada al hijo) o None si no hubo ninguna. La señal se
    registra en una celda local, sin estado global.
    """
    proc = subprocess.Popen(argv, env=env)
    received = []

    def _forward(signum, _frame):
        received.append(signum)
        if proc.poll() is None:
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

    old_term = signal.signal(signal.SIGTERM, _forward)
    old_int = signal.signal(signal.SIGINT, _forward)
    try:
        return proc.wait(), (received[0] if received else None)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def _require_container_roots(code_root, storage_root) -> tuple:
    """Resuelve roots y exige las constantes del contenedor antes de leer nada."""
    code = Path(code_root).resolve()
    store = Path(storage_root).resolve()
    if code != Path(CONTAINER_CODE).resolve():
        raise ValueError(f"code_root fuera del contenedor: {code_root!r}")
    if store != Path(CONTAINER_STORAGE).resolve():
        raise ValueError(f"storage_root fuera del contenedor: {store!r}")
    return code, store


def _check_input_manifest(manifest_in: dict) -> None:
    """Chequeos mínimos del manifiesto de entrada (forma, no contenido)."""
    if not isinstance(manifest_in, dict):
        raise ValueError("manifiesto de entrada ilegible: no es un objeto")
    if manifest_in.get("kind") != "btc-lab-smoke-input":
        raise ValueError("manifiesto de entrada con kind inesperado")
    if manifest_in.get("status") != "PREPARED":
        raise ValueError("manifiesto de entrada no está PREPARED")
    if manifest_in.get("image_ref") != PINNED_IMAGE:
        raise ValueError("manifiesto de entrada sin imagen fijada")
    if not _COMMIT_RE.fullmatch(str(manifest_in.get("commit") or "")):
        raise ValueError("manifiesto de entrada sin commit válido")
    if not manifest_in.get("input_id"):
        raise ValueError("manifiesto de entrada sin input_id")


def _finish_child(run_path: Path, base: dict, returncode, signum) -> int:
    if signum is not None:
        try:
            signame = signal.Signals(signum).name
        except ValueError:
            signame = str(signum)
        _atomic_replace(run_path, {
            **base, "status": "CANCELLED", "signal": signame,
            "exit_code": returncode, "finished_at": _utcnow_iso(),
        })
    else:
        _atomic_replace(run_path, {
            **base, "status": "SUCCEEDED" if returncode == 0 else "FAILED",
            "exit_code": returncode, "finished_at": _utcnow_iso(),
        })
    return returncode


def _run(profile, code_root, storage_root, input_path) -> int:
    """Ejecución común; `profile` solo "smoke"|"baseline".

    CANCELLED solo si el launcher recibe SIGTERM/SIGINT y la reenvía (con
    el exit original); un 130 espontáneo o un auto-SIGTERM del hijo sin
    señal al padre es FAILED.
    """
    if profile not in _POLICIES:
        raise ValueError(f"perfil desconocido: {profile!r}")
    policy = _POLICIES[profile]
    code, store = _require_container_roots(code_root, storage_root)
    try:
        manifest_in = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        label = "baseline" if profile == "baseline" else "entrada"
        raise ValueError(f"manifiesto {label} ilegible: {exc}") from exc
    if profile == "smoke":
        _check_input_manifest(manifest_in)
    else:
        _check_baseline_input_manifest(manifest_in)

    if profile == "smoke":
        config, _ = _load_config(code)
        validate_config(config, os.environ)
    else:
        config, _ = _load_baseline_config(code)
        validate_baseline_config(config, os.environ)
    strat_path = code / policy["strategy_rel"]
    if file_hash(code / policy["config_rel"]) != manifest_in.get("config_hash"):
        raise ValueError("config alterada tras prepare: hashes no coinciden")
    if file_hash(strat_path) != manifest_in.get("strategy_hash"):
        raise ValueError("estrategia alterada tras prepare: hashes no coinciden")
    if profile == "baseline":
        if file_hash(code / BASELINE_MARKET_REL) != manifest_in.get("market_hash"):
            raise ValueError("market/train.py alterado tras prepare: hash no coincide")
        if file_hash(code / BASELINE_RESEARCH_REL) != manifest_in.get("research_hash"):
            raise ValueError("operations/research.py alterado tras prepare: hash no coincide")
    for label, filename in (("launch", "launch.py"), ("health", "health.py")):
        expected = manifest_in.get(f"{label}_hash")
        if not expected or _module_hash(filename) != expected:
            raise ValueError(f"{filename} alterado tras prepare: hash no coincide")
    _validate_real_schema(config)

    if profile == "smoke":
        command = container_command()
    else:
        command = baseline_command()
    if manifest_in.get("expected_command") != command:
        raise ValueError("comando esperado no coincide con el manifiesto de entrada")

    run_id = uuid.uuid4().hex
    started_at = _utcnow_iso()
    run_path = store / RUNS_SUBDIR / f"{policy['run_prefix']}{started_at.replace(':', '').replace('+', '')}-{run_id[:8]}.json"
    base = {
        "kind": policy["run_kind"],
        "run_id": run_id,
        "input_id": manifest_in.get("input_id"),
        "created_at": started_at,
        "commit": manifest_in.get("commit"),
        "image_ref": manifest_in.get("image_ref"),
        "image_id": manifest_in.get("image_id"),
        "config_hash": manifest_in.get("config_hash"),
        "strategy_hash": manifest_in.get("strategy_hash"),
        "command": command,
        "logfile": f"{CONTAINER_USERDATA}/freqtrade.log",
    }
    if profile == "baseline":
        base["profile"] = _BASELINE_POLICY["profile"]
        base["market_hash"] = manifest_in.get("market_hash")
        base["research_hash"] = manifest_in.get("research_hash")
    _atomic_create_new(run_path, {**base, "status": "RUNNING", "exit_code": None})

    child_env = {k: v for k, v in os.environ.items() if not k.startswith("FREQTRADE__")}
    try:
        returncode, signum = _run_forwarded(command, child_env)
    except Exception as exc:
        _atomic_replace(run_path, {
            **base, "status": "FAILED", "exit_code": None,
            "finished_at": _utcnow_iso(), "error": f"{type(exc).__name__}: {exc}",
        })
        raise
    return _finish_child(run_path, base, returncode, signum)


def run_smoke(code_root, storage_root, input_path) -> int:
    """Revalida identidad contra el input y ejecuta; manifiesto propio atómico."""
    return _run("smoke", code_root, storage_root, input_path)


def _check_baseline_input_manifest(manifest_in: dict) -> None:
    if not isinstance(manifest_in, dict):
        raise ValueError("manifiesto baseline ilegible: no es un objeto")
    if manifest_in.get("kind") != BASELINE_INPUT_KIND:
        raise ValueError("manifiesto baseline con kind inesperado")
    if manifest_in.get("profile") != BASELINE_PROFILE:
        raise ValueError("manifiesto baseline con profile inesperado")
    if manifest_in.get("status") != "PREPARED":
        raise ValueError("manifiesto baseline no está PREPARED")
    if manifest_in.get("image_ref") != PINNED_IMAGE:
        raise ValueError("manifiesto baseline sin imagen fijada")
    if not _COMMIT_RE.fullmatch(str(manifest_in.get("commit") or "")):
        raise ValueError("manifiesto baseline sin commit válido")
    if not manifest_in.get("input_id"):
        raise ValueError("manifiesto baseline sin input_id")


def prepare_baseline(code_root, storage_root, image_ref) -> str:
    """Preflight host baseline: identidad cerrada y manifiesto único inmutable."""
    return _prepare("baseline", code_root, storage_root, image_ref)


def run_baseline(code_root, storage_root, input_path) -> int:
    """Revalida identidad baseline contra el input y ejecuta; manifiesto propio."""
    return _run("baseline", code_root, storage_root, input_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="operations.launch")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="freqtrade --version (sin mercado)")

    prep = sub.add_parser("prepare-smoke", help="preflight host + manifiesto de entrada")
    prep.add_argument("--code-root", default=os.environ.get("LAB_CODE_ROOT", ""))
    prep.add_argument("--storage-root", default=os.environ.get("LAB_STORAGE_ROOT", ""))
    prep.add_argument("--image", default=PINNED_IMAGE)

    smoke = sub.add_parser("smoke", help="ejecuta el smoke (en contenedor)")
    smoke.add_argument("--input", default=os.environ.get("SMOKE_INPUT", CONTAINER_ACTIVE_INPUT))

    prep_base = sub.add_parser("prepare-baseline", help="preflight host baseline + manifiesto")
    prep_base.add_argument("--code-root", default=os.environ.get("LAB_CODE_ROOT", ""))
    prep_base.add_argument("--storage-root", default=os.environ.get("LAB_STORAGE_ROOT", ""))
    prep_base.add_argument("--image", default=PINNED_IMAGE)

    baseline = sub.add_parser("baseline", help="ejecuta el baseline (en contenedor)")
    baseline.add_argument("--input", default=os.environ.get("BASELINE_INPUT", CONTAINER_ACTIVE_BASELINE_INPUT))

    args = parser.parse_args(argv)
    if args.command == "version":
        proc = subprocess.run(["freqtrade", "--version"])
        return proc.returncode
    if args.command == "prepare-smoke":
        if not args.code_root or not args.storage_root:
            print("prepare-smoke exige --code-root y --storage-root (o LAB_CODE_ROOT/LAB_STORAGE_ROOT)",
                  file=sys.stderr)
            return 2
        print(prepare_smoke(args.code_root, args.storage_root, args.image))
        return 0
    if args.command == "prepare-baseline":
        if not args.code_root or not args.storage_root:
            print("prepare-baseline exige --code-root y --storage-root (o LAB_CODE_ROOT/LAB_STORAGE_ROOT)",
                  file=sys.stderr)
            return 2
        print(prepare_baseline(args.code_root, args.storage_root, args.image))
        return 0
    if args.command == "smoke":
        if not args.input:
            print("smoke exige --input con el manifiesto congelado (o SMOKE_INPUT)", file=sys.stderr)
            return 2
        return run_smoke(CONTAINER_CODE, CONTAINER_STORAGE, args.input)
    if args.command == "baseline":
        if not args.input:
            print("baseline exige --input con el manifiesto congelado (o BASELINE_INPUT)", file=sys.stderr)
            return 2
        return run_baseline(CONTAINER_CODE, CONTAINER_STORAGE, args.input)
    # Compat: `smoke` sin subcomando explícito ya cubierto arriba; cualquier
    # otro valor llega aquí solo si argparse lo permite en el futuro.
    if not args.input:
        print("smoke exige --input con el manifiesto congelado (o SMOKE_INPUT)", file=sys.stderr)
        return 2
    return run_smoke(CONTAINER_CODE, CONTAINER_STORAGE, args.input)


if __name__ == "__main__":
    sys.exit(main())
