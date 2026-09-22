"""Runner cerrado de campana search PR02 (T06).

Fases REALES (sin placeholders): `screen` (TRAIN 72 fees 0.001/0.002 +
top9), `finalists` (top9 stress 0.0015/0.003 + bias nativo + top3),
`validation` (max3 all4 fees + candidata) y `test` (una all4 fees +
bundle paper solo si PASS). Autorizacion holdout via `prepare-phase`
(host, grants + inputs de rol + bindings de snapshot).


Cierre screen:
- Host `prepare --train-snapshot <basename>`: Git limpio + imagen fijada,
  definicion inmutable que ATA snapshot ref + source algorithm + config
  hash (no solo self-hash+DD), verificada contra registry/splits/fees
  reales. Genera la fuente via la unica renderer pura
  `research.campaign.render_strategy_module` (SpotCandidates delega, sin
  clones) y la escribe a `storage/search/generated/`. Sin acoplar dataset
  sintetico de QA como artefacto obligatorio. Input unico + estado canonico
  atomico bajo lock, sin overwrite.
- Config search fija tipo baseline pero stake_amount unlimited, max 1, 1h,
  market other, API/Telegram off keys blank. Sin flags arbitrarios.
- `LAB_SEARCH_INPUT` unico RO; state/control RW un campaignID canonico.
  Dataset/estrategia/config/generated RO, resultados RW, sin raiz Git/
  memoria/botstate. TRAIN solo ve TRAIN.
- Sin bypass JSON manual: grants por state+reports+definition hash.
  Sin campo `force`. `reserve_test` atomico antes de primera
  descarga/lectura TEST; resume solo misma candidata/grant/definicion.
- Screen nativo: batches <=6 via --strategy-list (+ control single con
  --strategy), un job CPU4/RAM8/thread1; lock y timeout; `_run_streaming`
  reusado de operations.research; hash mandatorio. Plan por rol/ano+seg
  (evaluation.plan_windows), start=max(yearstart, eval_start),
  end=min(yearend, end_exclusive), warmup 201 mismo pasado, short
  not-evaluable honesto. Misma ventana para candidatas y control. Datadir
  completo del seg RO + timerange_fmt nativo (NO ISO). Sin trades fuera de
  ventana. BH comparable MTM parent, fullBTC separado, sin CAGR.
- ZIP: TODOS los nombres exactos; ausente/extra/malformado core => FAIL.
  `trades_count == len(trades)` exigido. Ledger/reconcile tol 0.01 con 5m
  SOLO EVAL; DD de ledger. Records schema selection 12 campos; agregacion
  por ano con interseccion; forced retira solo positivo; cero real g0;
  faltante nonvalid, sin huecos silenciosos. Linaje completo. Cache nativa
  solo con firma exacta (imagen, template/gen, config, params, datafiles,
  ventana, fee + base alg search/evaluation/campaign/selection/equity).
  Runs validos no se repiten. Intentos/timeout cuentan wall a budget con
  checks previos (remaining>0, timeout<=remaining) y registro atomico
  posterior; crash con cargo conservador <=timeout+grace documentado, sin
  reset. RUNNING antes de trabajo; FAIL vence INCONCL. Progreso JSON por
  job, sin stream infinito. WF descriptivo + top9 freeze via selection
  pura; NO_CANDIDATE terminal si 0 sin abrir holdouts.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from operations import launch

CAMPAIGN_ID = "btc-strategy-search-pr02"
PAIR = "BTC/USDT"
TIMEFRAME_1H = "1h"
TIMEFRAME_5M = "5m"
WARMUP_BARS = 201
BUDGET_SECONDS = 43200
DD_LIMIT = 0.15
CUTOFF_ISO = "2026-09-22T00:00:00Z"
INPUT_KIND = "btc-lab-search-input"
REPORT_KIND = "btc-lab-search-report"
STATUS_KIND = "btc-lab-search-status"
SCREEN_SESSION_KIND = "btc-lab-search-screen"
BATCH_MAX = 6
NATIVE_TIMEOUT_S = 1800
NATIVE_GRACE_S = 30
SAME_CAUSE_CAP = 3

FEES_SCREEN = (0.001, 0.002)
FEES_STRESS = (0.0015, 0.003)
FEES_ALL = (0.001, 0.0015, 0.002, 0.003)

SEARCH_CONFIG_REL = Path("configs/search.json")
SEARCH_REL = Path("operations/search.py")
EVALUATION_REL = Path("research/evaluation.py")
CAMPAIGN_REL = Path("research/campaign.py")
SELECTION_REL = Path("research/selection.py")
STATE_REL = Path("research/state.py")
CANDIDATES_REL = Path("strategies/search/SpotCandidates.py")
CONTROL_REL = Path("strategies/search/ControlSmaCrossBaseline.py")
BASELINE_BASE_REL = Path("strategies/baseline/SmaCrossBaseline.py")
EQUITY_REL = Path("market/equity.py")
PARTITIONS_REL = Path("market/history.py")
TRAIN_HELPER_REL = Path("market/train.py")
HISTORY_REL = Path("operations/history.py")
RESEARCH_REL = Path("operations/research.py")

GENERATED_FILENAME = "SpotCandidatesGenerated.py"

CONTAINER_CODE = "/opt/btc-lab"
CONTAINER_SEARCH = "/lab-search"
CONTAINER_INPUT = "/lab-search/input.json"
CONTAINER_CONFIG = "/opt/btc-lab/configs/search.json"
CONTAINER_STRATEGY_CODE_PATH = "/opt/btc-lab/strategies/search"
CONTAINER_GENERATED = "/lab-search/generated"
CONTAINER_SNAP_TRAIN = "/lab-search/snapshots/train"
CONTAINER_SNAP_TRAIN_MANIFEST = "/lab-search/snapshots/train-manifest.json"
CONTAINER_SNAP_VAL = "/lab-search/snapshots/validation"
CONTAINER_SNAP_VAL_MANIFEST = "/lab-search/snapshots/validation-manifest.json"
CONTAINER_SNAP_TEST = "/lab-search/snapshots/test"
CONTAINER_SNAP_TEST_MANIFEST = "/lab-search/snapshots/test-manifest.json"

FINALISTS_SESSION_KIND = "btc-lab-search-finalists"
VALIDATION_SESSION_KIND = "btc-lab-search-validation"
TEST_SESSION_KIND = "btc-lab-search-test"
GRANT_KIND = "btc-lab-search-grant"
SNAP_BIND_KIND = "btc-lab-search-snapshot-binding"
PAPER_BUNDLE_KIND = "btc-lab-paper-bundle"

BIAS_FEE = 0.002
BIAS_MIN_TRADES = 5
RECURSIVE_STARTUPS = (201, 400, 800, 1200)
LOOKAHEAD_MIN_AMOUNT = 5

CONTROL_STRATEGY = "ControlSmaCrossBaseline"

PHASES = ("screen", "finalists", "validation", "test")


def _role_snap_paths(role: str) -> tuple[str, str]:
    """Rutas fijas por rol (resueltas en llamada, mockeables en tests)."""
    if role == "train":
        return CONTAINER_SNAP_TRAIN, CONTAINER_SNAP_TRAIN_MANIFEST
    if role == "validation":
        return CONTAINER_SNAP_VAL, CONTAINER_SNAP_VAL_MANIFEST
    if role == "test":
        return CONTAINER_SNAP_TEST, CONTAINER_SNAP_TEST_MANIFEST
    raise ValueError(f"rol desconocido: {role!r}")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug() -> str:
    return f"{_utcnow_iso().replace(':', '').replace('+', '')}-{uuid.uuid4().hex[:8]}"


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_search_config(config: dict, environ) -> None:
    """Config search cerrada; ValueError si no es la canonica."""
    for key in environ:
        if key.startswith("FREQTRADE__"):
            raise ValueError(f"override de entorno no permitido: {key}")
    if not isinstance(config, dict):
        raise ValueError("la configuracion debe ser un objeto JSON")
    allowed_top = {
        "exchange", "pairlists", "trading_mode", "dry_run", "dry_run_wallet",
        "stake_currency", "stake_amount", "max_open_trades", "timeframe",
        "strategy", "entry_pricing", "exit_pricing", "initial_state",
        "internals", "api_server", "telegram", "fee",
    }
    extra = set(config) - allowed_top
    if extra:
        raise ValueError(f"claves adicionales no permitidas: {sorted(extra)}")
    missing = allowed_top - set(config)
    if missing:
        raise ValueError(f"claves obligatorias ausentes: {sorted(missing)}")
    exchange = config.get("exchange")
    if not isinstance(exchange, dict):
        raise ValueError("exchange debe ser un objeto")
    if set(exchange) - {"name", "key", "secret", "password",
                        "pair_whitelist", "pair_blacklist"}:
        raise ValueError("claves de exchange no permitidas")
    if exchange.get("name") != "binance":
        raise ValueError("solo se permite exchange binance")
    if exchange.get("pair_whitelist") != ["BTC/USDT"]:
        raise ValueError("pair_whitelist debe ser exactamente [BTC/USDT]")
    if exchange.get("pair_blacklist") != []:
        raise ValueError("pair_blacklist debe ser []")
    for alias in ("key", "secret", "password"):
        if exchange.get(alias) != "":
            raise ValueError(f"credencial no permitida en {alias!r}: debe ser vacia")
    if config.get("pairlists") != [{"method": "StaticPairList"}]:
        raise ValueError("pairlists debe ser exactamente [StaticPairList]")
    if config.get("trading_mode") != "spot":
        raise ValueError("trading_mode debe ser spot")
    if config.get("dry_run") is not True:
        raise ValueError("dry_run debe ser true booleano")
    if config.get("dry_run_wallet") != 10000:
        raise ValueError("dry_run_wallet debe ser 10000")
    if config.get("stake_currency") != "USDT":
        raise ValueError("stake_currency debe ser USDT")
    if config.get("stake_amount") != "unlimited":
        raise ValueError("stake_amount debe ser exactamente 'unlimited'")
    if config.get("max_open_trades") != 1:
        raise ValueError("max_open_trades debe ser 1")
    if config.get("timeframe") != "1h":
        raise ValueError("timeframe debe ser 1h")
    strategy = config.get("strategy")
    if not isinstance(strategy, str) or not strategy:
        raise ValueError("strategy debe ser str no vacia")
    from research.campaign import generate_variants

    known = {str(v.get("class_name")) for v in generate_variants()}
    known.add(CONTROL_STRATEGY)
    if strategy not in known:
        raise ValueError(f"strategy fuera de registry cerrado: {strategy!r}")
    pin = {"price_side": "other", "use_order_book": False,
           "order_book_top": 1, "price_last_balance": 0.0}
    for pricing in ("entry_pricing", "exit_pricing"):
        if config.get(pricing) != pin:
            raise ValueError(f"{pricing} debe ser exactamente {pin}")
    if config.get("initial_state") != "running":
        raise ValueError("initial_state debe ser running")
    if config.get("internals") != {"heartbeat_interval": 60}:
        raise ValueError("internals debe ser exactamente {'heartbeat_interval': 60}")
    api = config.get("api_server")
    api_pin = {
        "enabled": False, "listen_ip_address": "127.0.0.1", "listen_port": 8080,
        "username": "", "password": "",
        "jwt_secret_key": "DISABLED-NO-API-SERVER-PLACEHOLDER-0000",
    }
    if api != api_pin:
        raise ValueError("api_server debe estar desactivado con valores fijados")
    telegram = config.get("telegram")
    if telegram != {"enabled": False, "token": "", "chat_id": ""}:
        raise ValueError("telegram debe estar desactivado con valores fijados")
    try:
        fee = float(config.get("fee"))
    except (TypeError, ValueError):
        raise ValueError("fee debe ser numerica") from None
    if fee not in FEES_ALL:
        raise ValueError(f"fee debe ser una de {FEES_ALL}, fue {fee!r}")
    if "force" in config:
        raise ValueError("campo 'force' prohibido")


def _code_hashes(code_root: Path) -> dict:
    code = Path(code_root)
    targets = {
        "search_hash": SEARCH_REL,
        "evaluation_hash": EVALUATION_REL,
        "campaign_hash": CAMPAIGN_REL,
        "selection_hash": SELECTION_REL,
        "state_hash": STATE_REL,
        "candidates_hash": CANDIDATES_REL,
        "control_hash": CONTROL_REL,
        "config_hash": SEARCH_CONFIG_REL,
        "equity_hash": EQUITY_REL,
        "partitions_hash": PARTITIONS_REL,
        "train_helper_hash": TRAIN_HELPER_REL,
        "history_hash": HISTORY_REL,
        "research_hash": RESEARCH_REL,
        "launch_hash": Path("operations/launch.py"),
        "health_hash": Path("operations/health.py"),
    }
    out = {}
    for key, rel in targets.items():
        out[key] = launch.file_hash(code / rel)
    # El wrapper del control hereda esta implementacion; debe formar parte de
    # la identidad igual que el propio wrapper.
    baseline_base = code / BASELINE_BASE_REL
    if not baseline_base.is_file():
        raise ValueError(f"control baseline ausente: {BASELINE_BASE_REL}")
    out["baseline_base_hash"] = launch.file_hash(baseline_base)
    return out


def _rendered_source_for(variants) -> str:
    from research.campaign import render_strategy_module

    return render_strategy_module(list(variants))


def _safe_manifest_basename(name: str) -> str:
    base = Path(str(name or "")).name
    if not base or base in (".", ".."):
        raise ValueError(f"snapshot ref no es nombre simple: {name!r}")
    if "/" in str(name or "") or "\\" in str(name or ""):
        raise ValueError(f"snapshot ref con ruta: {name!r}")
    if not base.endswith(".json"):
        raise ValueError(f"snapshot ref debe ser manifiesto .json: {name!r}")
    return base


def _load_snapshot_manifest_file(manifest_path: Path) -> dict:
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"snapshot ilegible: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("snapshot no es un objeto")
    return manifest


def _verify_snapshot_scope(manifest: dict, role: str = "train") -> None:
    if manifest.get("kind") != "btc-lab-history-snapshot":
        raise ValueError("snapshot kind inesperado")
    if manifest.get("status") != "FROZEN":
        raise ValueError("snapshot no esta FROZEN")
    if manifest.get("role") != role:
        raise ValueError(f"snapshot de rol {manifest.get('role')!r}, se exige {role!r}")
    from market.history import partition_bounds

    start, end = partition_bounds(role)
    if start.year < 2017:
        raise ValueError("bounds con datos anteriores a 2017")
    # Bounds exactos por rol (sin subcadenas): el manifiesto debe declarar
    # el intervalo cerrado completo, nunca un subrango ni otro rol.
    if str(manifest.get("range_start") or "") != start.isoformat():
        raise ValueError(f"snapshot {role} range_start fuera de bounds exactos")
    if str(manifest.get("range_end") or "") != end.isoformat():
        raise ValueError(f"snapshot {role} range_end fuera de bounds exactos")
    for key in ("snapshot_id", "snapshot_dir", "whole_5m_sha256", "whole_1h_sha256"):
        if not manifest.get(key):
            raise ValueError(f"snapshot sin {key}")
    if not manifest.get("segments_meta"):
        raise ValueError("snapshot sin segments_meta")


def _pair_feather(pair: str, timeframe: str) -> str:
    cleaned = pair
    for ch in ("/", " ", ".", "@", "$", "+", ":"):
        cleaned = cleaned.replace(ch, "_")
    return f"{cleaned}-{timeframe}.feather"


def _verify_snapshot_data_files(snapshot_root: Path, manifest: dict) -> dict:
    """Coteja hashes de archivos de datos (no solo campos del manifiesto).

    `snapshot_root` es el directorio real ya resuelto por el host o el mount
    fijo del rol en container. `snapshot_dir` sigue validandose como nombre
    simple, pero no se usa para reconstruir el alias del mount.
    """
    from operations.research import _contained, _within_root

    snap_raw = str(manifest.get("snapshot_dir") or "")
    if (not snap_raw or snap_raw in (".", "..")
            or Path(snap_raw).name != snap_raw
            or "/" in snap_raw or "\\" in snap_raw):
        raise ValueError("snapshot_dir ilegible")
    snap_dir = Path(snapshot_root).resolve()
    if not snap_dir.is_dir():
        raise ValueError("snapshot_dir ausente (RO, sin reparar)")
    checked = {"segments": 0, "files": []}
    for label, rel, key in (
        ("5m total", _pair_feather(PAIR, TIMEFRAME_5M), "whole_5m_sha256"),
        ("1h total", _pair_feather(PAIR, TIMEFRAME_1H), "whole_1h_sha256"),
    ):
        target = _contained(snap_dir, rel, label)
        if not target.is_file():
            raise ValueError(f"dato faltante ({label})")
        digest = launch.file_hash(target)
        if digest != manifest.get(key):
            raise ValueError(f"dato alterado ({label})")
        checked["files"].append(rel)
    for meta in manifest.get("segments_meta") or []:
        seg_raw = str(meta.get("seg_dir") or "")
        if not seg_raw or seg_raw in (".", ".."):
            raise ValueError(f"seg{meta.get('index')} dir ilegible")
        seg_dir = _within_root(snap_dir, Path(seg_raw), f"seg{meta.get('index')}")
        for rel, key in (
            (_pair_feather(PAIR, TIMEFRAME_5M), "file_5m_sha256"),
            (_pair_feather(PAIR, TIMEFRAME_1H), "file_1h_sha256"),
        ):
            target = _contained(seg_dir, rel, f"seg{meta.get('index')}")
            if not target.is_file():
                raise ValueError(f"segmento faltante seg{meta.get('index')} {rel}")
            if launch.file_hash(target) != meta.get(key):
                raise ValueError(f"segmento alterado seg{meta.get('index')} {rel}")
        checked["segments"] += 1
    return checked


def _module_code_root() -> Path:
    """Raiz del arbol que contiene este modulo (host o /opt/btc-lab)."""
    return Path(__file__).resolve().parents[1]


def build_definition(snapshot_ref: dict, generated_source_sha256: str,
                     config_hash: str, code_hashes=None) -> dict:
    """Definicion canonica que ATA snapshot ref + source + config + codigo.

    Incluye el mapa completo de code_hashes (base SpotCandidates, registry,
    control, config...): mutar la base con el generado intacto cambia la
    definicion y bloquea contra el estado ya registrado. Sin code_hashes
    explicitos se derivan del arbol vivo (mismo protocolo de 3 params).
    Verifica contra registry/splits/fees reales (no solo self-hash+DD).
    """
    from research.campaign import generate_variants

    from market.history import partition_bounds

    if code_hashes is None:
        code_hashes = _code_hashes(_module_code_root())
    if not isinstance(code_hashes, dict) or not code_hashes.get("candidates_hash"):
        raise ValueError("code_hashes incompletos para la definicion")
    variants = generate_variants()
    if len(variants) != 72:
        raise ValueError(f"registry debe tener 72, fue {len(variants)}")
    if len({str(v.get("id")) for v in variants}) != 72:
        raise ValueError("IDs de variantes no unicos")
    train_start, train_end = partition_bounds("train")
    val_start, val_end = partition_bounds("validation")
    test_start, test_end = partition_bounds("test")
    if train_start.year < 2017 or train_start.year == 2016:
        raise ValueError("rango TRAIN anterior a 2017")
    if not generated_source_sha256 or not isinstance(generated_source_sha256, str):
        raise ValueError("generated_source_sha256 requerido")
    if not config_hash or not isinstance(config_hash, str):
        raise ValueError("config_hash requerido")
    for key in ("manifest", "manifest_sha256", "snapshot_id", "snapshot_dir",
                "whole_5m_sha256", "whole_1h_sha256", "range_start", "range_end"):
        if not snapshot_ref.get(key):
            raise ValueError(f"snapshot_ref sin {key}")
    return {
        "campaign_id": CAMPAIGN_ID,
        "variants": variants,
        "gates": {
            "max_drawdown_pct": DD_LIMIT,
            "budget_seconds": BUDGET_SECONDS,
            "warmup_bars_1h": WARMUP_BARS,
            "tolerance_reconcile_usdt": 0.01,
            "batch_max": BATCH_MAX,
        },
        "splits": {
            "train": [train_start.isoformat(), train_end.isoformat()],
            "validation": [val_start.isoformat(), val_end.isoformat()],
            "test": [test_start.isoformat(), test_end.isoformat()],
        },
        "cutoff": CUTOFF_ISO,
        "fees": {
            "screen": list(FEES_SCREEN),
            "stress": list(FEES_STRESS),
            "all": list(FEES_ALL),
        },
        "snapshot_train_ref": dict(snapshot_ref),
        "source": {
            "renderer": "research.campaign.render_strategy_module",
            "generated_source_sha256": str(generated_source_sha256),
        },
        "config_hash": str(config_hash),
        "code_hashes": {str(k): str(v) for k, v in dict(code_hashes).items()},
    }


def definition_hash_for(definition: dict) -> str:
    return _sha256_bytes(_canonical(definition))


def _verify_definition_bindings(definition: dict, snapshot_ref: dict,
                                generated_source_sha256: str,
                                config_hash: str, code_hashes=None) -> None:
    from research.campaign import generate_variants

    from market.history import partition_bounds

    if not isinstance(definition, dict):
        raise ValueError("definition no es objeto")
    if code_hashes is None:
        code_hashes = _code_hashes(_module_code_root())
    if definition.get("code_hashes") != {str(k): str(v) for k, v in dict(code_hashes).items()}:
        raise ValueError("definition::code_hashes difiere del codigo actual "
                         "(base mutada con generado intacto: bloque, sin reset)")
    if str(definition.get("campaign_id")) != CAMPAIGN_ID:
        raise ValueError("definition de otra campana")
    live_variants = generate_variants()
    if definition.get("variants") != live_variants:
        raise ValueError("definition::variants difiere del registry actual")
    ts, te = partition_bounds("train")
    vs, ve = partition_bounds("validation")
    es, ee = partition_bounds("test")
    if definition.get("splits") != {
        "train": [ts.isoformat(), te.isoformat()],
        "validation": [vs.isoformat(), ve.isoformat()],
        "test": [es.isoformat(), ee.isoformat()],
    }:
        raise ValueError("definition::splits difiere de bounds actuales")
    if definition.get("fees") != {
        "screen": list(FEES_SCREEN),
        "stress": list(FEES_STRESS),
        "all": list(FEES_ALL),
    }:
        raise ValueError("definition::fees difiere de fees cerrados")
    if definition.get("snapshot_train_ref") != snapshot_ref:
        raise ValueError("definition::snapshot_train_ref difiere del snapshot real")
    if definition.get("source") != {
        "renderer": "research.campaign.render_strategy_module",
        "generated_source_sha256": str(generated_source_sha256),
    }:
        raise ValueError("definition::source difiere del renderer real")
    if str(definition.get("config_hash")) != str(config_hash):
        raise ValueError("definition::config_hash difiere de la config real")


def _host_locked_control(storage_root: Path):
    import fcntl

    control = Path(storage_root) / "search" / "control"
    control.mkdir(parents=True, exist_ok=True)
    lock_path = control / "search.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)

    class _Guard:
        def __enter__(self):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise RuntimeError("otro prepare/search en curso (lock ocupado)")
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return False

    return _Guard()


def _container_locked():
    import fcntl

    control = Path(CONTAINER_SEARCH) / "control"
    control.mkdir(parents=True, exist_ok=True)
    lock_path = control / "search.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)

    class _Guard:
        def __enter__(self):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise RuntimeError("otro job search en curso (lock ocupado)")
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return False

    return _Guard()


def prepare_search(code_root, storage_root, image_ref, train_snapshot: str) -> str:
    """Preflight HOST con snapshot explicito; crea fuente generada e input."""
    if not isinstance(image_ref, str) or image_ref.strip() != launch.PINNED_IMAGE:
        raise ValueError("image_ref debe ser exactamente la imagen fijada con digest")
    base = _safe_manifest_basename(train_snapshot)
    code = Path(code_root)
    store = Path(storage_root)
    if not code.is_dir():
        raise FileNotFoundError(f"codigo no encontrado: {code}")
    cfg_path = code / SEARCH_CONFIG_REL
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config search ilegible: {exc}") from exc
    validate_search_config(config, os.environ)
    for rel in (SEARCH_REL, EVALUATION_REL, CAMPAIGN_REL, SELECTION_REL,
                STATE_REL, CANDIDATES_REL, CONTROL_REL, EQUITY_REL,
                PARTITIONS_REL, TRAIN_HELPER_REL, HISTORY_REL, RESEARCH_REL,
                SEARCH_CONFIG_REL,
                Path("operations/launch.py"), Path("operations/health.py")):
        if not (code / rel).is_file():
            raise FileNotFoundError(f"fichero hasheado ausente: {rel}")
    # Snapshot explicito por CLI, verificado en alcance/hash/rango (sin latest).
    snaps = store / "history" / "snapshots"
    manifest_path = snaps / base
    snapshot_manifest = _load_snapshot_manifest_file(manifest_path)
    _verify_snapshot_scope(snapshot_manifest, "train")
    snap_dir = snaps / str(snapshot_manifest.get("snapshot_dir"))
    if not snap_dir.is_dir():
        raise ValueError("snapshot_dir ausente en storage")
    # Coteja archivos whole en host (los segmentos se cotejan en container).
    for rel, key in (
        (_pair_feather(PAIR, TIMEFRAME_5M), "whole_5m_sha256"),
        (_pair_feather(PAIR, TIMEFRAME_1H), "whole_1h_sha256"),
    ):
        target = snap_dir / rel
        if not target.is_file():
            raise ValueError(f"snapshot TRAIN sin {rel}")
        if launch.file_hash(target) != snapshot_manifest.get(key):
            raise ValueError(f"snapshot TRAIN alterado ({rel})")
    from research.campaign import generate_variants

    variants = generate_variants()
    rendered = _rendered_source_for(variants)
    generated_sha = _sha256_bytes(rendered.encode("utf-8"))
    config_hash = launch.file_hash(cfg_path)
    snapshot_ref = {
        "manifest": base,
        "manifest_sha256": launch.file_hash(manifest_path),
        "snapshot_id": str(snapshot_manifest.get("snapshot_id")),
        "snapshot_dir": str(snapshot_manifest.get("snapshot_dir")),
        "whole_5m_sha256": str(snapshot_manifest.get("whole_5m_sha256")),
        "whole_1h_sha256": str(snapshot_manifest.get("whole_1h_sha256")),
        "range_start": str(snapshot_manifest.get("range_start")),
        "range_end": str(snapshot_manifest.get("range_end")),
        "rows_5m": int(snapshot_manifest.get("rows_5m", 0)),
        "rows_1h": int(snapshot_manifest.get("rows_1h", 0)),
        "segments": int(snapshot_manifest.get("segments", 0)),
    }
    hashes = _code_hashes(code)
    definition = build_definition(snapshot_ref, generated_sha, config_hash, hashes)
    definition_hash = definition_hash_for(definition)
    commit = launch._git_commit(code)
    launch._git_clean(code)
    image_id = launch._inspect_image(image_ref.strip())
    with _host_locked_control(store):
        # Re-verificacion bajo lock: el arbol no puede haber cambiado entre
        # el hash pre-lock y la escritura del input/estado.
        if _code_hashes(code) != hashes:
            raise ValueError("codigo alterado durante preflight (bloque, sin reset)")
        # Fuente generada fija (determinista): reuse si identica, bloque si difiere.
        gen_dir = store / "search" / "generated"
        gen_dir.mkdir(parents=True, exist_ok=True)
        gen_path = gen_dir / GENERATED_FILENAME
        if gen_path.is_file():
            existing_sha = launch.file_hash(gen_path)
            if existing_sha != generated_sha:
                raise ValueError(
                    "fuente generada existente difiere del registry actual "
                    "(codigo alterado: requiere revision, no overwrite)")
        else:
            tmp = gen_path.with_name(f".{gen_path.name}.{os.getpid()}.tmp")
            tmp.write_text(rendered, encoding="utf-8")
            os.replace(tmp, gen_path)
        manifest = {
            "kind": INPUT_KIND,
            "status": "PREPARED",
            "input_id": uuid.uuid4().hex,
            "created_at": _utcnow_iso(),
            "commit": commit,
            "image_ref": image_ref.strip(),
            "image_id": image_id,
            "image_digest": launch.PINNED_IMAGE,
            "campaign_id": CAMPAIGN_ID,
            "definition_hash": definition_hash,
            "definition": definition,
            "generated_source_sha256": generated_sha,
            "generated_file": GENERATED_FILENAME,
            "snapshot_train_ref": snapshot_ref,
            "config_path": CONTAINER_CONFIG,
            "strategy_path": CONTAINER_GENERATED,
            **hashes,
        }
        if "force" in manifest:
            raise ValueError("campo 'force' prohibido")
        out = store / "search" / "inputs" / f"search-input-{_slug()}-{manifest['input_id'][:8]}.json"
        launch._atomic_create_new(out, manifest)
        state_path = store / "search" / "control" / f"campaign-{CAMPAIGN_ID}.json"
        if state_path.is_file():
            try:
                existing = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"estado existente ilegible: {exc}") from exc
            if str(existing.get("definition_hash")) != definition_hash:
                raise ValueError(
                    "definition_hash existente difiere (codigo/snapshot/fuente "
                    "cambiados tras registro): bloque significativo, sin reset "
                    "de budget; requiere nuevo spec aprobado")
            if str(existing.get("campaign_id")) != CAMPAIGN_ID:
                raise ValueError("campaign_id existente inesperado")
        else:
            from research.state import new_state

            state = new_state(CAMPAIGN_ID, definition_hash)
            state["input_id"] = manifest["input_id"]
            state["created_at"] = manifest["created_at"]
            launch._atomic_create_new(state_path, state)
    return str(out)


def _load_input() -> dict:
    path = Path(CONTAINER_INPUT)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"input ilegible: {exc}") from exc
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
    if manifest.get("campaign_id") != CAMPAIGN_ID:
        raise ValueError("input de otra campana")
    if not manifest.get("definition_hash"):
        raise ValueError("input sin definition_hash")
    if "force" in manifest:
        raise ValueError("campo 'force' prohibido en input")
    return manifest


def _verify_current_against_input(manifest_in: dict) -> dict:
    code = Path(CONTAINER_CODE)
    try:
        config = json.loads((code / SEARCH_CONFIG_REL).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config ilegible: {exc}") from exc
    validate_search_config(config, os.environ)
    current = _code_hashes(code)
    for key, value in current.items():
        expected = manifest_in.get(key)
        if not expected or expected != value:
            raise ValueError(
                f"codigo alterado tras prepare ({key}): bloque significativo, "
                "sin reset de budget; requiere nuevo spec")
    definition = manifest_in.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("input sin definition inmutable")
    if definition_hash_for(definition) != str(manifest_in.get("definition_hash")):
        raise ValueError("definition_hash no coincide con definition inmutable")
    # Re-ata bindings reales (no solo self-hash+DD).
    from research.campaign import generate_variants

    variants = generate_variants()
    rendered = _rendered_source_for(variants)
    generated_sha = _sha256_bytes(rendered.encode("utf-8"))
    if generated_sha != manifest_in.get("generated_source_sha256"):
        raise ValueError("fuente generada difiere del registry (bloque, sin reset)")
    gen_path = Path(CONTAINER_GENERATED) / GENERATED_FILENAME
    try:
        on_disk = gen_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError("fuente generada RO ausente en container") from None
    except OSError as exc:
        raise ValueError(f"fuente generada RO ilegible: {exc}") from exc
    if _sha256_bytes(on_disk.encode("utf-8")) != generated_sha:
        raise ValueError("fuente generada RO alterada")
    config_hash = launch.file_hash(code / SEARCH_CONFIG_REL)
    # Snapshot TRAIN: coteja manifiesto + datos en container.
    snap_manifest_path = Path(CONTAINER_SNAP_TRAIN_MANIFEST)
    snapshot_manifest = _load_snapshot_manifest_file(snap_manifest_path)
    _verify_snapshot_scope(snapshot_manifest, "train")
    if launch.file_hash(snap_manifest_path) != manifest_in.get("definition", {}).get(
            "snapshot_train_ref", {}).get("manifest_sha256"):
        raise ValueError("snapshot TRAIN manifiesto alterado")
    _verify_snapshot_data_files(Path(CONTAINER_SNAP_TRAIN), snapshot_manifest)
    snapshot_ref = {
        "manifest": str(manifest_in["definition"]["snapshot_train_ref"]["manifest"]),
        "manifest_sha256": launch.file_hash(snap_manifest_path),
        "snapshot_id": str(snapshot_manifest.get("snapshot_id")),
        "snapshot_dir": str(snapshot_manifest.get("snapshot_dir")),
        "whole_5m_sha256": str(snapshot_manifest.get("whole_5m_sha256")),
        "whole_1h_sha256": str(snapshot_manifest.get("whole_1h_sha256")),
        "range_start": str(snapshot_manifest.get("range_start")),
        "range_end": str(snapshot_manifest.get("range_end")),
        "rows_5m": int(snapshot_manifest.get("rows_5m", 0)),
        "rows_1h": int(snapshot_manifest.get("rows_1h", 0)),
        "segments": int(snapshot_manifest.get("segments", 0)),
    }
    _verify_definition_bindings(definition, snapshot_ref, generated_sha, config_hash,
                                current)
    if str(definition.get("config_hash")) != config_hash:
        raise ValueError("config_hash alterado (bloque, sin reset)")
    return current


def _load_state() -> tuple[dict, Path]:
    control = Path(CONTAINER_SEARCH) / "control"
    state_path = control / f"campaign-{CAMPAIGN_ID}.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"estado canonico ausente: {state_path}") from None
    except (OSError, ValueError) as exc:
        raise ValueError(f"estado ilegible: {exc}") from exc
    if str(state.get("campaign_id")) != CAMPAIGN_ID:
        raise ValueError("estado de otra campana")
    if "force" in state:
        raise ValueError("campo 'force' prohibido en estado")
    return state, state_path


def _save_state_atomic(state_path: Path, state: dict) -> None:
    launch._atomic_replace(Path(state_path), dict(state))


AUTHORITY_ROOT = "/lab-search-authority"


def _contained_under(root: Path, rel: str, label: str) -> Path:
    """Resuelve una ruta (relativa o absoluta) con contencion bajo root.

    Acepta absolutas contenidas (los tests usan tmp absolutos); escapes
    (`..` fuera de la raiz, symlinks externos) rechazan tras `resolve`,
    siempre antes de leer.
    """
    if not isinstance(rel, str) or not rel:
        raise ValueError(f"{label} ausente")
    root_res = Path(root).resolve()
    cand = Path(rel)
    target = (cand if cand.is_absolute() else (root_res / cand)).resolve()
    try:
        target.relative_to(root_res)
    except ValueError:
        raise ValueError(f"{label} fuera de la raiz") from None
    return target


def load_authority() -> dict:
    """Lee la autoridad canonica RO (control montado solo-lectura).

    Sin montaje falla cerrado (los roles externos quedan bloqueados; TRAIN
    nunca la necesita). Parcheable en tests via AUTHORITY_ROOT.
    """
    root = Path(str(AUTHORITY_ROOT))
    path = _contained_under(
        root, f"control/campaign-{CAMPAIGN_ID}.json", "estado autoridad")
    try:
        authority = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"autoridad ilegible: {exc}") from exc
    if not isinstance(authority, dict):
        raise ValueError("autoridad no es un objeto")
    return authority


def authorize_holdout(authority, role: str, grant) -> bool:
    """Autoriza un grant contra la autoridad canonica registrada.

    Exige: grant registrado IDENTICO en authority.grants[role], misma
    campana/definicion, y para TEST consumed True + mismo test_grant +
    candidata. La fase origen canonica es obligatoria y su SHA, estado y
    veredicto deben autorizar exactamente la promocion. Un dict con forma
    valida pero SIN registro rechaza siempre:
    `market.authorize_partition` puro nunca es autoridad.
    """
    if role not in ("validation", "test"):
        raise ValueError(f"grant solo para validation/test, no {role!r}")
    if not isinstance(authority, dict):
        raise ValueError("autoridad debe ser dict")
    if not isinstance(grant, dict):
        raise ValueError("grant debe ser dict")
    if "force" in grant:
        raise ValueError("campo 'force' prohibido en grant")
    for key in ("campaign_id", "grant_id", "definition_hash"):
        value = grant.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"grant sin {key} valido")
    if str(grant.get("campaign_id")) != str(authority.get("campaign_id")):
        raise ValueError("grant de otra campana")
    if str(grant.get("definition_hash")) != str(authority.get("definition_hash")):
        raise ValueError("grant con definition_hash distinto (inmutable)")
    stored = (authority.get("grants") or {}).get(role)
    if not isinstance(stored, dict) or stored != dict(grant):
        raise ValueError(f"grant no registrado como vigente para {role}")
    if role == "validation":
        frozen = sorted(str(v) for v in (authority.get("validation_ids") or []))
        if sorted(str(v) for v in (grant.get("validation_ids") or [])) != frozen:
            raise ValueError("grant fuera de validation_ids congelados")
    else:
        if authority.get("test_consumed") is not True:
            raise ValueError("TEST sin consumo registrado")
        tg = authority.get("test_grant") or {}
        if (tg.get("candidate_id") != grant.get("candidate_id")
                or tg.get("grant_id") != grant.get("grant_id")):
            raise ValueError("grant TEST diverge de la reserva")
        if "test_candidate" in authority:
            if str(authority.get("test_candidate")) != str(grant.get("candidate_id")):
                raise ValueError("grant TEST fuera de candidata reservada")
    source = "finalists" if role == "validation" else "validation"
    report_key = ("train_report_sha256" if role == "validation"
                  else "validation_report_sha256")
    entry = (authority.get("phase_reports") or {}).get(source)
    if not isinstance(entry, dict):
        raise ValueError(f"fase origen {source} sin referencia canonica")
    if entry.get("sha256") != grant.get(report_key):
        raise ValueError(f"grant cita report {source} distinto al registrado")
    if entry.get("status") != "SUCCEEDED":
        raise ValueError(f"report {source} registrado no SUCCEEDED")
    expected_verdict = "TOP3" if role == "validation" else "CANDIDATE"
    if entry.get("verdict") != expected_verdict:
        raise ValueError(
            f"report {source} no autoriza {role}: {entry.get('verdict')!r}")
    return True


def _expected_verdicts(phase: str) -> tuple:
    if phase == "screen":
        return ("TOP9", "NO_CANDIDATE")
    if phase == "finalists":
        return ("TOP3", "NO_CANDIDATE")
    if phase == "validation":
        return ("CANDIDATE", "NO_CANDIDATE")
    if phase == "test":
        return ("PASS", "FAIL", "INCONCLUSIVE")
    raise ValueError(f"fase desconocida: {phase!r}")


_POSITIVE_VERDICTS = {"screen": ("TOP9",), "finalists": ("TOP3",),
                      "validation": ("CANDIDATE",), "test": ("PASS",)}


def _resume_completed(phase: str, state: dict):
    """Artefacto autoritativo de una fase ya finalizada (sin re-ejecutar).

    Solo si state.phase_reports[phase] existe con status SUCCEEDED: resuelve
    la referencia canonica (path+SHA+schema+IDs) y retorna (payload, rc),
    con rc 0 en veredictos positivos y 1 en terminales negativos. Sin
    entrada (o entrada no SUCCEEDED) lanza ValueError: ejecutar normal con
    cache (un crash entre report y estado nunca adopta huerfanos por scan).
    """
    refs = state.get("phase_reports") or {}
    ref = refs.get(phase)
    if not isinstance(ref, dict) or ref.get("status") != "SUCCEEDED":
        raise ValueError(f"fase {phase} sin finalizacion SUCCEEDED registrada")
    payload, _sha = resolve_phase_report(state, phase)
    verdict = str(payload.get("verdict"))
    if phase == "test" and verdict == "PASS":
        if state.get("paper_ready") is not True:
            raise ValueError("TEST PASS sin publicacion paper_ready")
        candidate = str(payload.get("candidate_id") or "")
        grant_id = str(payload.get("grant_id") or "")
        evidence_sha = str((payload.get("lineage") or {}).get(
            "evidence_sha256") or "")
        if not candidate or not grant_id or not evidence_sha:
            raise ValueError("TEST PASS sin identidad de publicacion")
        control = Path(CONTAINER_SEARCH) / "control"
        evidence_path = control / f"test-evidence-{candidate}-{grant_id[:8]}.json"
        bundle_path = control / f"paper-bundle-{candidate}-{grant_id[:8]}.json"
        if _sha256_file(evidence_path) != evidence_sha:
            raise ValueError("evidencia TEST ausente o alterada")
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise ValueError(f"bundle paper ilegible: {exc}") from exc
        if (not isinstance(bundle, dict)
                or bundle.get("kind") != PAPER_BUNDLE_KIND
                or bundle.get("status") != "SEALED"
                or str(bundle.get("candidate_id") or "") != candidate
                or str((bundle.get("grant") or {}).get("grant_id") or "") != grant_id
                or str((bundle.get("reports") or {}).get("evidence_sha256") or "")
                != evidence_sha):
            raise ValueError("bundle paper no coincide con TEST PASS")
    shown = str(Path(CONTAINER_SEARCH) / str((state["phase_reports"][phase])["path"]))
    return payload, shown, (0 if verdict in _POSITIVE_VERDICTS[phase] else 1)


def _check_resolved_ids(phase: str, payload: dict, state: dict) -> None:
    """Consistencia semantica elegidos <=> congelados del estado."""
    if phase == "screen":
        frozen = state.get("train_ids")
        if not frozen:
            raise ValueError("screen resuelto sin top9 congelado")
        if sorted(str(v) for v in (payload.get("top9") or [])) != sorted(
                str(v) for v in frozen):
            raise ValueError("top9 resuelto difiere de congelados (IDs stale)")
    elif phase == "finalists":
        frozen = state.get("validation_ids")
        if not frozen:
            raise ValueError("finalists resuelto sin validation_ids congelados")
        if sorted(str(v) for v in (payload.get("validation_ids") or [])) != sorted(
                str(v) for v in frozen):
            raise ValueError("validation_ids resueltos difieren (IDs stale)")
    elif phase == "validation":
        if payload.get("verdict") == "CANDIDATE":
            cand = payload.get("chosen_test_candidate")
            if cand not in [str(v) for v in (state.get("validation_ids") or [])]:
                raise ValueError("candidata resuelta fuera de congelados (stale)")
    elif phase == "test":
        if payload.get("candidate_id") != str(state.get("test_candidate") or ""):
            raise ValueError("candidata resuelta difiere de la reservada")


def resolve_phase_report(state: dict, phase: str, search_root=None) -> tuple[dict, str]:
    """Resuelve el artefacto autoritativo de una fase completada.

    Usa EXCLUSIVAMENTE la referencia canonica persistida en
    state.phase_reports[phase] {path, sha256, status, verdict}: verifica
    contencion bajo search_root, existencia, SHA exacto del fichero,
    schema (definition/status/verdict) y consistencia de elegidos con el
    estado. Nunca glob-scanning de positivos ni re-hash de editados:
    un terminal NO_CANDIDATE registrado no rescata positivos antiguos, y
    un crash entre report y estado (sin entrada) re-ejecuta en vez de
    adoptar huerfanos. Solo SUCCEEDED es autoritativo.
    """
    if phase not in ("screen", "finalists", "validation", "test"):
        raise ValueError(f"fase desconocida: {phase!r}")
    if not isinstance(state, dict):
        raise ValueError("state debe ser dict")
    refs = state.get("phase_reports") or {}
    ref = refs.get(phase)
    if not isinstance(ref, dict):
        raise ValueError(f"fase {phase} sin referencia canonica (no re-ejecutar "
                         "a ciegas: falta finalizacion registrada)")
    for key in ("path", "sha256", "status", "verdict"):
        if not isinstance(ref.get(key), str) or not ref.get(key):
            raise ValueError(f"referencia {phase} sin {key} valido")
    if ref.get("status") != "SUCCEEDED":
        raise ValueError(f"fase {phase} registrada como {ref.get('status')}: "
                         "no autoritativa, re-ejecutar")
    if ref.get("verdict") not in _expected_verdicts(phase):
        raise ValueError(f"veredicto inesperado en referencia {phase}")
    root = Path(search_root if search_root is not None else CONTAINER_SEARCH)
    target = _contained_under(root, ref["path"], f"report {phase}")
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"report {phase} ilegible: {exc}") from exc
    if _sha256_bytes(raw) != ref["sha256"]:
        raise ValueError(f"report {phase} editado tras el sellado (SHA difiere)")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"report {phase} ilegible: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"report {phase} no es un objeto")
    if str(payload.get("definition_hash")) != str(state.get("definition_hash")):
        raise ValueError(f"report {phase} de otra definicion")
    if str(payload.get("status")) != "SUCCEEDED":
        raise ValueError(f"report {phase} no SUCCEEDED en fichero")
    if str(payload.get("verdict")) != ref["verdict"]:
        raise ValueError(f"report {phase} con veredicto distinto al registrado")
    _check_resolved_ids(phase, payload, state)
    return payload, ref["sha256"]


def _record_phase_report(state, state_path, phase: str, session_dir,
                         verdict: str, status: str = "SUCCEEDED") -> tuple[dict, str]:
    """Persiste el reporte terminal + su referencia canonica, bajo lock.

    Llamar con el estado fresco ya cargado bajo el lock adquirido; guarda el
    estado atomico tras registrar. Retorna (payload, sha256) del report.
    """
    report_path = Path(session_dir) / "report.json"
    try:
        raw = report_path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"report {phase} ilegible: {exc}") from exc
    sha = _sha256_bytes(raw)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"report {phase} ilegible: {exc}") from exc
    if str(payload.get("verdict") or "FAILED") != str(verdict):
        raise ValueError(f"report {phase} con veredicto inesperado")
    if str(payload.get("status")) != str(status):
        raise ValueError(f"report {phase} con estado inesperado")
    try:
        rel = str(report_path.resolve().relative_to(
            Path(CONTAINER_SEARCH).resolve()))
    except ValueError:
        raise ValueError(f"report {phase} fuera de la raiz de busqueda") from None
    refs = state.get("phase_reports")
    if not isinstance(refs, dict):
        refs = {}
        state["phase_reports"] = refs
    refs[str(phase)] = {"path": rel, "sha256": sha,
                        "status": str(status), "verdict": str(verdict)}
    _save_state_atomic(state_path, state)
    return payload, sha


def _batch_names(variant_ids, batch_size=BATCH_MAX):
    ordered = sorted(str(v) for v in variant_ids)
    batches = []
    for idx in range(0, len(ordered), batch_size):
        batches.append(ordered[idx:idx + batch_size])
    return batches


def _phase_guard(manifest_in: dict, state: dict, phase: str) -> None:
    if phase not in PHASES:
        raise ValueError(f"fase desconocida: {phase!r}")
    if str(manifest_in.get("definition_hash")) != str(state.get("definition_hash")):
        raise ValueError("input/estado con definition_hash distinto (inmutable)")
    if "force" in state or "force" in manifest_in:
        raise ValueError("campo 'force' prohibido")
    stage = str(state.get("stage") or "REGISTERED")
    order = {"REGISTERED": 0, "TRAIN_FROZEN": 1, "VALIDATION_FROZEN": 2,
             "TEST_RESERVED": 3}
    need = {"screen": 0, "finalists": 0, "validation": 1, "test": 2}
    if order.get(stage, -1) < need[phase]:
        raise ValueError(f"fase {phase} no autorizada en stage {stage} (ver status)")
    if phase in ("validation", "test") and stage not in ("TRAIN_FROZEN",
                                                         "VALIDATION_FROZEN",
                                                         "TEST_RESERVED"):
        raise ValueError(f"holdout {phase} bloqueado sin fase TRAIN/VAL verificada")


def _native_argv_for_batch(window, fee, strategy_names, strategy_path,
                           user_dir, export_dir, use_list=True,
                           snap_root=CONTAINER_SNAP_TRAIN) -> list:
    names = list(strategy_names)
    if use_list and (len(names) < 1 or len(names) > BATCH_MAX):
        raise ValueError(f"batch debe tener 1..{BATCH_MAX}, fue {len(names)}")
    if not use_list and len(names) != 1:
        raise ValueError("control single exige exactamente 1 estrategia")
    from research.evaluation import timerange_fmt

    timerange = timerange_fmt(window["start"], window["end_exclusive"])
    if re.fullmatch(r"\d{8}T\d{4}-\d{8}T\d{4}", timerange) is None:
        raise ValueError(f"timerange nativo invalido: {timerange!r}")
    seg_dir = str(Path(snap_root) / window["seg_dir"])
    argv = [
        "freqtrade", "backtesting",
        "--config", CONTAINER_CONFIG,
        "--datadir", seg_dir,
        "--userdir", str(user_dir),
        "--strategy-path", str(strategy_path),
        "--timeframe", TIMEFRAME_1H,
        "--timeframe-detail", TIMEFRAME_5M,
        "--timerange", timerange,
        "--fee", repr(float(fee)),
        "--export", "trades",
        "--export-directory", str(export_dir),
        "--cache", "none",
    ]
    if use_list:
        # argv: ... --userdir X --strategy-list n1..n6 --strategy-path P ...
        argv.insert(argv.index("--strategy-path"), "--strategy-list")
        idx = argv.index("--strategy-list")
        argv[idx + 1:idx + 1] = names
    else:
        argv.insert(argv.index("--strategy-path"), "--strategy")
        idx = argv.index("--strategy")
        argv[idx + 1:idx + 1] = names
    return argv


def _run_native_batch(argv, timeout_s, log_path):
    from operations.research import _run_streaming

    return _run_streaming(list(argv), int(timeout_s), Path(log_path))


def _load_prices_for_window(seg_dir_path, window):
    """5m EVAL + 1h EVAL con OPENs efectivos (requiere pandas en container)."""
    import pandas as pd

    seg = Path(seg_dir_path)
    f5 = seg / _pair_feather(PAIR, TIMEFRAME_5M)
    f1 = seg / _pair_feather(PAIR, TIMEFRAME_1H)
    if not f5.is_file() or not f1.is_file():
        raise ValueError("segmento sin feathers 5m/1h")
    df5 = pd.read_feather(str(f5))
    df1 = pd.read_feather(str(f1))
    start = pd.Timestamp(window["start"])
    end = pd.Timestamp(window["end_exclusive"])
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    s5 = pd.to_datetime(df5["date"], utc=True)
    s1 = pd.to_datetime(df1["date"], utc=True)
    m5 = (s5 >= start) & (s5 < end)
    m1 = (s1 >= start) & (s1 < end)
    eval5 = df5.loc[m5].sort_values("date").reset_index(drop=True)
    eval1 = df1.loc[m1].sort_values("date").reset_index(drop=True)
    if len(eval5) == 0 or len(eval1) == 0:
        raise ValueError("ventana sin velas EVAL (short)")
    return eval5, eval1, float(eval1["open"].iloc[0]), float(eval1["open"].iloc[-1])


def _ledger_for_trades(prices_5m_eval, trades, start_iso, end_iso):
    from market.equity import build_ledger

    return build_ledger(prices_5m_eval, list(trades), 10000.0, start_iso, end_iso)


def _batch_evidence_hash(image_id, generated_sha, config_hash, code_hashes,
                         seg_meta, window, timerange, fee, strategy_names,
                         strategy_params) -> str:
    return _sha256_bytes(_canonical({
        "image_id": str(image_id),
        "generated_source_sha256": str(generated_sha),
        "config_hash": str(config_hash),
        "search_hash": str(code_hashes.get("search_hash")),
        "evaluation_hash": str(code_hashes.get("evaluation_hash")),
        "campaign_hash": str(code_hashes.get("campaign_hash")),
        "selection_hash": str(code_hashes.get("selection_hash")),
        "candidates_hash": str(code_hashes.get("candidates_hash")),
        "control_hash": str(code_hashes.get("control_hash")),
        "equity_hash": str(code_hashes.get("equity_hash")),
        "data_5m": str(seg_meta.get("file_5m_sha256")),
        "data_1h": str(seg_meta.get("file_1h_sha256")),
        "window_start": str(window["start"]),
        "window_end": str(window["end_exclusive"]),
        "timerange": str(timerange),
        "fee": float(fee),
        "strategies": sorted(str(n) for n in strategy_names),
        "params": strategy_params,
    }))


def _sanitize_run_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", key)


class _JobFail(Exception):
    """Fallo terminal de un job nativo ya invocado (con causa registrable)."""

    def __init__(self, cause: str):
        super().__init__(cause)
        self.cause = str(cause)


def _output_digest(run_key: str, evidence: str, results_min: dict,
                   zip_relpath: str, zip_sha256: str) -> dict:
    """Digest de salida separado de la evidencia de entrada."""
    return {
        "zip_relpath": str(zip_relpath),
        "zip_sha256": str(zip_sha256),
        "payload_sha256": _sha256_bytes(_canonical({
            "run_key": str(run_key),
            "evidence_hash": str(evidence),
            "results": {str(n): {
                "profit_abs": float(v["profit_abs"]),
                "trades_count": int(v["trades_count"]),
                "trades": list(v.get("trades") or []),
            } for n, v in dict(results_min).items()},
        })),
    }


def _verify_cached_output(prior: dict, expected_output, run_key: str,
                          evidence: str, names) -> dict | None:
    """Re-verifica un exito cacheado contra el ZIP autoritativo.

    Exige digest de salida esperado en el estado, ruta canonica contenida,
    SHA del fichero, re-parseo con nombres/conteos y hash del payload
    recalculado desde lo reparseado (nunca los datos cacheados). Retorna
    {name: resumen} o None si manipulado/invalido (sin promocion).
    """
    if not isinstance(expected_output, dict):
        return None
    zip_rel = expected_output.get("zip_relpath")
    zip_sha = expected_output.get("zip_sha256")
    payload_sha = expected_output.get("payload_sha256")
    if not zip_rel or not zip_sha or not payload_sha:
        return None
    try:
        target = _contained_under(Path(CONTAINER_SEARCH), str(zip_rel), "zip cache")
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        digest = launch.file_hash(target)
    except OSError:
        return None
    if digest != zip_sha:
        return None
    from research.evaluation import parse_native_batch as _parse

    parsed, perr = _parse(target, list(names))
    if perr is not None or parsed is None:
        return None
    results_min = {n: {
        "profit_abs": parsed[n]["profit_abs"],
        "trades_count": parsed[n]["trades_count"],
        "trades": parsed[n]["trades"],
    } for n in names}
    if _sha256_bytes(_canonical({
            "run_key": str(run_key),
            "evidence_hash": str(evidence),
            "results": {str(n): {
                "profit_abs": float(v["profit_abs"]),
                "trades_count": int(v["trades_count"]),
                "trades": list(v.get("trades") or []),
            } for n, v in dict(results_min).items()},
    })) != payload_sha:
        return None
    return results_min


def _sha256_file(path) -> str:
    return launch.file_hash(Path(path))


def _load_verified_report(session_glob: str, definition_hash: str,
                          predicate, label: str,
                          search_root=None) -> tuple[dict, str]:
    """Ultimo report SUCCEEDED que cumple el predicado + su SHA inmutable.

    Ordena por created_at (desempate por nombre); nunca elige por contenido
    economico. Lanza ValueError si ninguno cumple (fail-closed, sin holdout).
    """
    sessions = Path(search_root if search_root is not None else CONTAINER_SEARCH) / "sessions"
    cands = []
    if sessions.is_dir():
        for rep_path in sorted(sessions.glob(session_glob)):
            try:
                payload = json.loads(rep_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("status") != "SUCCEEDED":
                continue
            if str(payload.get("definition_hash")) != str(definition_hash):
                continue
            try:
                if not predicate(payload):
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            cands.append((str(payload.get("created_at") or ""), rep_path, payload))
    if not cands:
        raise ValueError(f"sin report {label} verificado (SUCCEEDED + definicion)")
    cands.sort(key=lambda t: (t[0], str(t[1])))
    _, rep_path, payload = cands[-1]
    return payload, _sha256_file(rep_path)


def _load_search_input_file(path) -> dict:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"search input ilegible: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("search input no es un objeto")
    if manifest.get("kind") != INPUT_KIND:
        raise ValueError("search input con kind inesperado")
    if manifest.get("status") != "PREPARED":
        raise ValueError("search input no esta PREPARED")
    if manifest.get("image_ref") != launch.PINNED_IMAGE:
        raise ValueError("search input sin imagen fijada")
    if not launch._COMMIT_RE.fullmatch(str(manifest.get("commit") or "")):
        raise ValueError("search input sin commit valido")
    if not manifest.get("input_id"):
        raise ValueError("search input sin input_id")
    if manifest.get("campaign_id") != CAMPAIGN_ID:
        raise ValueError("search input de otra campana")
    if not manifest.get("definition_hash"):
        raise ValueError("search input sin definition_hash")
    if "force" in manifest:
        raise ValueError("campo 'force' prohibido en search input")
    return manifest


def _verify_search_input_host(search_input: dict, code: Path, store: Path) -> dict:
    """Bindings de definicion en host (sin importar freqtrade/pandas)."""
    try:
        config = json.loads((code / SEARCH_CONFIG_REL).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ValueError(f"config search ilegible: {exc}") from exc
    validate_search_config(config, os.environ)
    current = _code_hashes(code)
    for key, value in current.items():
        if not search_input.get(key) or search_input.get(key) != value:
            raise ValueError(f"codigo alterado tras prepare ({key}): bloque sin reset")
    definition = search_input.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("search input sin definition inmutable")
    if definition_hash_for(definition) != str(search_input.get("definition_hash")):
        raise ValueError("definition_hash no coincide con definition inmutable")
    from research.campaign import generate_variants

    variants = generate_variants()
    rendered = _rendered_source_for(variants)
    generated_sha = _sha256_bytes(rendered.encode("utf-8"))
    if generated_sha != search_input.get("generated_source_sha256"):
        raise ValueError("fuente generada difiere del registry (bloque, sin reset)")
    config_hash = launch.file_hash(code / SEARCH_CONFIG_REL)
    ref = definition.get("snapshot_train_ref") or {}
    snaps = store / "history" / "snapshots"
    manifest_path = snaps / _safe_manifest_basename(str(ref.get("manifest") or ""))
    snap_manifest = _load_snapshot_manifest_file(manifest_path)
    _verify_snapshot_scope(snap_manifest, "train")
    live_ref = {
        "manifest": manifest_path.name,
        "manifest_sha256": launch.file_hash(manifest_path),
        "snapshot_id": str(snap_manifest.get("snapshot_id")),
        "snapshot_dir": str(snap_manifest.get("snapshot_dir")),
        "whole_5m_sha256": str(snap_manifest.get("whole_5m_sha256")),
        "whole_1h_sha256": str(snap_manifest.get("whole_1h_sha256")),
        "range_start": str(snap_manifest.get("range_start")),
        "range_end": str(snap_manifest.get("range_end")),
        "rows_5m": int(snap_manifest.get("rows_5m", 0)),
        "rows_1h": int(snap_manifest.get("rows_1h", 0)),
        "segments": int(snap_manifest.get("segments", 0)),
    }
    _verify_definition_bindings(definition, live_ref, generated_sha, config_hash,
                                current)
    return current


def _find_history_input_by_grant(store: Path, role: str, grant_id: str):
    for path in sorted((store / "history" / "inputs").glob(f"history-input-{role}-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        grant = payload.get("grant") if isinstance(payload, dict) else None
        if isinstance(grant, dict) and str(grant.get("grant_id") or "") == str(grant_id):
            return path
    return None


def _resolve_host_grant(control: Path, prefix: str, definition_hash: str,
                        expected_ids, grant_id_or_none):
    cands = []
    for path in sorted(control.glob(f"{prefix}*.json")):
        try:
            grant = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(grant, dict):
            continue
        if str(grant.get("definition_hash")) != str(definition_hash):
            continue
        cands.append((path, grant))
    if grant_id_or_none:
        for path, grant in cands:
            if str(grant.get("grant_id") or "") == str(grant_id_or_none):
                return path, grant
        raise ValueError("grant-id sin coincidencia (sin elegir por defecto)")
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise ValueError("sin grants para el rol/definicion (prepare-phase paso 1 pendiente)")
    raise ValueError(f"{len(cands)} grants candidatos: exige --grant-id explicito")


def _verify_role_snapshot_host(store: Path, role: str, basename: str) -> dict:
    snaps = store / "history" / "snapshots"
    manifest_path = snaps / _safe_manifest_basename(basename)
    manifest = _load_snapshot_manifest_file(manifest_path)
    _verify_snapshot_scope(manifest, role)
    _verify_snapshot_data_files(snaps / str(manifest.get("snapshot_dir") or ""), manifest)
    from market.history import partition_bounds

    start, end = partition_bounds(role)
    if str(manifest.get("range_start")) != start.isoformat():
        raise ValueError(f"snapshot {role} range_start fuera de bounds")
    if str(manifest.get("range_end")) != end.isoformat():
        raise ValueError(f"snapshot {role} range_end fuera de bounds")
    return {
        "manifest": manifest_path.name,
        "manifest_sha256": launch.file_hash(manifest_path),
        "snapshot_id": str(manifest.get("snapshot_id")),
        "snapshot_dir": str(manifest.get("snapshot_dir")),
        "whole_5m_sha256": str(manifest.get("whole_5m_sha256")),
        "whole_1h_sha256": str(manifest.get("whole_1h_sha256")),
        "range_start": str(manifest.get("range_start")),
        "range_end": str(manifest.get("range_end")),
        "rows_5m": int(manifest.get("rows_5m", 0)),
        "rows_1h": int(manifest.get("rows_1h", 0)),
        "segments": int(manifest.get("segments", 0)),
    }


def cmd_prepare_phase(phase: str, search_input_path: str, code_root: str,
                      storage_root: str, image_ref: str, snapshot,
                      grant_id) -> str:
    """Autorizacion holdout en host (dos pasos por rol, sin flags manuales)."""
    if phase not in ("validation", "test"):
        raise ValueError(f"prepare-phase solo validation/test, no {phase!r}")
    if not isinstance(image_ref, str) or image_ref.strip() != launch.PINNED_IMAGE:
        raise ValueError("image_ref debe ser exactamente la imagen fijada con digest")
    code = Path(code_root)
    store = Path(storage_root)
    if not code.is_dir():
        raise FileNotFoundError(f"codigo no encontrado: {code}")
    search_input = _load_search_input_file(search_input_path)
    pre_hashes = _verify_search_input_host(search_input, code, store)
    definition_hash = str(search_input.get("definition_hash"))
    commit = launch._git_commit(code)
    launch._git_clean(code)
    image_id = launch._inspect_image(image_ref.strip())
    if image_id != str(search_input.get("image_id")):
        raise ValueError("image_id actual difiere del input (re-prepare requerido)")
    _ = commit
    control = store / "search" / "control"
    search_root = store / "search"
    with _host_locked_control(store):
        # Re-verificacion bajo lock: codigo inmutable entre preflight y grant.
        if _code_hashes(code) != pre_hashes:
            raise ValueError("codigo alterado durante preflight (bloque, sin reset)")
        try:
            state = json.loads((control / f"campaign-{CAMPAIGN_ID}.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FileNotFoundError("estado canonico ausente (prepare pendiente)") from None
        except (OSError, ValueError) as exc:
            raise ValueError(f"estado ilegible: {exc}") from exc
        if str(state.get("definition_hash")) != definition_hash:
            raise ValueError("estado con definition_hash distinto (bloque, sin reset)")
        if "force" in state:
            raise ValueError("campo 'force' prohibido en estado")
        state_path = control / f"campaign-{CAMPAIGN_ID}.json"
        if phase == "validation":
            return _prepare_phase_validation(
                search_input, definition_hash, code, store, control,
                search_root, state, state_path, image_ref, snapshot, grant_id)
        return _prepare_phase_test(
            search_input, definition_hash, code, store, control,
            search_root, state, state_path, image_ref, snapshot, grant_id)


def _prepare_phase_validation(search_input, definition_hash, code, store, control,
                              search_root, state, state_path, image_ref,
                              snapshot, grant_id):
    from operations.history import prepare_history_with_grant

    if str(state.get("stage")) != "VALIDATION_FROZEN":
        raise ValueError(f"prepare-phase validation exige stage VALIDATION_FROZEN "
                         f"(finalists primero; actual {state.get('stage')!r})")
    validation_ids = sorted(str(v) for v in (state.get("validation_ids") or []))
    if not validation_ids:
        raise ValueError("finalists pendiente (validation_ids vacio)")
    _frep, _frep_sha = resolve_phase_report(state, "finalists",
                                              search_root=search_root)
    _srep, srep_sha = resolve_phase_report(state, "screen",
                                           search_root=search_root)
    if str(_frep.get("screen_report_sha256") or "") != srep_sha:
        raise ValueError("nuevo screen tras finalists: re-ejecutar finalists")
    if snapshot is None:
        registered = (state.get("grants") or {}).get("validation")
        if isinstance(registered, dict):
            if (sorted(str(v) for v in (registered.get("validation_ids") or []))
                    == validation_ids
                    and str(registered.get("train_report_sha256") or "") == _frep_sha
                    and str(registered.get("definition_hash")) == definition_hash):
                grant = registered
                grant_path = control / f"validation-grant-{grant['grant_id'][:8]}.json"
                if (not grant_path.is_file() or json.loads(
                        grant_path.read_text(encoding="utf-8")) != grant):
                    raise ValueError("grant registrado sin fichero canonico")
                existing_hist = _find_history_input_by_grant(
                    store, "validation", str(grant.get("grant_id")))
                if existing_hist is not None:
                    return json.dumps({"grant": str(grant_path),
                                       "history_input": str(existing_hist),
                                       "resumed": True}, indent=2, sort_keys=True)
                hpath = prepare_history_with_grant(
                    str(code), str(store), image_ref, "validation", grant,
                    list(validation_ids), _frep_sha, definition_hash)
                return json.dumps({"grant": str(grant_path),
                                   "history_input": str(hpath),
                                   "resumed": True}, indent=2, sort_keys=True)
            raise ValueError("grant validation registrado difiere (inmutable: "
                             "un solo vigente por rol)")
        grant = {
            "campaign_id": CAMPAIGN_ID,
            "grant_id": uuid.uuid4().hex,
            "definition_hash": definition_hash,
            "phase": "validation",
            "validation_ids": list(validation_ids),
            "train_report_sha256": _frep_sha,
        }
        grant_path = control / f"validation-grant-{grant['grant_id'][:8]}.json"
        launch._atomic_create_new(grant_path, grant)
        grants = state.get("grants")
        if not isinstance(grants, dict):
            grants = {}
            state["grants"] = grants
        grants["validation"] = dict(grant)
        _save_state_atomic(state_path, state)
        hpath = prepare_history_with_grant(
            str(code), str(store), image_ref, "validation", grant,
            list(validation_ids), _frep_sha, definition_hash)
        return json.dumps({"grant": str(grant_path), "history_input": str(hpath)},
                          indent=2, sort_keys=True)
    grant_path, grant = _resolve_host_grant(
        control, "validation-grant-", definition_hash, validation_ids, grant_id)
    if (state.get("grants") or {}).get("validation") != dict(grant):
        raise ValueError("grant no registrado como vigente en el estado")
    from operations.history import verify_holdout_grant

    verify_holdout_grant("validation", grant, definition_hash,
                         validation_ids, _frep_sha)
    if str(grant.get("train_report_sha256") or "") != _frep_sha:
        raise ValueError("grant con train_report_sha256 de otros finalists")
    live = _verify_role_snapshot_host(store, "validation", snapshot)
    binding = {
        "kind": SNAP_BIND_KIND,
        "grant_id": str(grant.get("grant_id")),
        "definition_hash": definition_hash,
        "expected_ids": list(validation_ids),
        "train_report_sha256": _frep_sha,
        **live,
    }
    bind_path = control / f"validation-snapshot-{str(grant.get('grant_id'))[:8]}.json"
    if bind_path.is_file():
        try:
            prev = json.loads(bind_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"binding existente ilegible: {exc}") from exc
        if prev != binding:
            raise ValueError("binding existente difiere (inmutable, sin overwrite)")
    else:
        launch._atomic_create_new(bind_path, binding)
    return json.dumps({"grant": str(grant_path), "binding": str(bind_path)},
                      indent=2, sort_keys=True)


def _prepare_phase_test(search_input, definition_hash, code, store, control,
                        search_root, state, state_path, image_ref,
                        snapshot, grant_id):
    from operations.history import prepare_history_with_grant
    from research.state import authorize_test_resume, reserve_test

    want_ids = sorted(str(v) for v in (state.get("validation_ids") or []))
    if not want_ids:
        raise ValueError("validation pendiente (validation_ids vacio)")
    vrep, vrep_sha = resolve_phase_report(state, "validation",
                                          search_root=search_root)
    candidate = str(vrep.get("chosen_test_candidate") or "")
    if not candidate or candidate not in want_ids:
        raise ValueError("validation sin candidata elegible verificada")
    if snapshot is None:
        if state.get("test_consumed") is True:
            if str(state.get("test_candidate")) != candidate:
                raise ValueError("TEST consumido con otra candidata (switch prohibido)")
            if not authorize_test_resume(state, candidate,
                                         str(state.get("test_grant_id") or ""),
                                         definition_hash):
                raise ValueError("resume TEST no autoriza (grant/definicion)")
            grant_path, grant = _resolve_host_grant(
                control, "test-grant-", definition_hash, [candidate], grant_id)
            if str(grant.get("validation_report_sha256") or "") != vrep_sha:
                raise ValueError("nuevo validation tras reserva: bloque")
            if (state.get("grants") or {}).get("test") != dict(grant):
                raise ValueError("grant TEST no registrado como vigente")
            existing_hist = _find_history_input_by_grant(store, "test",
                                                         str(grant.get("grant_id")))
            if existing_hist is not None:
                return json.dumps({"grant": str(grant_path),
                                   "history_input": str(existing_hist),
                                   "resumed": True}, indent=2, sort_keys=True)
            hpath = prepare_history_with_grant(
                str(code), str(store), image_ref, "test", grant,
                [candidate], vrep_sha, definition_hash)
            return json.dumps({"grant": str(grant_path),
                               "history_input": str(hpath),
                               "resumed": True}, indent=2, sort_keys=True)
        if str(state.get("stage")) != "VALIDATION_FROZEN":
            raise ValueError(f"prepare-phase test exige stage VALIDATION_FROZEN "
                             f"(actual {state.get('stage')!r})")
        # Reserva ATOMICA antes de cualquier descarga/lectura posterior.
        reserved = reserve_test(state, candidate)
        _save_state_atomic(state_path, state)
        grant = dict(reserved)
        grant["phase"] = "test"
        grant["validation_report_sha256"] = vrep_sha
        state["test_grant"] = {k: grant[k] for k in (
            "campaign_id", "candidate_id", "grant_id", "definition_hash",
            "phase", "validation_report_sha256")}
        grants = state.get("grants")
        if not isinstance(grants, dict):
            grants = {}
            state["grants"] = grants
        grants["test"] = dict(grant)
        _save_state_atomic(state_path, state)
        grant_path = control / f"test-grant-{grant['grant_id'][:8]}.json"
        launch._atomic_create_new(grant_path, grant)
        hpath = prepare_history_with_grant(
            str(code), str(store), image_ref, "test", grant,
            [candidate], vrep_sha, definition_hash)
        return json.dumps({"grant": str(grant_path), "history_input": str(hpath),
                           "reserved": True}, indent=2, sort_keys=True)
    if state.get("test_consumed") is not True:
        raise ValueError("TEST sin reserva consumida (prepare-phase paso 1 pendiente)")
    if not authorize_test_resume(state, candidate,
                                 str(state.get("test_grant_id") or ""),
                                 definition_hash):
        raise ValueError("resume TEST no autoriza (grant/definicion)")
    grant_path, grant = _resolve_host_grant(
        control, "test-grant-", definition_hash, [candidate], grant_id)
    if str(grant.get("grant_id")) != str(state.get("test_grant_id")):
        raise ValueError("grant diverge de la reserva (switch prohibido)")
    if (state.get("grants") or {}).get("test") != dict(grant):
        raise ValueError("grant TEST no registrado como vigente")
    if str(grant.get("validation_report_sha256") or "") != vrep_sha:
        raise ValueError("nuevo validation tras reserva: bloque")
    live = _verify_role_snapshot_host(store, "test", snapshot)
    binding = {
        "kind": SNAP_BIND_KIND,
        "grant_id": str(grant.get("grant_id")),
        "definition_hash": definition_hash,
        "expected_ids": [candidate],
        "validation_report_sha256": vrep_sha,
        **live,
    }
    bind_path = control / f"test-snapshot-{str(grant.get('grant_id'))[:8]}.json"
    if bind_path.is_file():
        try:
            prev = json.loads(bind_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"binding existente ilegible: {exc}") from exc
        if prev != binding:
            raise ValueError("binding existente difiere (inmutable, sin overwrite)")
    else:
        launch._atomic_create_new(bind_path, binding)
    return json.dumps({"grant": str(grant_path), "binding": str(bind_path)},
                      indent=2, sort_keys=True)


def _load_screen_report_verified(state: dict,
                                   search_root=None) -> tuple[dict, str]:
    train_ids = state.get("train_ids")
    if not train_ids:
        raise ValueError("screen sin top9 congelado (train_ids vacio)")
    want = sorted(str(v) for v in train_ids)

    def _pred(payload: dict) -> bool:
        return (payload.get("verdict") == "TOP9"
                and sorted(str(v) for v in (payload.get("top9") or [])) == want
                and isinstance(payload.get("records"), list))

    return _load_verified_report("screen-*/report.json",
                                 str(state.get("definition_hash")), _pred,
                                 "screen/TOP9", search_root=search_root)


def _load_finalists_report_verified(state: dict,
                                    search_root=None) -> tuple[dict, str]:
    validation_ids = state.get("validation_ids")
    if not validation_ids:
        raise ValueError("finalists sin validation_ids congelados")
    want = sorted(str(v) for v in validation_ids)

    def _pred(payload: dict) -> bool:
        return (payload.get("verdict") == "TOP3"
                and sorted(str(v) for v in (payload.get("validation_ids") or [])) == want
                and isinstance(payload.get("bias_verdicts"), dict))

    return _load_verified_report("finalists-*/report.json",
                                 str(state.get("definition_hash")), _pred,
                                 "finalists/TOP3", search_root=search_root)


def _load_validation_report_verified(state: dict,
                                     search_root=None) -> tuple[dict, str]:
    def _pred(payload: dict) -> bool:
        cand = payload.get("chosen_test_candidate")
        return (payload.get("verdict") == "CANDIDATE"
                and isinstance(cand, str) and bool(cand)
                and cand in [str(v) for v in (state.get("validation_ids") or [])])

    return _load_verified_report("validation-*/report.json",
                                 str(state.get("definition_hash")), _pred,
                                 "validation/CANDIDATE", search_root=search_root)


def _native_argv_lookahead(seg_dir: str, strategy_class: str, user_dir,
                           timerange: str, targeted: int, csv_path) -> list:
    if targeted < LOOKAHEAD_MIN_AMOUNT:
        raise ValueError(f"targeted {targeted} < minimo {LOOKAHEAD_MIN_AMOUNT}")
    return [
        "freqtrade", "lookahead-analysis",
        "--config", CONTAINER_CONFIG,
        "--datadir", str(seg_dir),
        "--userdir", str(user_dir),
        "--strategy", str(strategy_class),
        "--strategy-path", CONTAINER_GENERATED,
        "--timeframe", TIMEFRAME_1H,
        "--timeframe-detail", TIMEFRAME_5M,
        "--timerange", str(timerange),
        "--minimum-trade-amount", str(LOOKAHEAD_MIN_AMOUNT),
        "--targeted-trade-amount", str(int(targeted)),
        "--lookahead-analysis-exportfilename", str(csv_path),
    ]


def _native_argv_recursive(seg_dir: str, strategy_class: str, user_dir,
                           timerange: str) -> list:
    from research.evaluation import RECURSIVE_WINDOWS

    if tuple(RECURSIVE_WINDOWS) != (201, 400, 800, 1200):
        raise ValueError("ventanas recursive fuera de cierre 201/400/800/1200")
    return [
        "freqtrade", "recursive-analysis",
        "--config", CONTAINER_CONFIG,
        "--datadir", str(seg_dir),
        "--userdir", str(user_dir),
        "--strategy", str(strategy_class),
        "--strategy-path", CONTAINER_GENERATED,
        "--timeframe", TIMEFRAME_1H,
        "--timerange", str(timerange),
        "--startup-candle", *[str(s) for s in RECURSIVE_WINDOWS],
    ]


def _load_full_1h(seg_dir_path):
    """Frame 1h completo del segmento para own_strategy_gate (container)."""
    import pandas as pd

    target = Path(seg_dir_path) / _pair_feather(PAIR, TIMEFRAME_1H)
    if not target.is_file():
        raise ValueError("segmento sin feather 1h")
    frame = pd.read_feather(str(target))
    return frame.sort_values("date").reset_index(drop=True)


def _execute_native_matrix(*, phase, role, windows, fees, groups, snap_root,
                           session_dir, batch_dir, state, state_path,
                           manifest_in, code_hashes, seg_meta_by_dir,
                           by_class_params):
    """Matriz nativa batch/single con budget/cache/progreso compartidos.

    groups: [{names:[...], use_list:bool, spath:str, params:dict, suffix:str}]
    Retorna (results, stats): results[(year, seg_dir, fee, name)] con
    profit_abs/trades/zip/evidencia; stats {done, succ, fail, causes}.
    Lanza ValueError en budget agotado o misma causa x3 (abortar fase).
    """
    from research.state import can_reuse_run, record_attempt

    jobs = []
    for window in windows:
        for fee in fees:
            for grp in groups:
                jobs.append((window, float(fee), grp))
    total = len(jobs)
    done = succ = fail = 0
    causes: dict = {}
    progress_path = session_dir / "progress.json"

    def _progress():
        launch._atomic_replace(progress_path, {
            "phase": phase, "session": session_dir.name,
            "done": done, "succeeded": succ, "failed": fail,
            "total": total, "remaining": total - done,
            "consumed": state.get("consumed"),
            "remaining_budget_s": float(state.get("budget_limit", BUDGET_SECONDS)) - float(state.get("consumed", 0)),
            "failures_by_cause": dict(causes),
            "updated_at": _utcnow_iso(),
        })

    _progress()
    results: dict = {}

    def _prior_batch_file(run_key, evidence):
        for prev in sorted(Path(CONTAINER_SEARCH).joinpath("sessions").glob(f"{phase}-*/batches/*.json")):
            try:
                payload = json.loads(Path(prev).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if payload.get("run_key") != run_key:
                continue
            if payload.get("evidence_hash") != evidence:
                continue
            if payload.get("status") != "SUCCEEDED":
                continue
            return payload
        return None

    from research.evaluation import parse_native_batch as _parse
    from research.evaluation import timerange_fmt

    for window, fee, grp in jobs:
        year = int(window["year"])
        seg_dir = str(window["seg_dir"])
        names = list(grp["names"])
        use_list = bool(grp["use_list"])
        spath = str(grp["spath"])
        params = dict(grp.get("params") or {})
        suffix = str(grp.get("suffix") or "batch")
        timerange = timerange_fmt(window["start"], window["end_exclusive"])
        seg_meta = seg_meta_by_dir.get(seg_dir) or {}
        extra_params = dict(params)
        for n in names:
            if n in by_class_params:
                extra_params[n] = by_class_params[n]
        run_key = f"{phase}|{role}|y{year}|{seg_dir}|fee{fee:.4f}|{suffix}"
        evidence = _batch_evidence_hash(
            manifest_in.get("image_id"), manifest_in.get("generated_source_sha256"),
            manifest_in.get("config_hash"), code_hashes, seg_meta,
            window, timerange, fee, names, extra_params)
        batch_payload = None
        reused = False
        if can_reuse_run(state, run_key, evidence):
            prior = _prior_batch_file(run_key, evidence)
            if prior is not None:
                expected_output = ((state.get("native_runs") or {}).get(run_key)
                                   or {}).get("output")
                verified = _verify_cached_output(prior, expected_output,
                                                 run_key, evidence, names)
                if verified is not None:
                    batch_payload = dict(prior)
                    batch_payload["results"] = verified
                    reused = True
                    done += 1
                    succ += 1
                    _progress()
                else:
                    # Cache invalida o manipulada: FAILED sin cargo (ningun
                    # proceso invocado), sin promocion ni fallback a otro
                    # positivo antiguo. Motivo fuera de NativeCalls.
                    fail += 1
                    done += 1
                    causes["cache-invalid"] = causes.get("cache-invalid", 0) + 1
                    batch_payload = {
                        "run_key": run_key, "evidence_hash": evidence,
                        "status": "FAILED", "cause": "cache-invalid",
                        "elapsed_s": 0.0, "fee": fee,
                        "window": {k: window[k] for k in ("year", "seg_dir", "start", "end_exclusive")},
                        "strategies": names,
                    }
                    launch._atomic_create_new(batch_dir / f"{_sanitize_run_key(run_key)}.json",
                                              batch_payload)
                    _progress()
                    continue
        if batch_payload is None:
            remaining = float(state.get("budget_limit", BUDGET_SECONDS)) - float(state.get("consumed", 0))
            if remaining <= 0:
                raise ValueError("presupuesto 12h agotado (sin rebajar gates)")
            if NATIVE_TIMEOUT_S > remaining:
                raise ValueError(
                    f"timeout {NATIVE_TIMEOUT_S}s excede remaining {remaining:.0f}s")
            job_tag = _sanitize_run_key(run_key)
            user_dir = session_dir / f"user_{job_tag}"
            export_dir = session_dir / f"native_{job_tag}"
            user_dir.mkdir(parents=True, exist_ok=False)
            export_dir.mkdir(parents=True, exist_ok=False)
            log_path = session_dir / f"native_{job_tag}.log"
            argv = _native_argv_for_batch(window, fee, names, spath,
                                          user_dir, export_dir, use_list,
                                          snap_root=snap_root)
            t0 = time.monotonic()
            try:
                rc, timed_out = _run_native_batch(argv, NATIVE_TIMEOUT_S, log_path)
                elapsed = time.monotonic() - t0
                exec_error = None
            except OSError as exc:
                elapsed = min(time.monotonic() - t0,
                              NATIVE_TIMEOUT_S + NATIVE_GRACE_S)
                rc, timed_out = None, False
                exec_error = f"{type(exc).__name__}: {exc}"
            if exec_error is not None:
                cause, status = f"exec:{exec_error.split(':')[0]}", "FAILED"
            elif timed_out or rc is None:
                cause, status = "timeout", "FAILED"
                elapsed = min(float(elapsed), NATIVE_TIMEOUT_S + NATIVE_GRACE_S)
            elif rc != 0:
                cause, status = f"exit:{rc}", "FAILED"
            else:
                cause, status = None, None
            if status == "FAILED":
                assert cause is not None
                try:
                    record_attempt(state, run_key, float(elapsed), "FAILED", str(evidence))
                finally:
                    _save_state_atomic(state_path, state)
                causes[cause] = causes.get(cause, 0) + 1
                fail += 1
                done += 1
                batch_payload = {
                    "run_key": run_key, "evidence_hash": evidence,
                    "status": "FAILED", "cause": cause,
                    "elapsed_s": float(elapsed), "fee": fee,
                    "window": {k: window[k] for k in ("year", "seg_dir", "start", "end_exclusive")},
                    "strategies": names, "argv": argv,
                }
                launch._atomic_create_new(batch_dir / f"{job_tag}.json", batch_payload)
                _progress()
                if causes[cause] >= SAME_CAUSE_CAP:
                    raise ValueError(f"misma causa {cause} x{SAME_CAUSE_CAP}: stop")
                continue
            # Post-nativo con cargo unico garantizado: UNA invocacion real
            # produce EXACTAMENTE un intento (aunque parse/open fallen con
            # OSError); el budget jamas se resetea.
            charged = False

            def _charge(status, output=None):
                nonlocal charged
                if charged:
                    raise AssertionError("doble cargo de intento nativo")
                charged = True
                try:
                    record_attempt(state, run_key, float(elapsed), status,
                                   str(evidence), output=output)
                finally:
                    _save_state_atomic(state_path, state)

            def _fail_job(cause):
                _charge("FAILED")
                causes[str(cause)] = causes.get(str(cause), 0) + 1
                payload = {
                    "run_key": run_key, "evidence_hash": evidence,
                    "status": "FAILED", "cause": cause,
                    "elapsed_s": float(elapsed), "fee": fee,
                    "window": {k: window[k] for k in ("year", "seg_dir", "start", "end_exclusive")},
                    "strategies": names, "argv": argv,
                }
                if zip_name is not None:
                    payload["zip"] = zip_name
                launch._atomic_create_new(batch_dir / f"{job_tag}.json", payload)
                return payload

            zip_name = None
            try:
                zips = sorted(export_dir.glob("*.zip"))
                if len(zips) != 1:
                    raise _JobFail(f"zip:{'0' if not zips else 'multi'}")
                zip_name = zips[0].name
                parsed, perr = _parse(zips[0], names)
                if perr is not None or parsed is None:
                    raw_err = str(perr or "schema")
                    # Conteo len==total_trades con prefijo estable del parse:
                    # causa propia, no generica de parse.
                    raise _JobFail(raw_err[:60] if raw_err.startswith("count:")
                                   else f"parse:{raw_err[:60]}")
                try:
                    zip_rel = str((export_dir / zips[0].name).resolve().relative_to(
                        Path(CONTAINER_SEARCH).resolve()))
                except ValueError:
                    raise _JobFail("output:path") from None
                try:
                    zip_sha = launch.file_hash(export_dir / zips[0].name)
                except OSError as exc:
                    raise _JobFail(f"output:{type(exc).__name__}") from exc
                results_min = {n: {
                    "profit_abs": parsed[n]["profit_abs"],
                    "trades_count": parsed[n]["trades_count"],
                    "trades": parsed[n]["trades"],
                } for n in names}
                output = _output_digest(run_key, str(evidence), results_min,
                                        zip_rel, zip_sha)
                batch_payload = {
                    "run_key": run_key, "evidence_hash": evidence,
                    "status": "SUCCEEDED", "elapsed_s": float(elapsed),
                    "fee": fee,
                    "window": {k: window[k] for k in ("year", "seg_dir", "start", "end_exclusive")},
                    "strategies": names, "argv": argv,
                    "zip": zips[0].name,
                    "zip_sha256": zip_sha,
                    "results": results_min,
                    "reused": False,
                    "output": dict(output),
                }
                launch._atomic_create_new(batch_dir / f"{job_tag}.json", batch_payload)
                _charge("SUCCEEDED", output)
            except _JobFail as exc:
                batch_payload = _fail_job(exc.cause)
            except OSError as exc:
                if charged:
                    raise
                batch_payload = _fail_job(f"output:{type(exc).__name__}")
            fail += 1 if batch_payload.get("status") == "FAILED" else 0
            done += 1
            if batch_payload.get("status") == "SUCCEEDED":
                succ += 1
            else:
                assert batch_payload.get("cause") is not None
            _progress()
            if batch_payload.get("status") == "FAILED":
                cause = batch_payload.get("cause")
                if causes.get(cause, 0) >= SAME_CAUSE_CAP:
                    raise ValueError(f"misma causa {cause} x{SAME_CAUSE_CAP}: stop")
                continue
        if batch_payload.get("status") != "SUCCEEDED":
            continue
        for nname, res in (batch_payload.get("results") or {}).items():
            results[(year, seg_dir, fee, nname)] = {
                "profit_abs": res["profit_abs"],
                "trades_count": res["trades_count"],
                "trades": list(res.get("trades") or []),
                "zip": batch_payload.get("zip"),
                "evidence_hash": batch_payload.get("evidence_hash"),
                "elapsed_s": batch_payload.get("elapsed_s"),
                "reused": bool(reused),
            }
    return results, {"done": done, "succeeded": succ, "failed": fail,
                     "total": total, "causes": dict(causes)}


def _episode_for_variant(variant, trades, profit_abs, window, fee, seg_data_root):
    """Episodio ledger+BH honesto (short/ledger valid False, reconcile raise)."""
    from research.evaluation import (
        buyhold_comparable_for_window as _bh,
        check_trades_in_window,
        forced_positive_pnl,
        nonforced_count,
        turnover_for_trades,
    )

    check_trades_in_window(list(trades), window["start"], window["end_exclusive"])
    try:
        prices5, _f1, first_open, last_open = _load_prices_for_window(
            str(Path(seg_data_root) / str(window["seg_dir"])), window)
    except ValueError as exc:
        return {
            "days": float(window["days"]), "initial": 10000.0,
            "final_equity": 10000.0, "max_drawdown_pct": 0.0,
            "trades_nonforced": 0, "turnover": 0.0,
            "bh_final": 10000.0, "forced_positive": 0.0,
            "valid": False, "reason": f"short:{exc}",
        }
    try:
        _curve, summary = _ledger_for_trades(
            prices5, list(trades), window["start"], window["end_exclusive"])
    except ValueError as exc:
        return {
            "days": float(window["days"]),
            "initial": 10000.0, "final_equity": 10000.0,
            "max_drawdown_pct": 0.0, "trades_nonforced": 0,
            "turnover": 0.0, "bh_final": 10000.0,
            "forced_positive": 0.0, "valid": False,
            "reason": f"ledger:{exc}",
        }
    from market.equity import reconcile_final_ledger

    rec = reconcile_final_ledger(summary, float(profit_abs), 10000.0, 0.01)
    if not rec.get("ok"):
        raise ValueError(
            f"reconcile y{window['year']} {window['seg_dir']} fee {fee}: "
            f"diff {rec.get('diff')}")
    bh = _bh(first_open, last_open, variant, float(fee))
    forced = forced_positive_pnl(list(trades))
    return {
        "days": float(summary["days_observed"]),
        "initial": 10000.0,
        "final_equity": float(summary["final_equity"]),
        "max_drawdown_pct": float(summary["max_drawdown_pct"]),
        "trades_nonforced": int(nonforced_count(list(trades))),
        "turnover": float(turnover_for_trades(list(trades))),
        "bh_final": 10000.0 + float(bh["pnl"]),
        "forced_positive": float(forced if forced > 0.0 else 0.0),
        "valid": True,
    }


def _aggregate_role_records(role, episodes, windows, fees, by_id):
    from research.evaluation import aggregate_year_record

    per_yf: dict = {}
    for window in windows:
        for fee in fees:
            per_yf.setdefault((int(window["year"]), float(fee)), 0)
            per_yf[(int(window["year"]), float(fee))] += 1
    records = []
    for vid in sorted(by_id):
        for fee in fees:
            for year in sorted({int(w["year"]) for w in windows}):
                key = (vid, year, float(fee))
                eps = episodes.get(key, [])
                exp = per_yf.get((year, float(fee)), 0)
                records.append(aggregate_year_record(
                    vid, role, year, float(fee), eps, exp))
    return records


def _run_bias_gate(*, variant, class_name, seg_meta, snap_train_root,
                   session_dir, state, state_path, manifest_in, code_hashes,
                   causes, progress_cb):
    """Gate de sesgo por finalista: ref + CSV + recursive + own (budget real).

    Retorna (verdict_bool, detail): True solo con PASS tecnico; False con
    FAIL probado o INCONCLUSIVE (motivo en detail, sin backfill posterior).
    FAIL conocido precede a INCONCLUSIVE. Requiere segmento >=1201 barras.
    """
    from research.evaluation import (
        bias_reference_checks,
        lookahead_coverage_ok,
        own_strategy_gate,
        parse_lookahead_csv_generic,
        timerange_fmt,
    )
    from research.state import can_reuse_run, record_attempt

    vid = str(variant.get("id"))
    detail: dict = {"variant_id": vid, "class_name": class_name}
    seg_len = int(seg_meta.get("length", 0) or 0)
    if seg_len < 1201:
        detail.update({"verdict": "INCONCLUSIVE",
                       "reason": f"segmento {seg_len} <1201 barras"})
        return False, detail
    try:
        eval_start = str(seg_meta.get("eval_start") or seg_meta["start"])
        seg_end = str(seg_meta["end_exclusive"])
    except KeyError as exc:
        detail.update({"verdict": "INCONCLUSIVE", "reason": f"meta sin fechas: {exc}"})
        return False, detail
    window = {"start": eval_start, "end_exclusive": seg_end,
              "seg_dir": str(seg_meta.get("seg_dir")), "year": "bias"}
    timerange = timerange_fmt(eval_start, seg_end)
    seg_dir = str(Path(snap_train_root) / str(seg_meta.get("seg_dir")))
    bias_dir = session_dir / "bias" / vid
    bias_dir.mkdir(parents=True, exist_ok=True)
    bias_file = session_dir / "bias" / f"{vid}.json"

    def _ev(command, extra):
        base = {
            "image_id": str(manifest_in.get("image_id")),
            "generated_source_sha256": str(manifest_in.get("generated_source_sha256")),
            "config_hash": str(manifest_in.get("config_hash")),
            "search_hash": str(code_hashes.get("search_hash")),
            "evaluation_hash": str(code_hashes.get("evaluation_hash")),
            "candidates_hash": str(code_hashes.get("candidates_hash")),
            "command": str(command),
            "class": str(class_name),
            "params": dict(variant.get("params") or {}),
            "stop": float(variant.get("stop")),
            "data_5m": str(seg_meta.get("file_5m_sha256")),
            "data_1h": str(seg_meta.get("file_1h_sha256")),
            "timerange": str(timerange),
            "fee": float(BIAS_FEE),
        }
        base.update(dict(extra or {}))
        return _sha256_bytes(_canonical(base))

    def _charge(run_key, evidence, elapsed, status):
        try:
            record_attempt(state, run_key, float(elapsed), status, str(evidence))
        finally:
            _save_state_atomic(state_path, state)

    def _budget_ok():
        remaining = float(state.get("budget_limit", BUDGET_SECONDS)) - float(state.get("consumed", 0))
        if remaining <= 0:
            raise ValueError("presupuesto 12h agotado (sin rebajar gates)")
        if NATIVE_TIMEOUT_S > remaining:
            raise ValueError(f"timeout {NATIVE_TIMEOUT_S}s excede remaining {remaining:.0f}s")
        return remaining

    # Reuso exacto: las tres evidencias deben coincidir con el bias guardado.
    ev_ref = _ev("backtest-ref", {})
    ev_look = _ev("lookahead", {})
    ev_rec = _ev("recursive", {})
    def _prior_bias_file():
        want = {"ref": ev_ref, "lookahead": ev_look, "recursive": ev_rec}
        for prev in sorted(Path(CONTAINER_SEARCH).joinpath("sessions").glob("finalists-*/bias/*.json")):
            if Path(prev).name != f"{vid}.json":
                continue
            try:
                payload = json.loads(Path(prev).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("status") != "SUCCEEDED":
                continue
            if payload.get("evidences") != want:
                continue
            if "verdict" not in payload or "detail" not in payload:
                continue
            return payload
        return None

    reuse_ok = all(
        can_reuse_run(state, f"finalists|bias|{vid}|{k}", e)
        for k, e in (("ref", ev_ref), ("lookahead", ev_look), ("recursive", ev_rec))
    )
    if reuse_ok:
        prior = _prior_bias_file()
        if prior is not None:
            detail.update(dict(prior.get("detail") or {}))
            return bool(prior.get("verdict")), detail
    if reuse_ok and bias_file.is_file():
        try:
            saved = json.loads(bias_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = None
        if (isinstance(saved, dict) and saved.get("status") == "SUCCEEDED"
                and saved.get("evidences") == {"ref": ev_ref, "lookahead": ev_look, "recursive": ev_rec}):
            detail.update(saved.get("detail") or {})
            return bool(saved.get("verdict")), detail
    fail_known: list = []
    inconcl: list = []
    ref_trades: list = []
    analysable = 0
    # 1) Referencia nativa con la clase real del registry.
    _budget_ok()
    ref_user = bias_dir / "user_data"
    ref_native = bias_dir / "native"
    ref_user.mkdir(parents=True, exist_ok=True)
    ref_native.mkdir(parents=True, exist_ok=True)
    ref_argv = [
        "freqtrade", "backtesting",
        "--config", CONTAINER_CONFIG,
        "--datadir", seg_dir,
        "--userdir", str(ref_user),
        "--strategy", str(class_name),
        "--strategy-path", CONTAINER_GENERATED,
        "--timeframe", TIMEFRAME_1H,
        "--timeframe-detail", TIMEFRAME_5M,
        "--timerange", timerange,
        "--fee", repr(float(BIAS_FEE)),
        "--export", "trades",
        "--export-directory", str(ref_native),
        "--cache", "none",
    ]
    ref_log = bias_dir / "ref.log"
    t0 = time.monotonic()
    try:
        ref_rc, ref_timeout = _run_native_batch(ref_argv, NATIVE_TIMEOUT_S, ref_log)
        ref_elapsed = time.monotonic() - t0
        ref_exec = None
    except OSError as exc:
        ref_elapsed = min(time.monotonic() - t0, NATIVE_TIMEOUT_S + NATIVE_GRACE_S)
        ref_rc, ref_timeout, ref_exec = None, False, f"{type(exc).__name__}: {exc}"
    if ref_exec is not None or ref_timeout or ref_rc != 0:
        _charge(f"finalists|bias|{vid}|ref", ev_ref, ref_elapsed, "FAILED")
        causes["bias-ref"] = causes.get("bias-ref", 0) + 1
        progress_cb()
        if causes["bias-ref"] >= SAME_CAUSE_CAP:
            raise ValueError("misma causa bias-ref x3: stop")
        detail.update({"verdict": "INCONCLUSIVE",
                       "reason": ref_exec or ("timeout" if ref_timeout else f"exit:{ref_rc}")})
        _write_bias_file(bias_file, vid, False, detail,
                         {"ref": ev_ref, "lookahead": ev_look, "recursive": ev_rec})
        return False, detail
    _charge(f"finalists|bias|{vid}|ref", ev_ref, ref_elapsed, "SUCCEEDED")
    progress_cb()
    from research.evaluation import parse_native_batch as _parse

    zips = sorted(ref_native.glob("*.zip"))
    if len(zips) != 1:
        detail.update({"verdict": "INCONCLUSIVE", "reason": "ref sin ZIP unico"})
        _write_bias_file(bias_file, vid, False, detail,
                         {"ref": ev_ref, "lookahead": ev_look, "recursive": ev_rec})
        return False, detail
    parsed, perr = _parse(zips[0], [class_name])
    if perr is not None or parsed is None:
        detail.update({"verdict": "INCONCLUSIVE", "reason": f"ref parse: {perr}"})
        _write_bias_file(bias_file, vid, False, detail,
                         {"ref": ev_ref, "lookahead": ev_look, "recursive": ev_rec})
        return False, detail
    entry = parsed[class_name]
    if int(entry.get("trades_count", -1)) != len(entry.get("trades") or []):
        detail.update({"verdict": "INCONCLUSIVE", "reason": "ref conteo distinto"})
        _write_bias_file(bias_file, vid, False, detail,
                         {"ref": ev_ref, "lookahead": ev_look, "recursive": ev_rec})
        return False, detail
    ref_trades = list(entry.get("trades") or [])
    analysable = sum(1 for t in ref_trades
                     if str((t or {}).get("exit_reason") or "") != "force_exit")
    detail["ref_trades"] = len(ref_trades)
    detail["analysable"] = analysable
    ok_tags, tag_reason = bias_reference_checks(variant, ref_trades)
    if not ok_tags:
        # Cobertura (<5 o sin ambos cruces) => INCONCLUSIVE; tags fuera de
        # familia => FAIL probado (la clase emite senales ajenas).
        if tag_reason.startswith("solo ") or tag_reason.startswith("sin cobertura"):
            inconcl.append(f"tags-coverage: {tag_reason}")
        else:
            fail_known.append(f"tags: {tag_reason}")
    # 2) Lookahead nativo con target = analizables (ALL).
    look_csv = bias_dir / "lookahead.csv"
    look_user = bias_dir / "lookahead_user"
    look_user.mkdir(parents=True, exist_ok=True)
    if analysable < BIAS_MIN_TRADES:
        inconcl.append(f"solo {analysable} analizables (<{BIAS_MIN_TRADES})")
        look_parsed = None
    else:
        _budget_ok()
        look_argv = _native_argv_lookahead(seg_dir, class_name, look_user,
                                           timerange, analysable, look_csv)
        look_log = bias_dir / "lookahead.log"
        t0 = time.monotonic()
        try:
            look_rc, look_timeout = _run_native_batch(look_argv, NATIVE_TIMEOUT_S, look_log)
            look_elapsed = time.monotonic() - t0
            look_exec = None
        except OSError as exc:
            look_elapsed = min(time.monotonic() - t0, NATIVE_TIMEOUT_S + NATIVE_GRACE_S)
            look_rc, look_timeout, look_exec = None, False, f"{type(exc).__name__}: {exc}"
        if look_exec is not None:
            _charge(f"finalists|bias|{vid}|lookahead", ev_look, look_elapsed, "FAILED")
            causes["bias-look-exec"] = causes.get("bias-look-exec", 0) + 1
            progress_cb()
            inconcl.append(f"lookahead exec: {look_exec}")
            look_parsed = None
        elif look_timeout:
            _charge(f"finalists|bias|{vid}|lookahead", ev_look,
                    min(float(look_elapsed), NATIVE_TIMEOUT_S + NATIVE_GRACE_S), "FAILED")
            causes["bias-look-timeout"] = causes.get("bias-look-timeout", 0) + 1
            progress_cb()
            inconcl.append("lookahead timeout")
            look_parsed = None
        elif look_rc != 0:
            _charge(f"finalists|bias|{vid}|lookahead", ev_look, look_elapsed, "FAILED")
            causes["bias-look-exit"] = causes.get("bias-look-exit", 0) + 1
            progress_cb()
            try:
                from operations.research import _read_tail as _tail

                tail = _tail(look_log, 8192).lower()
            except (ImportError, OSError, ValueError):
                tail = ""
            if ("minimum_trade_amount" in tail or "too few" in tail
                    or "insufficient" in tail or "no data" in tail):
                inconcl.append("lookahead sin cobertura")
            else:
                fail_known.append(f"lookahead exit:{look_rc}")
            look_parsed = None
        else:
            _charge(f"finalists|bias|{vid}|lookahead", ev_look, look_elapsed, "SUCCEEDED")
            progress_cb()
            look_parsed, look_err = parse_lookahead_csv_generic(look_csv, class_name)
            if look_err is not None or look_parsed is None:
                inconcl.append(f"lookahead CSV: {look_err or 'vacio'}")
                look_parsed = None
            elif int(look_parsed.get("total_signals", -1)) < BIAS_MIN_TRADES:
                inconcl.append(f"lookahead solo {look_parsed.get('total_signals')} trades")
                look_parsed = None
            elif not lookahead_coverage_ok(look_parsed.get("total_signals"), ref_trades):
                inconcl.append("lookahead incompleto frente a referencia analizable")
                look_parsed = None
            elif (look_parsed.get("has_bias") or look_parsed.get("biased_entry_signals", 0) > 0
                    or look_parsed.get("biased_exit_signals", 0) > 0
                    or look_parsed.get("biased_indicators")):
                fail_known.append("lookahead marca sesgo")
                look_parsed = None
    # 3) Recursive nativo + own gate con la clase real (sin formula manual).
    if look_parsed is None and fail_known:
        pass  # FAIL ya probado; aun asi se corre recursive para evidencia.
    _budget_ok()
    rec_user = bias_dir / "recursive_user"
    rec_user.mkdir(parents=True, exist_ok=True)
    rec_argv = _native_argv_recursive(seg_dir, class_name, rec_user, timerange)
    rec_log = bias_dir / "recursive.log"
    t0 = time.monotonic()
    try:
        rec_rc, rec_timeout = _run_native_batch(rec_argv, NATIVE_TIMEOUT_S, rec_log)
        rec_elapsed = time.monotonic() - t0
        rec_exec = None
    except OSError as exc:
        rec_elapsed = min(time.monotonic() - t0, NATIVE_TIMEOUT_S + NATIVE_GRACE_S)
        rec_rc, rec_timeout, rec_exec = None, False, f"{type(exc).__name__}: {exc}"
    if rec_exec is not None or rec_timeout:
        _charge(f"finalists|bias|{vid}|recursive", ev_rec, rec_elapsed, "FAILED")
        causes["bias-rec-exec"] = causes.get("bias-rec-exec", 0) + 1
        progress_cb()
        inconcl.append(f"recursive exec: {rec_exec or 'timeout'}")
    elif rec_rc != 0:
        _charge(f"finalists|bias|{vid}|recursive", ev_rec, rec_elapsed, "FAILED")
        causes["bias-rec-exit"] = causes.get("bias-rec-exit", 0) + 1
        progress_cb()
        try:
            from operations.research import _read_tail as _tail

            tail = _tail(rec_log, 8192).lower()
        except (ImportError, OSError, ValueError):
            tail = ""
        if ("insufficient" in tail or "too few" in tail or "no data" in tail):
            inconcl.append("recursive sin cobertura")
        else:
            fail_known.append(f"recursive exit:{rec_rc}")
    else:
        _charge(f"finalists|bias|{vid}|recursive", ev_rec, rec_elapsed, "SUCCEEDED")
        progress_cb()
    try:
        frame_1h = _load_full_1h(str(Path(snap_train_root) / str(seg_meta.get("seg_dir"))))
    except (ValueError, OSError) as exc:
        inconcl.append(f"1h ilegible: {exc}")
        frame_1h = None
    if frame_1h is not None:
        own = own_strategy_gate(dict(variant), frame_1h)
        detail["own_gate"] = {k: own.get(k) for k in ("verdict", "reason", "checked_events")}
        if own.get("verdict") == "FAIL":
            fail_known.append(f"own: {own.get('reason', '')}")
        elif own.get("verdict") != "PASS":
            inconcl.append(f"own: {own.get('reason', '')}")
    # FAIL conocido precede a INCONCLUSIVE.
    if fail_known:
        detail.update({"verdict": "FAIL", "reasons": list(fail_known),
                       "inconclusive_also": list(inconcl)})
        _write_bias_file(bias_file, vid, False, detail,
                         {"ref": ev_ref, "lookahead": ev_look, "recursive": ev_rec})
        return False, detail
    if inconcl:
        detail.update({"verdict": "INCONCLUSIVE", "reasons": list(inconcl)})
        _write_bias_file(bias_file, vid, False, detail,
                         {"ref": ev_ref, "lookahead": ev_look, "recursive": ev_rec})
        return False, detail
    detail.update({"verdict": "PASS",
                   "reason": "lookahead/recursive sin sesgo + clase exacta"})
    _write_bias_file(bias_file, vid, True, detail,
                     {"ref": ev_ref, "lookahead": ev_look, "recursive": ev_rec})
    return True, detail


def _write_bias_file(bias_file, vid, verdict, detail, evidences) -> None:
    payload = {
        "variant_id": str(vid), "status": "SUCCEEDED",
        "verdict": bool(verdict), "detail": dict(detail),
        "evidences": dict(evidences),
    }
    if bias_file.is_file():
        try:
            prev = json.loads(Path(bias_file).read_text(encoding="utf-8"))
            if prev == payload:
                return
        except (OSError, ValueError):
            pass
        launch._atomic_replace(Path(bias_file), payload)
    else:
        launch._atomic_create_new(Path(bias_file), payload)


def cmd_status() -> int:
    try:
        manifest_in = _load_input()
        _verify_current_against_input(manifest_in)
        state, _ = _load_state()
    except FileNotFoundError as exc:
        print(json.dumps({"kind": STATUS_KIND, "status": "UNPREPARED",
                          "campaign_id": CAMPAIGN_ID,
                          "error": f"{type(exc).__name__}: {exc}"},
                         indent=2, sort_keys=True))
        return 2
    except (ValueError, OSError) as exc:
        print(f"status: input/codigo/estado no valido: {exc}", file=sys.stderr)
        return 2
    sessions = Path(CONTAINER_SEARCH) / "sessions"
    reports = sorted(sessions.glob("*/report.json")) if sessions.is_dir() else []
    payload = {
        "kind": STATUS_KIND,
        "campaign_id": CAMPAIGN_ID,
        "definition_hash": manifest_in.get("definition_hash"),
        "input_id": manifest_in.get("input_id"),
        "stage": state.get("stage"),
        "consumed": state.get("consumed"),
        "budget_limit": state.get("budget_limit"),
        "train_ids": state.get("train_ids"),
        "validation_ids": state.get("validation_ids"),
        "test_candidate": state.get("test_candidate"),
        "test_grant_id": state.get("test_grant_id"),
        "test_consumed": state.get("test_consumed"),
        "reports": [str(p) for p in reports],
        "snapshot_train_ref": manifest_in.get("definition", {}).get("snapshot_train_ref"),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_report() -> int:
    try:
        manifest_in = _load_input()
        _verify_current_against_input(manifest_in)
        state, _ = _load_state()
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"report: input/codigo/estado no valido: {exc}", file=sys.stderr)
        return 2
    stage = str(state.get("stage") or "REGISTERED")
    train_ids = state.get("train_ids")
    validation_ids = state.get("validation_ids")
    test_candidate = state.get("test_candidate")
    if stage == "REGISTERED" and not train_ids:
        verdict = "NOT_STARTED"
        reasons = ["screen pendiente de ejecucion real"]
    elif train_ids is not None and len(train_ids) == 0:
        verdict = "NO_CANDIDATE"
        reasons = ["screen sin finalistas TRAIN elegibles; holdout no abierto"]
    elif validation_ids is not None and len(validation_ids) == 0:
        verdict = "NO_CANDIDATE"
        reasons = ["sin candidatos VAL; TEST no abierto"]
    elif test_candidate is None and stage in ("VALIDATION_FROZEN",):
        verdict = "NO_CANDIDATE"
        reasons = ["sin candidata TEST elegible; bundle paper no generado"]
    else:
        verdict = "PENDING"
        reasons = [f"stage {stage}: fases screen/finalists/validation/test implementadas; "
                   "coordinator continua fase autorizada"]
    payload = {
        "kind": REPORT_KIND,
        "campaign_id": CAMPAIGN_ID,
        "definition_hash": manifest_in.get("definition_hash"),
        "stage": stage,
        "verdict": verdict,
        "reasons": reasons,
        "train_ids": train_ids,
        "validation_ids": validation_ids,
        "test_candidate": test_candidate,
        "consumed": state.get("consumed"),
        "budget_limit": state.get("budget_limit"),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if verdict in ("NOT_STARTED", "PENDING") else 1


def cmd_screen() -> int:
    from research.evaluation import plan_windows

    try:
        manifest_in = _load_input()
        code_hashes = _verify_current_against_input(manifest_in)
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        print(f"screen: fase no autorizada / input-codigo no valido: {exc}",
              file=sys.stderr)
        return 2
    from research.campaign import generate_variants
    from research.selection import choose_train_finalists, walk_forward_select
    from research.state import freeze_train_selection

    variants = generate_variants()
    by_id = {str(v["id"]): v for v in variants}
    by_class = {str(v["class_name"]): v for v in variants}
    if len(variants) != 72:
        print("screen: registry sin 72", file=sys.stderr)
        return 1
    snapshot_manifest = _load_snapshot_manifest_file(Path(CONTAINER_SNAP_TRAIN_MANIFEST))
    try:
        with _container_locked():
            # Estado mutable SIEMPRE bajo el mismo lock adquirido: re-lectura
            # fresca tras entrar; un consumo concurrente previo frena aqui
            # antes de cualquier Popen y sin overwrite al salir.
            try:
                state, state_path = _load_state()
                _phase_guard(manifest_in, state, "screen")
            except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
                print(f"screen: fase no autorizada / estado no valido: {exc}",
                      file=sys.stderr)
                return 2
            refs = state.get("phase_reports") or {}
            if isinstance(refs.get("screen"), dict) and refs["screen"].get("status") == "SUCCEEDED":
                try:
                    _resumed, _shown, _rc = _resume_completed("screen", state)
                except (FileNotFoundError, ValueError, OSError) as exc:
                    print(f"screen: artefacto registrado invalido: {exc}", file=sys.stderr)
                    return 1
                print(_shown)
                return _rc
            sessions = Path(CONTAINER_SEARCH) / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            session_dir = sessions / f"screen-{_slug()}"
            session_dir.mkdir(parents=True, exist_ok=False)
            batch_dir = session_dir / "batches"
            batch_dir.mkdir(parents=True, exist_ok=False)
            base_report = {
                "kind": SCREEN_SESSION_KIND,
                "status": "RUNNING",
                "session": session_dir.name,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "definition_hash": manifest_in.get("definition_hash"),
            }
            launch._atomic_create_new(session_dir / "report.json", base_report)
            windows = plan_windows("train", snapshot_manifest)
            if not windows:
                raise ValueError("screen sin ventanas TRAIN (snapshot vacio)")
            # Cobertura excluida honesta: anos del rol sin interseccion.
            from market.history import partition_bounds

            rstart, rend = partition_bounds("train")
            years_role = sorted({rstart.year, rend.year - 1} | set(range(rstart.year, rend.year)))
            years_cov = sorted({int(w["year"]) for w in windows})
            excluded = [y for y in years_role if y not in years_cov]
            class_names = [str(v["class_name"]) for v in variants]
            groups = []
            for idx, chunk in enumerate(_batch_names(class_names, BATCH_MAX)):
                groups.append({
                    "names": chunk, "use_list": True,
                    "spath": CONTAINER_GENERATED,
                    "params": {n: by_class[n]["params"] for n in chunk},
                    "suffix": f"batch{idx:02d}",
                })
            groups.append({
                "names": [CONTROL_STRATEGY], "use_list": False,
                "spath": CONTAINER_STRATEGY_CODE_PATH,
                "params": {CONTROL_STRATEGY: {"control": True}},
                "suffix": "control",
            })
            seg_meta_by_dir = {str(m.get("seg_dir")): m
                               for m in snapshot_manifest.get("segments_meta") or []}
            by_class_params = {str(v["class_name"]): v["params"] for v in variants}
            # Matriz nativa compartida con las demas fases (mismos run_keys,
            # evidencias, batches<=6, budget/cache/progreso, 1 intento/job).
            results, stats = _execute_native_matrix(
                phase="screen", role="train", windows=windows,
                fees=list(FEES_SCREEN), groups=groups,
                snap_root=CONTAINER_SNAP_TRAIN,
                session_dir=session_dir, batch_dir=batch_dir,
                state=state, state_path=state_path,
                manifest_in=manifest_in, code_hashes=code_hashes,
                seg_meta_by_dir=seg_meta_by_dir,
                by_class_params=by_class_params)
            succ, fail, total = stats["succeeded"], stats["failed"], stats["total"]
            # --- Metricas desde batches (frescos o cacheados exactos) ---
            episodes = _candidate_episodes(results, windows, list(FEES_SCREEN),
                                           sorted(by_id), by_id,
                                           CONTAINER_SNAP_TRAIN)
            control_cov = _control_coverage(results, windows, list(FEES_SCREEN),
                                            CONTAINER_SNAP_TRAIN)
            if fail > 0:
                launch._atomic_replace(session_dir / "report.json", {
                    **base_report, "status": "FAILED",
                    "finished_at": _utcnow_iso(),
                    "error": f"{fail}/{total} batches FAILED (ver batches/*.json)",
                    "succeeded": succ, "failed": fail, "total": total,
                    "excluded_coverage_years": excluded,
                })
                _record_phase_report(state, state_path, "screen", session_dir,
                                     "FAILED", status="FAILED")
                print(str(session_dir / "report.json"))
                return 1
            # Agregacion por ano honesta (cero real g0, faltante nonvalid).
            records = _aggregate_role_records(
                "train", episodes, windows, list(FEES_SCREEN), by_id)
            wf = {}
            for year in (2019, 2020, 2021, 2022):
                try:
                    wf[str(year)] = walk_forward_select(records, int(year))
                except ValueError as exc:
                    raise ValueError(f"WF descriptivo y{year}: {exc}") from exc
            top9 = choose_train_finalists(records)
            if not top9:
                launch._atomic_replace(session_dir / "report.json", {
                    **base_report, "status": "SUCCEEDED",
                    "finished_at": _utcnow_iso(),
                    "verdict": "NO_CANDIDATE",
                    "reasons": ["screen sin top9 elegible; holdout no abierto"],
                    "records": len(records), "wf": wf, "top9": [],
                    "excluded_coverage_years": excluded,
                    "consumed": state.get("consumed"),
                })
                _record_phase_report(state, state_path, "screen", session_dir,
                                     "NO_CANDIDATE")
                print(str(session_dir / "report.json"))
                # Terminal NO_CANDIDATE: sin freeze vacio (la API exige 1..9);
                # el estado queda REGISTERED sin holdouts abiertos.
                return 1
            if state.get("train_ids") is None:
                freeze_train_selection(state, list(top9))
                _save_state_atomic(state_path, state)
            elif sorted(str(v) for v in (state.get("train_ids") or [])) != sorted(top9):
                raise ValueError("train_ids congelados difieren del top9 recalculado")
            launch._atomic_replace(session_dir / "report.json", {
                **base_report, "status": "SUCCEEDED",
                "finished_at": _utcnow_iso(),
                "verdict": "TOP9",
                "records": records,
                "wf_descriptive": wf,
                "top9": sorted(top9),
                "control_windows": control_cov["ok"] + control_cov["short"],
                "control_coverage": dict(control_cov),
                "excluded_coverage_years": excluded,
                "consumed": state.get("consumed"),
                "lineage": {
                    "image_id": manifest_in.get("image_id"),
                    "generated_source_sha256": manifest_in.get("generated_source_sha256"),
                    "config_hash": manifest_in.get("config_hash"),
                    "definition_hash": manifest_in.get("definition_hash"),
                },
            })
            _record_phase_report(state, state_path, "screen", session_dir, "TOP9")
            print(str(session_dir / "report.json"))
            return 0
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        print(f"screen: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            sessions = Path(CONTAINER_SEARCH) / "sessions"
            cands = sorted(sessions.glob("screen-*/report.json"))
            if cands:
                last = cands[-1]
                try:
                    payload = json.loads(last.read_text(encoding="utf-8"))
                    if payload.get("status") == "RUNNING":
                        launch._atomic_replace(last, {
                            **payload, "status": "FAILED",
                            "finished_at": _utcnow_iso(),
                            "error": f"{type(exc).__name__}: {exc}"})
                        print(str(last))
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
        return 1


def _control_coverage(results, windows, fees, seg_data_root):
    """Ledger+reconcile del control por ventana (contexto equitativo).

    El control ausente o con ledger erroneo FALLA la fase con error
    etiquetado (ventana/fee): nunca short silencioso. Las exclusiones
    genuinas de cobertura se declaran antes de los jobs (plan_windows),
    no como catchall de errores matematicos.
    """
    from research.evaluation import check_trades_in_window

    ok = 0
    for window in windows:
        for fee in fees:
            label = (f"control y{window['year']} {window['seg_dir']} "
                     f"fee {float(fee):.4f}")
            key = (int(window["year"]), str(window["seg_dir"]), float(fee),
                   CONTROL_STRATEGY)
            res = results.get(key)
            if res is None:
                raise ValueError(f"{label}: sin resultado nativo (FAIL de fase)")
            trades = list(res.get("trades") or [])
            try:
                check_trades_in_window(trades, window["start"], window["end_exclusive"])
            except ValueError as exc:
                raise ValueError(f"{label} rango: {exc}") from exc
            try:
                prices5, _f1, _o, _l = _load_prices_for_window(
                    str(Path(seg_data_root) / str(window["seg_dir"])), window)
            except ValueError as exc:
                raise ValueError(f"{label} precios: {exc}") from exc
            try:
                _c, _s = _ledger_for_trades(
                    prices5, trades, window["start"], window["end_exclusive"])
            except ValueError as exc:
                raise ValueError(f"{label} ledger: {exc}") from exc
            from market.equity import reconcile_final_ledger as _rec

            _r = _rec(_s, float(res["profit_abs"]), 10000.0, 0.01)
            if not _r.get("ok"):
                raise ValueError(f"reconcile {label}: diff {_r.get('diff')}")
            ok += 1
    return {"ok": ok, "short": 0}


def _candidate_episodes(results, windows, fees, targets, by_id, seg_data_root):
    """Episodios por (vid, year, fee) con ledger+BH honestos."""
    episodes: dict = {}
    for window in windows:
        for fee in fees:
            for vid in targets:
                cname = str(by_id[vid]["class_name"])
                key = (int(window["year"]), str(window["seg_dir"]), float(fee), cname)
                res = results.get(key)
                if res is None:
                    continue
                ep = _episode_for_variant(
                    by_id[vid], list(res.get("trades") or []),
                    float(res["profit_abs"]), window, float(fee), seg_data_root)
                episodes.setdefault((vid, int(window["year"]), float(fee)), []).append(ep)
    return episodes


def _fail_session(session_dir, base_report, succ, fail, total, error):
    launch._atomic_replace(session_dir / "report.json", {
        **base_report, "status": "FAILED",
        "finished_at": _utcnow_iso(),
        "error": str(error),
        "succeeded": succ, "failed": fail, "total": total,
    })
    print(str(session_dir / "report.json"))
    return 1


def cmd_finalists() -> int:
    from research.evaluation import longest_train_segment, plan_windows

    try:
        manifest_in = _load_input()
        code_hashes = _verify_current_against_input(manifest_in)
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        print(f"finalists: fase no autorizada / input-codigo no valido: {exc}",
              file=sys.stderr)
        return 2
    from research.campaign import generate_variants
    from research.selection import choose_validation_candidates
    from research.state import freeze_validation_selection

    variants = generate_variants()
    by_id = {str(v["id"]): v for v in variants}
    by_class = {str(v["class_name"]): v for v in variants}
    if len(variants) != 72:
        print("finalists: registry sin 72", file=sys.stderr)
        return 1
    snapshot_manifest = _load_snapshot_manifest_file(Path(CONTAINER_SNAP_TRAIN_MANIFEST))
    try:
        with _container_locked():
            # Estado mutable bajo el mismo lock: re-lectura fresca tras entrar.
            try:
                state, state_path = _load_state()
                _phase_guard(manifest_in, state, "finalists")
            except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
                print(f"finalists: fase no autorizada / estado no valido: {exc}",
                      file=sys.stderr)
                return 2
            train_ids = list(state.get("train_ids") or [])
            if not train_ids:
                print("finalists: screen pendiente (train_ids vacio)", file=sys.stderr)
                return 2
            if len(train_ids) > 9:
                print(f"finalists: mas de 9 train_ids: {len(train_ids)}", file=sys.stderr)
                return 1
            for vid in train_ids:
                if str(vid) not in by_id:
                    print(f"finalists: train_id desconocida: {vid!r}", file=sys.stderr)
                    return 1
            refs = state.get("phase_reports") or {}
            if isinstance(refs.get("finalists"), dict) and refs["finalists"].get("status") == "SUCCEEDED":
                try:
                    _resumed, _shown, _rc = _resume_completed("finalists", state)
                except (FileNotFoundError, ValueError, OSError) as exc:
                    print(f"finalists: artefacto registrado invalido: {exc}", file=sys.stderr)
                    return 1
                print(_shown)
                return _rc
            try:
                screen_report, screen_sha = resolve_phase_report(state, "screen")
            except (FileNotFoundError, ValueError, OSError) as exc:
                print(f"finalists: report screen no verificado: {exc}", file=sys.stderr)
                return 2
            screen_records = list(screen_report.get("records") or [])
            if not screen_records:
                print("finalists: screen sin records", file=sys.stderr)
                return 1
            sessions = Path(CONTAINER_SEARCH) / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            session_dir = sessions / f"finalists-{_slug()}"
            session_dir.mkdir(parents=True, exist_ok=False)
            batch_dir = session_dir / "batches"
            batch_dir.mkdir(parents=True, exist_ok=False)
            base_report = {
                "kind": FINALISTS_SESSION_KIND,
                "status": "RUNNING",
                "session": session_dir.name,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "definition_hash": manifest_in.get("definition_hash"),
                "screen_report_sha256": screen_sha,
            }
            launch._atomic_create_new(session_dir / "report.json", base_report)
            windows = plan_windows("train", snapshot_manifest)
            if not windows:
                raise ValueError("finalists sin ventanas TRAIN")
            from market.history import partition_bounds

            rstart, rend = partition_bounds("train")
            years_cov = sorted({int(w["year"]) for w in windows})
            excluded = sorted(set(range(rstart.year, rend.year)) - set(years_cov))
            targets = sorted(str(v) for v in train_ids)
            class_names = [str(by_id[v]["class_name"]) for v in targets]
            groups = []
            for idx, chunk in enumerate(_batch_names(class_names, BATCH_MAX)):
                groups.append({
                    "names": chunk, "use_list": True,
                    "spath": CONTAINER_GENERATED,
                    "params": {n: by_class[n]["params"] for n in chunk},
                    "suffix": f"stress{idx:02d}",
                })
            groups.append({
                "names": [CONTROL_STRATEGY], "use_list": False,
                "spath": CONTAINER_STRATEGY_CODE_PATH,
                "params": {CONTROL_STRATEGY: {"control": True}},
                "suffix": "control",
            })
            seg_meta_by_dir = {str(m.get("seg_dir")): m
                               for m in snapshot_manifest.get("segments_meta") or []}
            by_class_params = {str(v["class_name"]): v["params"] for v in variants}
            results, stats = _execute_native_matrix(
                phase="finalists", role="train", windows=windows,
                fees=list(FEES_STRESS), groups=groups,
                snap_root=CONTAINER_SNAP_TRAIN,
                session_dir=session_dir, batch_dir=batch_dir,
                state=state, state_path=state_path,
                manifest_in=manifest_in, code_hashes=code_hashes,
                seg_meta_by_dir=seg_meta_by_dir,
                by_class_params=by_class_params)
            if stats["failed"] > 0:
                return _fail_session(session_dir, base_report, stats["succeeded"],
                                     stats["failed"], stats["total"],
                                     f"{stats['failed']}/{stats['total']} batches FAILED")
            episodes = _candidate_episodes(results, windows, list(FEES_STRESS),
                                           targets, by_id, CONTAINER_SNAP_TRAIN)
            control_cov = _control_coverage(results, windows, list(FEES_STRESS),
                                            CONTAINER_SNAP_TRAIN)
            stress_records = _aggregate_role_records(
                "train", episodes, windows, list(FEES_STRESS),
                {v: by_id[v] for v in targets})
            all_records = list(screen_records) + list(stress_records)
            longest = longest_train_segment(snapshot_manifest)
            if longest is None:
                raise ValueError("snapshot TRAIN sin segmentos para sesgo")
            bias_verdicts: dict = {}
            bias_detail: dict = {}
            bias_causes: dict = dict(stats["causes"])
            bias_charges = 0

            def _bias_progress():
                nonlocal bias_charges
                bias_charges += 1
                launch._atomic_replace(session_dir / "progress.json", {
                    "phase": "finalists", "session": session_dir.name,
                    "done": stats["done"], "succeeded": stats["succeeded"],
                    "failed": stats["failed"], "total": stats["total"],
                    "remaining": 0,
                    "bias_native_charges": bias_charges,
                    "consumed": state.get("consumed"),
                    "remaining_budget_s": float(state.get("budget_limit", BUDGET_SECONDS)) - float(state.get("consumed", 0)),
                    "failures_by_cause": dict(bias_causes),
                    "updated_at": _utcnow_iso(),
                })

            for vid in targets:
                verdict, detail = _run_bias_gate(
                    variant=by_id[vid],
                    class_name=str(by_id[vid]["class_name"]),
                    seg_meta=longest, snap_train_root=CONTAINER_SNAP_TRAIN,
                    session_dir=session_dir, state=state, state_path=state_path,
                    manifest_in=manifest_in, code_hashes=code_hashes,
                    causes=bias_causes, progress_cb=_bias_progress)
                bias_verdicts[vid] = bool(verdict)
                bias_detail[vid] = detail
            validation_ids = choose_validation_candidates(
                all_records, targets, dict(bias_verdicts))
            if not validation_ids:
                launch._atomic_replace(session_dir / "report.json", {
                    **base_report, "status": "SUCCEEDED",
                    "finished_at": _utcnow_iso(),
                    "verdict": "NO_CANDIDATE",
                    "reasons": ["ningun finalista supera stress+bias; holdout no abierto"],
                    "bias_verdicts": dict(bias_verdicts),
                    "bias_detail": bias_detail,
                    "control_coverage": control_cov,
                    "excluded_coverage_years": excluded,
                    "consumed": state.get("consumed"),
                })
                _record_phase_report(state, state_path, "finalists", session_dir,
                                     "NO_CANDIDATE")
                print(str(session_dir / "report.json"))
                return 1
            if state.get("validation_ids") is None:
                freeze_validation_selection(state, list(validation_ids))
                _save_state_atomic(state_path, state)
            elif sorted(str(v) for v in (state.get("validation_ids") or [])) != sorted(validation_ids):
                raise ValueError("validation_ids congelados difieren de los recalculados")
            launch._atomic_replace(session_dir / "report.json", {
                **base_report, "status": "SUCCEEDED",
                "finished_at": _utcnow_iso(),
                "verdict": "TOP3",
                "validation_ids": sorted(validation_ids),
                "bias_verdicts": dict(bias_verdicts),
                "bias_detail": bias_detail,
                "stress_records": len(stress_records),
                "control_coverage": control_cov,
                "excluded_coverage_years": excluded,
                "consumed": state.get("consumed"),
                "lineage": {
                    "image_id": manifest_in.get("image_id"),
                    "generated_source_sha256": manifest_in.get("generated_source_sha256"),
                    "config_hash": manifest_in.get("config_hash"),
                    "definition_hash": manifest_in.get("definition_hash"),
                    "screen_report_sha256": screen_sha,
                },
            })
            _record_phase_report(state, state_path, "finalists", session_dir, "TOP3")
            print(str(session_dir / "report.json"))
            return 0
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        print(f"finalists: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            sessions = Path(CONTAINER_SEARCH) / "sessions"
            cands = sorted(sessions.glob("finalists-*/report.json"))
            if cands:
                last = cands[-1]
                try:
                    payload = json.loads(last.read_text(encoding="utf-8"))
                    if payload.get("status") == "RUNNING":
                        launch._atomic_replace(last, {
                            **payload, "status": "FAILED",
                            "finished_at": _utcnow_iso(),
                            "error": f"{type(exc).__name__}: {exc}"})
                        print(str(last))
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
        return 1


def _resolve_role_binding(role: str, state: dict) -> tuple[dict, dict]:
    """Binding + grant del rol con cadena de custodia verificada.

    Localiza control/<role>-snapshot-*.json (definition + ids congelados,
    ultimo por created_at), su grant asociado y el report que lo autoriza;
    verifica SHA encadenados. Lanza ValueError si falta o diverge.
    """
    if role == "validation":
        expected_ids = sorted(str(v) for v in (state.get("validation_ids") or []))
        if not expected_ids:
            raise ValueError("sin validation_ids congelados")
        # El grant encadena directamente finalists/TOP3. La ascendencia hasta
        # SCREEN se comprueba dentro del propio reporte finalists.
        frep, frep_sha = resolve_phase_report(state, "finalists")
        _screen, screen_sha = resolve_phase_report(state, "screen")
        if str(frep.get("screen_report_sha256") or "") != screen_sha:
            raise ValueError("finalists no encadena el SCREEN canonico")
        prefix, grant_prefix = "validation-snapshot-", "validation-grant-"
        report_key = "train_report_sha256"
    elif role == "test":
        cand = str(state.get("test_candidate") or "")
        if not cand:
            raise ValueError("sin test_candidate reservada")
        expected_ids = [cand]
        frep, frep_sha = resolve_phase_report(state, "validation")
        prefix, grant_prefix = "test-snapshot-", "test-grant-"
        report_key = "validation_report_sha256"
    else:
        raise ValueError(f"binding solo validation/test, no {role!r}")
    control = Path(CONTAINER_SEARCH) / "control"
    cands = []
    for path in sorted(control.glob(f"{prefix}*.json")):
        try:
            binding = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(binding, dict):
            continue
        if binding.get("kind") != SNAP_BIND_KIND:
            continue
        if str(binding.get("definition_hash")) != str(state.get("definition_hash")):
            continue
        if sorted(str(v) for v in (binding.get("expected_ids") or [])) != expected_ids:
            continue
        cands.append((str(binding.get("created_at") or ""), path, binding))
    if not cands:
        raise ValueError(f"sin binding {role} (prepare-phase pendiente)")
    cands.sort(key=lambda t: (t[0], str(t[1])))
    _, bind_path, binding = cands[-1]
    grant_id = str(binding.get("grant_id") or "")
    grant_path = control / f"{grant_prefix}{grant_id[:8]}.json"
    # El grant puede vivir con nombre completo grant_id; resuelve por prefijo.
    if not grant_path.is_file():
        matches = sorted(control.glob(f"{grant_prefix}*.json"))
        found = None
        for cand_path in matches:
            try:
                cand_grant = json.loads(cand_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(cand_grant.get("grant_id") or "") == grant_id:
                found = cand_path
                break
        if found is None:
            raise ValueError(f"grant {role} ausente para binding")
        grant_path = found
    try:
        grant = json.loads(grant_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"grant ilegible: {exc}") from exc
    from operations.history import verify_holdout_grant

    verify_holdout_grant(role, grant, str(state.get("definition_hash")),
                         expected_ids, frep_sha)
    if str(grant.get(report_key) or "") != frep_sha:
        raise ValueError(f"grant sin {report_key} del report verificado")
    if str(binding.get(report_key) or "") != frep_sha:
        raise ValueError(f"binding sin {report_key} del report verificado")
    return binding, grant


def _load_role_snapshot(role: str, binding: dict) -> tuple[dict, Path]:
    """Manifiesto del rol en ruta fija con rol/rango/hashes cotejados."""
    snap_root, snap_manifest_path = _role_snap_paths(role)
    manifest = _load_snapshot_manifest_file(Path(snap_manifest_path))
    _verify_snapshot_scope(manifest, role)
    if launch.file_hash(Path(snap_manifest_path)) != binding.get("manifest_sha256"):
        raise ValueError(f"snapshot {role} manifiesto alterado tras binding")
    for key in ("snapshot_id", "snapshot_dir", "whole_5m_sha256",
                "whole_1h_sha256", "range_start", "range_end"):
        if str(manifest.get(key)) != str(binding.get(key)):
            raise ValueError(f"snapshot {role} diverge del binding ({key})")
    _verify_snapshot_data_files(Path(snap_root), manifest)
    return manifest, Path(snap_root)


def cmd_validation() -> int:
    from research.evaluation import plan_windows

    try:
        manifest_in = _load_input()
        code_hashes = _verify_current_against_input(manifest_in)
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        print(f"validation: fase no autorizada / input-codigo no valido: {exc}",
              file=sys.stderr)
        return 2
    from research.campaign import generate_variants
    from research.selection import choose_test_candidate

    variants = generate_variants()
    by_id = {str(v["id"]): v for v in variants}
    by_class = {str(v["class_name"]): v for v in variants}
    if len(variants) != 72:
        print("validation: registry sin 72", file=sys.stderr)
        return 1
    try:
        with _container_locked():
            # Estado mutable bajo el mismo lock: re-lectura fresca tras entrar.
            try:
                state, state_path = _load_state()
                _phase_guard(manifest_in, state, "validation")
            except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
                print(f"validation: fase no autorizada / estado no valido: {exc}",
                      file=sys.stderr)
                return 2
            validation_ids = list(state.get("validation_ids") or [])
            if not validation_ids:
                print("validation: finalists pendiente (validation_ids vacio)", file=sys.stderr)
                return 2
            if len(validation_ids) > 3:
                print(f"validation: mas de 3 validation_ids: {len(validation_ids)}", file=sys.stderr)
                return 1
            for vid in validation_ids:
                if str(vid) not in by_id:
                    print(f"validation: validation_id desconocida: {vid!r}", file=sys.stderr)
                    return 1
            try:
                binding, _grant = _resolve_role_binding("validation", state)
            except ValueError as exc:
                print(f"validation: autorizacion holdout no valida: {exc}", file=sys.stderr)
                return 2
            try:
                snapshot_manifest, _snap_root = _load_role_snapshot("validation", binding)
            except (FileNotFoundError, ValueError, OSError) as exc:
                print(f"validation: snapshot no valido: {exc}", file=sys.stderr)
                return 2
            refs = state.get("phase_reports") or {}
            if isinstance(refs.get("validation"), dict) and refs["validation"].get("status") == "SUCCEEDED":
                try:
                    _resumed, _shown, _rc = _resume_completed("validation", state)
                except (FileNotFoundError, ValueError, OSError) as exc:
                    print(f"validation: artefacto registrado invalido: {exc}", file=sys.stderr)
                    return 1
                print(_shown)
                return _rc
            sessions = Path(CONTAINER_SEARCH) / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            session_dir = sessions / f"validation-{_slug()}"
            session_dir.mkdir(parents=True, exist_ok=False)
            batch_dir = session_dir / "batches"
            batch_dir.mkdir(parents=True, exist_ok=False)
            base_report = {
                "kind": VALIDATION_SESSION_KIND,
                "status": "RUNNING",
                "session": session_dir.name,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "definition_hash": manifest_in.get("definition_hash"),
                "grant_id": binding.get("grant_id"),
            }
            launch._atomic_create_new(session_dir / "report.json", base_report)
            windows = plan_windows("validation", snapshot_manifest)
            if not windows:
                raise ValueError("validation sin ventanas (snapshot vacio)")
            from market.history import partition_bounds

            rstart, rend = partition_bounds("validation")
            years_cov = sorted({int(w["year"]) for w in windows})
            excluded = sorted(set(range(rstart.year, rend.year)) - set(years_cov))
            targets = sorted(str(v) for v in validation_ids)
            class_names = [str(by_id[v]["class_name"]) for v in targets]
            groups = [{
                "names": class_names, "use_list": True,
                "spath": CONTAINER_GENERATED,
                "params": {n: by_class[n]["params"] for n in class_names},
                "suffix": "all",
            }]
            groups.append({
                "names": [CONTROL_STRATEGY], "use_list": False,
                "spath": CONTAINER_STRATEGY_CODE_PATH,
                "params": {CONTROL_STRATEGY: {"control": True}},
                "suffix": "control",
            })
            seg_meta_by_dir = {str(m.get("seg_dir")): m
                               for m in snapshot_manifest.get("segments_meta") or []}
            by_class_params = {str(v["class_name"]): v["params"] for v in variants}
            results, stats = _execute_native_matrix(
                phase="validation", role="validation", windows=windows,
                fees=list(FEES_ALL), groups=groups,
                snap_root=CONTAINER_SNAP_VAL,
                session_dir=session_dir, batch_dir=batch_dir,
                state=state, state_path=state_path,
                manifest_in=manifest_in, code_hashes=code_hashes,
                seg_meta_by_dir=seg_meta_by_dir,
                by_class_params=by_class_params)
            if stats["failed"] > 0:
                return _fail_session(session_dir, base_report, stats["succeeded"],
                                     stats["failed"], stats["total"],
                                     f"{stats['failed']}/{stats['total']} batches FAILED")
            episodes = _candidate_episodes(results, windows, list(FEES_ALL),
                                           targets, by_id, CONTAINER_SNAP_VAL)
            control_cov = _control_coverage(results, windows, list(FEES_ALL),
                                            CONTAINER_SNAP_VAL)
            records = _aggregate_role_records(
                "validation", episodes, windows, list(FEES_ALL),
                {v: by_id[v] for v in targets})
            chosen = choose_test_candidate(records, targets)
            if chosen is None:
                launch._atomic_replace(session_dir / "report.json", {
                    **base_report, "status": "SUCCEEDED",
                    "finished_at": _utcnow_iso(),
                    "verdict": "NO_CANDIDATE",
                    "reasons": ["ninguna validada supera gates VAL; TEST no abierto"],
                    "control_coverage": control_cov,
                    "excluded_coverage_years": excluded,
                    "consumed": state.get("consumed"),
                })
                _record_phase_report(state, state_path, "validation", session_dir,
                                     "NO_CANDIDATE")
                print(str(session_dir / "report.json"))
                return 1
            launch._atomic_replace(session_dir / "report.json", {
                **base_report, "status": "SUCCEEDED",
                "finished_at": _utcnow_iso(),
                "verdict": "CANDIDATE",
                "chosen_test_candidate": str(chosen),
                "records": records,
                "control_coverage": control_cov,
                "excluded_coverage_years": excluded,
                "consumed": state.get("consumed"),
                "lineage": {
                    "image_id": manifest_in.get("image_id"),
                    "generated_source_sha256": manifest_in.get("generated_source_sha256"),
                    "config_hash": manifest_in.get("config_hash"),
                    "definition_hash": manifest_in.get("definition_hash"),
                    "grant_id": binding.get("grant_id"),
                    "snapshot_manifest_sha256": binding.get("manifest_sha256"),
                },
            })
            _record_phase_report(state, state_path, "validation", session_dir,
                                 "CANDIDATE")
            print(str(session_dir / "report.json"))
            return 0
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        print(f"validation: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            sessions = Path(CONTAINER_SEARCH) / "sessions"
            cands = sorted(sessions.glob("validation-*/report.json"))
            if cands:
                last = cands[-1]
                try:
                    payload = json.loads(last.read_text(encoding="utf-8"))
                    if payload.get("status") == "RUNNING":
                        launch._atomic_replace(last, {
                            **payload, "status": "FAILED",
                            "finished_at": _utcnow_iso(),
                            "error": f"{type(exc).__name__}: {exc}"})
                        print(str(last))
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
        return 1


TEST_EVIDENCE_KIND = "btc-lab-test-evidence"


def _write_test_evidence(*, candidate, variant, manifest_in, grant,
                         snapshot_ref_test, validation_sha, finalists_sha,
                         economics, records):
    """Evidencia cientifica inmutable del PASS (separada del reporte).

    Solo se escribe con economia PASS + tecnica exacta True. Contenido
    determinista (sin timestamps ni sesion) para igualdad idempotente.
    Retorna (control_path, sha256).
    """
    evidence = {
        "kind": TEST_EVIDENCE_KIND,
        "candidate_id": str(candidate),
        "class_name": str(variant.get("class_name")),
        "params": dict(variant.get("params") or {}),
        "stop": float(variant.get("stop")),
        "risk_profile": str(variant.get("risk_profile")),
        "campaign_id": CAMPAIGN_ID,
        "definition_hash": manifest_in.get("definition_hash"),
        "image_ref": manifest_in.get("image_ref"),
        "image_id": manifest_in.get("image_id"),
        "config_hash": manifest_in.get("config_hash"),
        "generated_source_sha256": manifest_in.get("generated_source_sha256"),
        "code_hashes": {
            "search_hash": manifest_in.get("search_hash"),
            "evaluation_hash": manifest_in.get("evaluation_hash"),
            "campaign_hash": manifest_in.get("campaign_hash"),
            "selection_hash": manifest_in.get("selection_hash"),
            "state_hash": manifest_in.get("state_hash"),
            "candidates_hash": manifest_in.get("candidates_hash"),
            "control_hash": manifest_in.get("control_hash"),
            "equity_hash": manifest_in.get("equity_hash"),
        },
        "snapshot_test_ref": dict(snapshot_ref_test),
        "grant": {k: grant.get(k) for k in (
            "campaign_id", "candidate_id", "grant_id", "definition_hash",
            "phase", "validation_report_sha256") if k in grant},
        "economics": dict(economics),
        "technical_bias_pass": True,
        "records": [dict(r) for r in (records or [])],
        "reports": {
            "validation_sha256": validation_sha,
            "finalists_sha256": finalists_sha,
        },
    }
    control_path = (Path(CONTAINER_SEARCH) / "control"
                    / f"test-evidence-{candidate}-{str(grant.get('grant_id') or '')[:8]}.json")
    if control_path.is_file():
        try:
            prev = json.loads(control_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"evidencia existente ilegible: {exc}") from exc
        if prev != evidence:
            raise ValueError("evidencia existente difiere (inmutable, sin overwrite)")
    else:
        launch._atomic_create_new(control_path, evidence)
    return control_path, _sha256_file(control_path)


def _write_paper_bundle(*, session_dir, candidate, variant, manifest_in,
                        state, grant, snapshot_ref_test, evidence_sha,
                        validation_sha, finalists_sha, economics):
    code_hashes_now = {
        "search_hash": manifest_in.get("search_hash"),
        "evaluation_hash": manifest_in.get("evaluation_hash"),
        "campaign_hash": manifest_in.get("campaign_hash"),
        "selection_hash": manifest_in.get("selection_hash"),
        "state_hash": manifest_in.get("state_hash"),
        "candidates_hash": manifest_in.get("candidates_hash"),
        "control_hash": manifest_in.get("control_hash"),
        "equity_hash": manifest_in.get("equity_hash"),
    }
    bundle = {
        "kind": PAPER_BUNDLE_KIND,
        "status": "SEALED",
        "candidate_id": str(candidate),
        "class_name": str(variant.get("class_name")),
        "params": dict(variant.get("params") or {}),
        "stop": float(variant.get("stop")),
        "risk_profile": str(variant.get("risk_profile")),
        "campaign_id": CAMPAIGN_ID,
        "definition_hash": manifest_in.get("definition_hash"),
        "image_ref": manifest_in.get("image_ref"),
        "image_id": manifest_in.get("image_id"),
        "config_hash": manifest_in.get("config_hash"),
        "generated_source_sha256": manifest_in.get("generated_source_sha256"),
        "code_hashes": code_hashes_now,
        "snapshot_test_ref": dict(snapshot_ref_test),
        "grant": {k: grant.get(k) for k in (
            "campaign_id", "candidate_id", "grant_id", "definition_hash",
            "phase", "validation_report_sha256") if k in grant},
        "economics": dict(economics),
        "reports": {
            "evidence_sha256": evidence_sha,
            "validation_sha256": validation_sha,
            "finalists_sha256": finalists_sha,
        },
        "runtime": {
            "user_data": f"storage/runtime/paper-{candidate}",
            "db": f"storage/runtime/paper-{candidate}/tradesv3.paper.dryrun.sqlite",
            "note": ("DB y monitor propios, separados del baseline; "
                     "activacion solo por coordinator, sin timer ni arranque aqui"),
        },
    }
    control = Path(CONTAINER_SEARCH) / "control"
    for prev_path in sorted(control.glob("paper-bundle-*.json")):
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"bundle publicado ilegible ({prev_path.name}): {exc}") from exc
        if not isinstance(prev, dict):
            raise ValueError(f"bundle publicado invalido: {prev_path.name}")
        if str(prev.get("candidate_id") or "") != str(candidate):
            raise ValueError(
                f"bundle publicado para otra candidata ({prev_path.name}): "
                "overwrite prohibido")
    session_path = Path(session_dir) / "paper-bundle.json"
    launch._atomic_create_new(session_path, bundle)
    control_path = (control
                    / f"paper-bundle-{candidate}-{str(grant.get('grant_id') or '')[:8]}.json")
    if control_path.is_file():
        try:
            prev = json.loads(control_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"bundle existente ilegible: {exc}") from exc
        if prev != bundle:
            raise ValueError("bundle existente difiere (inmutable, sin overwrite)")
    else:
        launch._atomic_create_new(control_path, bundle)
    return session_path, control_path


def cmd_test() -> int:
    from research.evaluation import plan_windows
    from research.selection import test_verdict
    from research.state import authorize_test_resume

    try:
        manifest_in = _load_input()
        code_hashes = _verify_current_against_input(manifest_in)
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        print(f"test: fase no autorizada / input-codigo no valido: {exc}",
              file=sys.stderr)
        return 2
    from research.campaign import generate_variants

    variants = generate_variants()
    by_id = {str(v["id"]): v for v in variants}
    if len(variants) != 72:
        print("test: registry sin 72", file=sys.stderr)
        return 1
    try:
        with _container_locked():
            # Estado mutable bajo el mismo lock: re-lectura fresca tras entrar.
            try:
                state, state_path = _load_state()
                _phase_guard(manifest_in, state, "test")
            except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
                print(f"test: fase no autorizada / estado no valido: {exc}",
                      file=sys.stderr)
                return 2
            candidate = str(state.get("test_candidate") or "")
            grant_id = str(state.get("test_grant_id") or "")
            if not candidate or not grant_id or state.get("test_consumed") is not True:
                print("test: sin reserva TEST consumida (prepare-phase test pendiente)",
                      file=sys.stderr)
                return 2
            if not authorize_test_resume(state, candidate,
                                         grant_id, str(state.get("definition_hash") or "")):
                print("test: grant no coincide (cambio tras consumo prohibido)", file=sys.stderr)
                return 1
            control = Path(CONTAINER_SEARCH) / "control"
            grant = None
            for cand_path in sorted(control.glob("test-grant-*.json")):
                try:
                    cand_grant = json.loads(cand_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if str(cand_grant.get("grant_id") or "") == grant_id:
                    grant = cand_grant
                    break
            if grant is None:
                print("test: fichero de grant ausente para la reserva", file=sys.stderr)
                return 1
            if (str(grant.get("candidate_id")) != candidate
                    or str(grant.get("definition_hash")) != str(state.get("definition_hash"))):
                print("test: grant diverge del estado (tuning tras consumo prohibido)",
                      file=sys.stderr)
                return 1
            if candidate not in by_id:
                print(f"test: candidata desconocida: {candidate!r}", file=sys.stderr)
                return 1
            try:
                binding, _ = _resolve_role_binding("test", state)
            except ValueError as exc:
                print(f"test: autorizacion holdout no valida: {exc}", file=sys.stderr)
                return 2
            if str(binding.get("grant_id") or "") != grant_id:
                print("test: binding de otro grant (re-consulta con el grant reservado)",
                      file=sys.stderr)
                return 1
            try:
                snapshot_manifest, _snap_root = _load_role_snapshot("test", binding)
            except (FileNotFoundError, ValueError, OSError) as exc:
                print(f"test: snapshot no valido: {exc}", file=sys.stderr)
                return 2
            try:
                _frep, frep_sha = resolve_phase_report(state, "finalists")
            except (FileNotFoundError, ValueError, OSError) as exc:
                print(f"test: report finalists no verificado: {exc}", file=sys.stderr)
                return 2
            bias_map = _frep.get("bias_verdicts") or {}
            # Puerta tecnica exacta: solo el bool verificado True activa.
            tech_ok = bias_map.get(candidate) is True
            refs = state.get("phase_reports") or {}
            if isinstance(refs.get("test"), dict) and refs["test"].get("status") == "SUCCEEDED":
                try:
                    _resumed, _shown, _rc = _resume_completed("test", state)
                except (FileNotFoundError, ValueError, OSError) as exc:
                    print(f"test: artefacto registrado invalido: {exc}", file=sys.stderr)
                    return 1
                print(_shown)
                return _rc
            sessions = Path(CONTAINER_SEARCH) / "sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            session_dir = sessions / f"test-{_slug()}"
            session_dir.mkdir(parents=True, exist_ok=False)
            batch_dir = session_dir / "batches"
            batch_dir.mkdir(parents=True, exist_ok=False)
            base_report = {
                "kind": TEST_SESSION_KIND,
                "status": "RUNNING",
                "session": session_dir.name,
                "created_at": _utcnow_iso(),
                "input_id": manifest_in.get("input_id"),
                "definition_hash": manifest_in.get("definition_hash"),
                "grant_id": grant_id,
                "candidate_id": candidate,
            }
            launch._atomic_create_new(session_dir / "report.json", base_report)
            windows = plan_windows("test", snapshot_manifest)
            if not windows:
                raise ValueError("test sin ventanas (snapshot vacio)")
            from market.history import partition_bounds

            rstart, rend = partition_bounds("test")
            years_cov = sorted({int(w["year"]) for w in windows})
            excluded = sorted(set(range(rstart.year, rend.year)) - set(years_cov))
            cname = str(by_id[candidate]["class_name"])
            groups = [{
                "names": [cname], "use_list": True,
                "spath": CONTAINER_GENERATED,
                "params": {cname: by_id[candidate]["params"]},
                "suffix": "all",
            }]
            groups.append({
                "names": [CONTROL_STRATEGY], "use_list": False,
                "spath": CONTAINER_STRATEGY_CODE_PATH,
                "params": {CONTROL_STRATEGY: {"control": True}},
                "suffix": "control",
            })
            seg_meta_by_dir = {str(m.get("seg_dir")): m
                               for m in snapshot_manifest.get("segments_meta") or []}
            by_class_params = {str(v["class_name"]): v["params"] for v in variants}
            results, stats = _execute_native_matrix(
                phase="test", role="test", windows=windows,
                fees=list(FEES_ALL), groups=groups,
                snap_root=CONTAINER_SNAP_TEST,
                session_dir=session_dir, batch_dir=batch_dir,
                state=state, state_path=state_path,
                manifest_in=manifest_in, code_hashes=code_hashes,
                seg_meta_by_dir=seg_meta_by_dir,
                by_class_params=by_class_params)
            if stats["failed"] > 0:
                return _fail_session(session_dir, base_report, stats["succeeded"],
                                     stats["failed"], stats["total"],
                                     f"{stats['failed']}/{stats['total']} batches FAILED")
            episodes = _candidate_episodes(results, windows, list(FEES_ALL),
                                           [candidate], by_id, CONTAINER_SNAP_TEST)
            control_cov = _control_coverage(results, windows, list(FEES_ALL),
                                            CONTAINER_SNAP_TEST)
            records = _aggregate_role_records(
                "test", episodes, windows, list(FEES_ALL), {candidate: by_id[candidate]})
            economics = test_verdict(records, candidate)
            if economics.get("verdict") == "PASS" and tech_ok:
                final = "PASS"
            elif economics.get("verdict") == "PASS":
                # Economia PASS pero tecnica no exacta: NONPASS terminal
                # (nunca se hereda el veredicto economico).
                final = "FAIL"
            else:
                final = economics.get("verdict")
            if final != "PASS":
                reasons = list(economics.get("reasons") or [])
                if not tech_ok:
                    reasons = reasons + ["tecnica: bias no PASS en finalists"]
                launch._atomic_replace(session_dir / "report.json", {
                    **base_report, "status": "SUCCEEDED",
                    "finished_at": _utcnow_iso(),
                    "verdict": final,
                    "economics": dict(economics),
                    "technical_bias_pass": tech_ok,
                    "reasons": reasons,
                    "control_coverage": control_cov,
                    "excluded_coverage_years": excluded,
                    "consumed": state.get("consumed"),
                })
                _record_phase_report(state, state_path, "test", session_dir, final)
                print(str(session_dir / "report.json"))
                return 1
            _vrep, vrep_sha = resolve_phase_report(state, "validation")
            snapshot_ref_test = {
                "manifest": str(binding.get("manifest")),
                "manifest_sha256": str(binding.get("manifest_sha256")),
                "snapshot_id": str(binding.get("snapshot_id")),
                "snapshot_dir": str(binding.get("snapshot_dir")),
                "whole_5m_sha256": str(binding.get("whole_5m_sha256")),
                "whole_1h_sha256": str(binding.get("whole_1h_sha256")),
                "range_start": str(binding.get("range_start")),
                "range_end": str(binding.get("range_end")),
            }
            # Evidencia cientifica inmutable y separada del reporte
            # operacional (rompe el ciclo de hash): solo existe con
            # economia PASS + tecnica exacta True.
            _evidence_path, evidence_sha = _write_test_evidence(
                candidate=candidate, variant=by_id[candidate],
                manifest_in=manifest_in, grant=grant,
                snapshot_ref_test=snapshot_ref_test,
                validation_sha=vrep_sha, finalists_sha=frep_sha,
                economics=economics, records=records)
            try:
                _write_paper_bundle(
                    session_dir=session_dir, candidate=candidate,
                    variant=by_id[candidate], manifest_in=manifest_in,
                    state=state, grant=grant, snapshot_ref_test=snapshot_ref_test,
                    evidence_sha=evidence_sha, validation_sha=vrep_sha,
                    finalists_sha=frep_sha, economics=economics)
            except (ValueError, OSError, RuntimeError) as exc:
                # Fallo de publicacion: reporte FAILED, paper_ready False y
                # estado no listo aunque la economia sea PASS. Sin PASS falso.
                state["paper_ready"] = False
                launch._atomic_replace(session_dir / "report.json", {
                    **base_report, "status": "FAILED",
                    "finished_at": _utcnow_iso(),
                    "verdict": "FAILED",
                    "economics": dict(economics),
                    "technical_bias_pass": tech_ok,
                    "publication_error": f"{type(exc).__name__}: {exc}",
                    "evidence_sha256": evidence_sha,
                    "consumed": state.get("consumed"),
                })
                _record_phase_report(state, state_path, "test", session_dir,
                                     "FAILED", status="FAILED")
                print(str(session_dir / "report.json"))
                print(f"test: publicacion FAILED {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                return 1
            launch._atomic_replace(session_dir / "report.json", {
                **base_report, "status": "SUCCEEDED",
                "finished_at": _utcnow_iso(),
                "verdict": "PASS",
                "economics": dict(economics),
                "technical_bias_pass": tech_ok,
                "records": len(records),
                "control_coverage": control_cov,
                "excluded_coverage_years": excluded,
                "consumed": state.get("consumed"),
                "lineage": {
                    "image_id": manifest_in.get("image_id"),
                    "generated_source_sha256": manifest_in.get("generated_source_sha256"),
                    "config_hash": manifest_in.get("config_hash"),
                    "definition_hash": manifest_in.get("definition_hash"),
                    "grant_id": grant_id,
                    "snapshot_manifest_sha256": binding.get("manifest_sha256"),
                    "evidence_sha256": evidence_sha,
                },
            })
            state["paper_ready"] = True
            _record_phase_report(state, state_path, "test", session_dir, "PASS")
            print(str(session_dir / "report.json"))
            return 0
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        print(f"test: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            sessions = Path(CONTAINER_SEARCH) / "sessions"
            cands = sorted(sessions.glob("test-*/report.json"))
            if cands:
                last = cands[-1]
                try:
                    payload = json.loads(last.read_text(encoding="utf-8"))
                    if payload.get("status") == "RUNNING":
                        launch._atomic_replace(last, {
                            **payload, "status": "FAILED",
                            "finished_at": _utcnow_iso(),
                            "error": f"{type(exc).__name__}: {exc}"})
                        print(str(last))
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
        return 1


def cmd_resume(phase: str) -> int:
    if phase not in PHASES:
        print(f"resume: fase desconocida {phase!r} (usar screen/finalists/validation/test)",
              file=sys.stderr)
        return 2
    if phase == "screen":
        try:
            manifest_in = _load_input()
            _verify_current_against_input(manifest_in)
            state, _ = _load_state()
        except (FileNotFoundError, ValueError, OSError) as exc:
            print(f"resume: no autorizado: {exc}", file=sys.stderr)
            return 2
        return cmd_screen()
    if phase in ("finalists", "validation"):
        try:
            manifest_in = _load_input()
            _verify_current_against_input(manifest_in)
            state, _ = _load_state()
            _phase_guard(manifest_in, state, phase)
        except (FileNotFoundError, ValueError, OSError) as exc:
            print(f"resume: no autorizado: {exc}", file=sys.stderr)
            return 2
        return {"finalists": cmd_finalists, "validation": cmd_validation}[phase]()
    if phase == "test":
        try:
            manifest_in = _load_input()
            _verify_current_against_input(manifest_in)
            state, _ = _load_state()
            _phase_guard(manifest_in, state, phase)
        except (FileNotFoundError, ValueError, OSError) as exc:
            print(f"resume: no autorizado: {exc}", file=sys.stderr)
            return 2
        from research.state import authorize_test_resume

        if not authorize_test_resume(state, str(state.get("test_candidate") or ""),
                                     str(state.get("test_grant_id") or ""),
                                     str(state.get("definition_hash") or "")):
            print("resume: grant TEST no coincide (cambio tras consumo prohibido)",
                  file=sys.stderr)
            return 1
        return cmd_test()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="operations.search")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="preflight host + input congelado")
    prep.add_argument("--code-root", default=os.environ.get("LAB_CODE_ROOT", ""))
    prep.add_argument("--storage-root", default=os.environ.get("LAB_STORAGE_ROOT", ""))
    prep.add_argument("--image", default=launch.PINNED_IMAGE)
    prep.add_argument("--train-snapshot", required=True,
                      help="manifiesto TRAIN en history/snapshots (nombre simple)")
    sub.add_parser("screen", help="TRAIN 72 real fees 0.001/0.002 + top9")
    sub.add_parser("finalists", help="top9 stress 0.0015/0.003 + bias + top3")
    sub.add_parser("validation", help="max3 all4 fees + candidata TEST")
    sub.add_parser("test", help="una all4 fees + bundle solo si PASS")
    pp = sub.add_parser("prepare-phase",
                        help="autorizacion holdout host: grant + input de rol")
    pp.add_argument("--phase", required=True, choices=("validation", "test"))
    pp.add_argument("--search-input", required=True,
                    help="input search PREPARED explicito (ruta host, sin latest)")
    pp.add_argument("--code-root", default=os.environ.get("LAB_CODE_ROOT", ""))
    pp.add_argument("--storage-root", default=os.environ.get("LAB_STORAGE_ROOT", ""))
    pp.add_argument("--image", default=launch.PINNED_IMAGE)
    pp.add_argument("--snapshot", default=None,
                    help="manifiesto del rol en history/snapshots (segundo paso)")
    pp.add_argument("--grant-id", default=None,
                    help="grant existente (resume o segundo paso)")
    sub.add_parser("report", help="dictamen reproducible sin holdout extra")
    sub.add_parser("status", help="estado de campana y budget")
    res = sub.add_parser("resume", help="reanuda fase autorizada explicita")
    res.add_argument("--phase", required=True,
                     help="screen|finalists|validation|test")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        if not args.code_root or not args.storage_root:
            print("prepare exige --code-root y --storage-root (o LAB_CODE_ROOT/LAB_STORAGE_ROOT)",
                  file=sys.stderr)
            return 2
        try:
            print(prepare_search(args.code_root, args.storage_root,
                                 args.image, args.train_snapshot))
            return 0
        except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
            print(f"prepare: {exc}", file=sys.stderr)
            return 1
    if args.command == "status":
        return cmd_status()
    if args.command == "report":
        return cmd_report()
    if args.command == "screen":
        return cmd_screen()
    if args.command in ("finalists", "validation", "test"):
        return {"finalists": cmd_finalists,
                "validation": cmd_validation,
                "test": cmd_test}[args.command]()
    if args.command == "resume":
        return cmd_resume(args.phase)
    if args.command == "prepare-phase":
        if not args.code_root or not args.storage_root or not args.search_input:
            print("prepare-phase exige --search-input, --code-root y --storage-root",
                  file=sys.stderr)
            return 2
        try:
            print(cmd_prepare_phase(args.phase, args.search_input, args.code_root,
                                    args.storage_root, args.image,
                                    args.snapshot, args.grant_id))
            return 0
        except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
            print(f"prepare-phase: {exc}", file=sys.stderr)
            return 1
    parser.error("comando desconocido")
    return 2


if __name__ == "__main__":
    sys.exit(main())
