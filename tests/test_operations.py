"""T02 RED: launcher, manifiesto y health (contrato T03 para implementer).

validate_config(config, environ): solo canonica BTC spot dry-run con
  pares en exchange.pair_whitelist (Freqtrade real). ValueError + rechazo
  FREQTRADE__* antes de cualquier subprocess.
build_command(code_root, storage_root): argv de EJECUCION flexible (rutas
  host como datos, sin shell); fija unica config + NoTradeSmoke.
file_hash(path): sha256 hex; FileNotFoundError si falta.
prepare_smoke(code_root, storage_root, image_ref): preflight host con Git
  limpio REAL y docker image inspect REAL (sin fallback). expected_command
  normalizado a contenedor (/opt/btc-lab, /lab-storage, /freqtrade/
  user_data). Fallo => excepcion, ningun exito.
run_smoke(code_root, storage_root, input_path): revalida hashes y ejecuta
  con subprocess.Popen (propaga SIGTERM/SIGINT; senales reales a T05).
  Manifiesto propio atomico en runs/, id unica, UTC, exit+estado; nunca
  sobrescribe; parcial/ausente nunca es exito.
evaluate_health(inspect, log, free_bytes, now): solo vale el heartbeat
  AUTENTICO 'YYYY-MM-DD HH:MM:SS,mmm - freqtrade.worker - INFO - Bot
  heartbeat. PID=.. state=RUNNING'; otra fecha no prueba vida; heartbeat
  < StartedAt no cuenta. Umbral 180 s. {status: healthy|unhealthy|
  unknown, reasons: [...]}; no-cero salvo healthy.
RED: import condicional + setUp con fail explicito. Git/imagen via run
mocks realistas; ejecucion via Popen; reloj controlado; sin red/Docker.
Run: python -m unittest discover -s tests -v (raiz del worktree).
"""

import contextlib
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
from unittest import mock

try:
    from operations import health
    _HEALTH_ERR = None
except Exception as exc:
    health = None
    _HEALTH_ERR = exc

try:
    from operations import launch
    _LAUNCH_ERR = None
except Exception as exc:
    launch = None
    _LAUNCH_ERR = exc


PINNED_IMAGE = (
    "freqtradeorg/freqtrade:2026.8@sha256:"
    "4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4"
)
COMMIT = "9f3a1c2d4e5b6a7890abcdef1234567890abcdef12"
IMAGE_ID = "sha256:" + "4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4"


def _canonical_config():
    return {
        "exchange": {
            "name": "binance",
            "key": "",
            "secret": "",
            "password": "",
            "pair_whitelist": ["BTC/USDT"],
            "pair_blacklist": [],
        },
        "pairlists": [{"method": "StaticPairlist"}],
        "trading_mode": "spot",
        "dry_run": True,
        "dry_run_wallet": 10000,
        "stake_currency": "USDT",
        "stake_amount": 1000,
        "max_open_trades": 1,
        "timeframe": "5m",
        "strategy": "NoTradeSmoke",
        "api_server": {"enabled": False},
        "telegram": {"enabled": False},
        "fee": 0.001,
    }


def _write_tree(root):
    cfg = Path(root) / "configs"
    strat = Path(root) / "strategies" / "smoke"
    cfg.mkdir(parents=True, exist_ok=True)
    strat.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg / "smoke.json"
    cfg_file.write_text(json.dumps(_canonical_config()), encoding="utf-8")
    strat_file = strat / "NoTradeSmoke.py"
    strat_file.write_text("class NoTradeSmoke:\n    pass\n", encoding="utf-8")
    return cfg_file, strat_file


@contextlib.contextmanager
def _git_image(git="clean", image="present"):
    """Stdout realista a CUALQUIER consulta git/docker (sin imponer flags)."""
    def fake_run(argv, **kwargs):
        head = argv[0] if isinstance(argv, list) and argv else ""
        text = " ".join(argv) if isinstance(argv, list) else str(argv)
        if head == "git":
            if git == "missing":
                raise FileNotFoundError("git no disponible")
            if "status" in text:
                out = " M configs/smoke.json\n" if git == "dirty" else ""
                return mock.Mock(returncode=0, stdout=out, stderr="")
            return mock.Mock(returncode=0, stdout=COMMIT + "\n", stderr="")
        if head == "docker":
            if image == "missing":
                raise launch.subprocess.CalledProcessError(1, argv, "No such image")
            meta = json.dumps([{"Id": IMAGE_ID, "RepoDigests": [PINNED_IMAGE]}])
            return mock.Mock(returncode=0, stdout=meta, stderr="")
        raise AssertionError(f"consulta subprocess inesperada: {argv!r}")

    with mock.patch.object(launch.subprocess, "run", side_effect=fake_run) as m:
        yield m


def _fake_popen(returncode=0):
    proc = mock.MagicMock()
    proc.returncode = returncode
    proc.wait.return_value = returncode
    proc.__enter__.return_value = proc
    proc.__exit__.return_value = False
    return proc


def _fmt(ts):
    return f"{ts.strftime('%Y-%m-%d %H:%M:%S')},{ts.microsecond // 1000:03d}"


def _heartbeat(ts):
    return f"{_fmt(ts)} - freqtrade.worker - INFO - Bot heartbeat. PID=42 state=RUNNING\n"


def _other_line(ts):
    return f"{_fmt(ts)} - freqtrade.worker - INFO - Starting worker ...\n"


def _inspect(started_at, restarts=0, oom=False, running=True):
    state = {"Status": "running" if running else "exited", "Running": running,
             "OOMKilled": oom, "StartedAt": started_at.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return {"State": state, "RestartCount": restarts}


class LaunchCase(unittest.TestCase):
    def setUp(self):
        if launch is None:
            self.fail(f"RED: operations.launch no implementado ({_LAUNCH_ERR!r})")

    def test_canonical_accepted(self):
        launch.validate_config(_canonical_config(), {})

    def test_rejects_unsafe(self):
        base = _canonical_config()
        ex = base["exchange"]
        variants = {
            "dry_run false": {**base, "dry_run": False},
            "dry_run ambiguo": {**base, "dry_run": "true"},
            "futures": {**base, "trading_mode": "futures"},
            "par extra": {**base, "exchange": {**ex, "pair_whitelist": ["BTC/USDT", "ETH/USDT"]}},
            "par distinto": {**base, "exchange": {**ex, "pair_whitelist": ["ETH/USDT"]}},
            "exchange": {**base, "exchange": {**ex, "name": "kraken"}},
            "estrategia": {**base, "strategy": "MyStrategy"},
            "api": {**base, "api_server": {"enabled": True}},
            "telegram": {**base, "telegram": {"enabled": True}},
            "secret": {**base, "exchange": {**ex, "secret": "y"}},
        }
        for label, cfg in variants.items():
            with self.subTest(label=label), self.assertRaises(ValueError, msg=label):
                launch.validate_config(cfg, {})

    def test_rejects_aliases_overlaps_env(self):
        base = _canonical_config()
        ex = base["exchange"]
        for alias in ("key", "api_key", "secret", "api_secret", "password"):
            with self.subTest(alias=alias), self.assertRaises(ValueError, msg=alias):
                launch.validate_config({**base, "exchange": {**ex, alias: "x"}}, {})
        launch.validate_config({**base, "exchange": {**ex, "api_key": ""}}, {})
        for label, cfg in [
            ("add_config_files", {**base, "add_config_files": ["extra.json"]}),
            ("segunda config", {**base, "config": "other.json"}),
        ]:
            with self.subTest(label=label), self.assertRaises(ValueError, msg=label):
                launch.validate_config(cfg, {})
        for env in ({"FREQTRADE__DRY_RUN": "false"}, {"FREQTRADE__STRATEGY": "X"}):
            with self.subTest(env=env), self.assertRaises(ValueError, msg=str(env)):
                launch.validate_config(base, env)

    def test_build_pinned_shell_safe(self):
        with tempfile.TemporaryDirectory(prefix="code dir; rm ") as code, \
                tempfile.TemporaryDirectory(prefix="store $HOME && ") as store:
            argv = launch.build_command(code, store)
            self.assertIsInstance(argv, list)
            self.assertIn("NoTradeSmoke", argv)
            cfgs = [a for a in argv if a.endswith(".json")]
            self.assertEqual(len(cfgs), 1, argv)
            self.assertTrue(any(code in a for a in argv), argv)
            self.assertTrue(any(store in a for a in argv), argv)

    def test_file_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "in.bin"
            target.write_bytes(b"alpha")
            first = launch.file_hash(target)
            self.assertEqual(first, hashlib.sha256(b"alpha").hexdigest())
            target.write_bytes(b"beta")
            self.assertNotEqual(launch.file_hash(target), first)
            with self.assertRaises(FileNotFoundError):
                launch.file_hash(Path(tmp) / "missing.json")

    def test_prepare_ok_container_manifest(self):
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            cfg_path, strat_path = _write_tree(code)
            with _git_image():
                before = datetime.now(timezone.utc)
                out = Path(launch.prepare_smoke(code, store, PINNED_IMAGE))
                after = datetime.now(timezone.utc)
            manifest = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(manifest["config_hash"], launch.file_hash(cfg_path))
            self.assertEqual(manifest["strategy_hash"], launch.file_hash(strat_path))
            self.assertEqual(manifest["commit"], COMMIT)
            self.assertIn(PINNED_IMAGE, json.dumps(manifest))
            expected = manifest["expected_command"]
            joined = " ".join(expected)
            self.assertIn("NoTradeSmoke", expected)
            self.assertIn("/opt/btc-lab", joined)
            self.assertIn("/freqtrade/user_data", joined)
            self.assertNotIn(code, joined, "host debe normalizarse a contenedor")
            self.assertNotIn(store, joined, "host debe normalizarse a contenedor")
            ts = manifest.get("created_at") or manifest.get("timestamp")
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            self.assertLessEqual(before - timedelta(seconds=5), parsed)
            self.assertLessEqual(parsed, after + timedelta(seconds=5))
            self.assertTrue(manifest.get("run_id") or manifest.get("input_id"))

    def test_prepare_failures_no_success(self):
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            _write_tree(code)
            with _git_image(git="dirty"), self.assertRaises((ValueError, RuntimeError, SystemExit)):
                launch.prepare_smoke(code, store, PINNED_IMAGE)
            with _git_image(git="missing"), self.assertRaises(Exception):
                launch.prepare_smoke(code, store, PINNED_IMAGE)
            with _git_image(image="missing"), self.assertRaises(Exception):
                launch.prepare_smoke(code, store, PINNED_IMAGE)
            with _git_image(), self.assertRaises((ValueError, RuntimeError, SystemExit)):
                launch.prepare_smoke(code, store, "")
            with _git_image(), self.assertRaises((ValueError, RuntimeError, SystemExit, FileNotFoundError)):
                launch.prepare_smoke(str(Path(code) / "nope"), store, PINNED_IMAGE)
            for doc in Path(store).rglob("*.json"):
                try:
                    status = str(json.loads(doc.read_text(encoding="utf-8")).get("status", "")).upper()
                except Exception:
                    continue
                self.assertNotIn(status, ("SUCCEEDED", "SUCCESS", "OK"), doc)

    def test_run_failure_partial_never_success(self):
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            _write_tree(code)
            with _git_image():
                manifest_in = launch.prepare_smoke(code, store, PINNED_IMAGE)
            proc = _fake_popen(3)
            with mock.patch.object(launch.subprocess, "Popen", return_value=proc) as popen:
                rc = launch.run_smoke(code, store, manifest_in)
            self.assertEqual(rc, 3)
            _, kwargs = popen.call_args
            self.assertFalse(kwargs.get("shell", False))
            leaked = [k for k in (kwargs.get("env", {}) or {}) if k.startswith("FREQTRADE__")]
            self.assertEqual(leaked, [])
            runs = sorted((Path(store) / "runs").rglob("*.json"))
            payload = json.loads(runs[-1].read_text(encoding="utf-8"))
            self.assertEqual(payload["exit_code"], 3)
            self.assertEqual(str(payload["status"]).upper(), "FAILED")
            with mock.patch.object(launch.subprocess, "Popen", side_effect=OSError("boom")), \
                    self.assertRaises(Exception):
                launch.run_smoke(code, store, manifest_in)
            (Path(code) / "configs" / "smoke.json").write_bytes(b'{"tampered": true}')
            proc_ok = _fake_popen(0)
            with mock.patch.object(launch.subprocess, "Popen", return_value=proc_ok) as popen2, \
                    self.assertRaises((ValueError, RuntimeError, SystemExit)):
                launch.run_smoke(code, store, manifest_in)
            self.assertFalse(popen2.called, "hash cambiado frena antes del Popen")

    def test_run_never_overwrites(self):
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            _write_tree(code)
            with _git_image():
                first = launch.prepare_smoke(code, store, PINNED_IMAGE)
                with mock.patch.object(launch.subprocess, "Popen", return_value=_fake_popen(0)):
                    launch.run_smoke(code, store, first)
                second = launch.prepare_smoke(code, store, PINNED_IMAGE)
                with mock.patch.object(launch.subprocess, "Popen", return_value=_fake_popen(0)):
                    launch.run_smoke(code, store, second)
            runs = sorted((Path(store) / "runs").rglob("*.json"))
            self.assertGreaterEqual(len(runs), 2, runs)
            ids = [json.loads(p.read_text(encoding="utf-8")).get("run_id") or p.name for p in runs]
            self.assertEqual(len(set(ids)), len(ids))


class HealthCase(unittest.TestCase):
    def setUp(self):
        if health is None:
            self.fail(f"RED: operations.health no implementado ({_HEALTH_ERR!r})")

    def test_healthy(self):
        now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
        started = now - timedelta(seconds=300)
        res = health.evaluate_health(
            _inspect(started), _heartbeat(now - timedelta(seconds=30)), 50 * 1024**3, now)
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "healthy", res)
        self.assertIsInstance(res["reasons"], list)

    def test_not_healthy(self):
        now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
        started = now - timedelta(seconds=300)
        fresh = _heartbeat(now - timedelta(seconds=30))
        stale = _heartbeat(now - timedelta(seconds=600))
        recent_other = _other_line(now - timedelta(seconds=30))
        iso_line = f"{(now - timedelta(seconds=30)).isoformat()} engine heartbeat\n"
        restarted = now - timedelta(seconds=60)
        cases = {
            "detenido": (_inspect(started, running=False), fresh, 50 * 1024**3),
            "stale": (_inspect(started), stale, 50 * 1024**3),
            "no-heartbeat": (_inspect(started), "", 50 * 1024**3),
            "reciente-no-heartbeat": (_inspect(started), recent_other, 50 * 1024**3),
            "fecha-cualquiera-no-prueba": (_inspect(started), iso_line, 50 * 1024**3),
            "heartbeat-previo-a-StartedAt": (_inspect(restarted), _heartbeat(now - timedelta(seconds=120)), 50 * 1024**3),
            "oom": (_inspect(started, oom=True), fresh, 50 * 1024**3),
            "restarts": (_inspect(started, restarts=2), fresh, 50 * 1024**3),
            "disco-0": (_inspect(started), fresh, 0),
            "espacio-None": (_inspect(started), fresh, None),
            "inspect-None": (None, fresh, 50 * 1024**3),
            "malformado": (_inspect(started), "basura sin formato", 50 * 1024**3),
        }
        for label, (insp, log, free) in cases.items():
            with self.subTest(label=label):
                res = health.evaluate_health(insp, log, free, now)
                self.assertNotEqual(res["status"], "healthy", (label, res))
                self.assertTrue(res["reasons"], label)


if __name__ == "__main__":
    unittest.main()
