"""T02: launcher, manifiesto y health contra API real (T03 implementado).

validate_config: canonica = configs/smoke.json (pares en
  exchange.pair_whitelist, StaticPairList exacto, pricing/initial_state/
  internals fijados, api_server desactivado con jwt placeholder publico
  exacto, telegram con token/chat_id vacios). ValueError + rechazo
  FREQTRADE__* antes de subprocess.
prepare_smoke: Git limpio + docker image inspect reales (mocks
  realistas); expected_command de contenedor; image_ref mutable
  rechazado; cada prepare devuelve una ruta unica e inmutable (Compose monta
  la ruta elegida por LAB_SMOKE_INPUT como input explícito al hacer up).
run_smoke: CLI smoke sin --code-root/--storage-root e ignora
  LAB_CODE_ROOT/LAB_STORAGE_ROOT/LAB_STORAGE; main llama con
  (CONTAINER_CODE, CONTAINER_STORAGE). run_smoke RECHAZA roots que no
  resuelvan al contenedor antes de leer inputs. Popen argv ==
  container_command() exacto. exit 130 espontaneo o auto-SIGTERM del
  hijo (sin senal al padre) => FAILED, nunca CANCELLED falso; SIGTERM
  recibida por el launcher y reenviada al hijo => CANCELLED 130.
  Revalida hashes incl. launch_hash/health_hash (tamper bloquea Popen); Popen con
  env limpio; runs/ solo run-*.json, id unica, sin sobrescribir;
  parcial/ausente nunca es exito.
evaluate_health: solo heartbeat autentico, >= StartedAt, no futuro,
  180 s; inspect incompleto (OOM/Status/StartedAt/restarts), NaN,
  causan no-healthy con motivo.
Run: python -m unittest discover -s tests -v (raiz del worktree).
"""

import contextlib
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
from unittest import mock

from operations import health, launch

PINNED_IMAGE = (
    "freqtradeorg/freqtrade:2026.8@sha256:"
    "4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4"
)
COMMIT = "9f3a1c2d4e5b6a7890abcdef1234567890abcde1"
IMAGE_ID = "sha256:4d23160b501d2b34579e76f57ad75edfa274967cd0dd824ff1c1b86d8c166ab4"
JWT_PLACEHOLDER = "DISABLED-NO-API-SERVER-PLACEHOLDER-0000"
ROOT = Path(__file__).resolve().parents[1]


def _canonical_config():
    pricing = {"price_side": "same", "use_order_book": False,
               "order_book_top": 1, "price_last_balance": 0.0}
    return {
        "exchange": {"name": "binance", "key": "", "secret": "", "password": "",
                     "pair_whitelist": ["BTC/USDT"], "pair_blacklist": []},
        "pairlists": [{"method": "StaticPairList"}],
        "trading_mode": "spot",
        "dry_run": True,
        "dry_run_wallet": 10000,
        "stake_currency": "USDT",
        "stake_amount": 1000,
        "max_open_trades": 1,
        "timeframe": "5m",
        "strategy": "NoTradeSmoke",
        "entry_pricing": dict(pricing),
        "exit_pricing": dict(pricing),
        "initial_state": "running",
        "internals": {"heartbeat_interval": 60},
        "api_server": {"enabled": False, "listen_ip_address": "127.0.0.1",
                       "listen_port": 8080, "username": "", "password": "",
                       "jwt_secret_key": JWT_PLACEHOLDER},
        "telegram": {"enabled": False, "token": "", "chat_id": ""},
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
    """Simula las respuestas de las consultas Git/Docker del launcher."""
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


def _runs(store):
    return sorted((Path(store) / "runs").glob("run-*.json"))


@contextlib.contextmanager
def _roots(code, store):
    """Fija el contenedor a la fixture: argv y validacion, misma fuente."""
    with mock.patch.object(launch, "CONTAINER_CODE", str(code)), \
            mock.patch.object(launch, "CONTAINER_STORAGE", str(store)), \
            mock.patch.object(launch, "CONTAINER_USERDATA", str(Path(store) / "user_data")):
        yield


def _real_smoke(script):
    """Ejecuta run_smoke con un proceso controlado real (sin mock Popen)."""
    with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
        _write_tree(code)
        with _roots(code, store), \
                mock.patch.object(launch, "container_command", return_value=script), \
                _git_image():
            manifest_in = launch.prepare_smoke(code, store, PINNED_IMAGE)
            rc = launch.run_smoke(code, store, manifest_in)
            payload = json.loads(_runs(store)[-1].read_text(encoding="utf-8"))
    return rc, payload


class LaunchCase(unittest.TestCase):
    def test_canonical_accepted(self):
        launch.validate_config(_canonical_config(), {})
        real = json.loads((ROOT / "configs" / "smoke.json").read_text(encoding="utf-8"))
        launch.validate_config(real, {})

    def test_rejects_unsafe(self):
        base = _canonical_config()
        ex = base["exchange"]
        variants = {
            "dry_run false": {**base, "dry_run": False},
            "dry_run ambiguo": {**base, "dry_run": "true"},
            "futures": {**base, "trading_mode": "futures"},
            "par extra": {**base, "exchange": {**ex, "pair_whitelist": ["BTC/USDT", "ETH/USDT"]}},
            "par distinto": {**base, "exchange": {**ex, "pair_whitelist": ["ETH/USDT"]}},
            "par top-level invalido": {**base, "pair_whitelist": ["BTC/USDT"]},
            "pairlists typo": {**base, "pairlists": [{"method": "StaticPairlist"}]},
            "exchange": {**base, "exchange": {**ex, "name": "kraken"}},
            "estrategia": {**base, "strategy": "MyStrategy"},
            "api on": {**base, "api_server": {**base["api_server"], "enabled": True}},
            "jwt distinto": {**base, "api_server": {**base["api_server"], "jwt_secret_key": "x"}},
            "api user": {**base, "api_server": {**base["api_server"], "username": "u"}},
            "telegram on": {**base, "telegram": {"enabled": True, "token": "", "chat_id": ""}},
            "telegram token": {**base, "telegram": {"enabled": False, "token": "t", "chat_id": ""}},
            "pricing": {**base, "entry_pricing": {**base["entry_pricing"], "use_order_book": True}},
            "internals": {**base, "internals": {"heartbeat_interval": 30}},
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
                frozen = out.read_bytes()
                again = Path(launch.prepare_smoke(code, store, PINNED_IMAGE))
            self.assertNotEqual(out, again, "cada prepare devuelve ruta unica")
            self.assertEqual(out.read_bytes(), frozen, "el segundo prepare no toca el primero")
            manifest = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(manifest["config_hash"], launch.file_hash(cfg_path))
            self.assertEqual(manifest["strategy_hash"], launch.file_hash(strat_path))
            self.assertEqual(manifest["commit"], COMMIT)
            self.assertIn(PINNED_IMAGE, json.dumps(manifest))
            joined = " ".join(manifest["expected_command"])
            self.assertIn("NoTradeSmoke", manifest["expected_command"])
            self.assertIn("/opt/btc-lab", joined)
            self.assertIn("/freqtrade/user_data", joined)
            self.assertNotIn(code, joined, "host debe normalizarse a contenedor")
            self.assertNotIn(store, joined, "host debe normalizarse a contenedor")
            parsed = datetime.fromisoformat((manifest.get("created_at") or manifest["timestamp"]).replace("Z", "+00:00"))
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
            with _git_image(), self.assertRaises((ValueError, RuntimeError, SystemExit)):
                launch.prepare_smoke(code, store, "freqtradeorg/freqtrade:2026.8")
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
            with _roots(code, store), _git_image():
                manifest_in = launch.prepare_smoke(code, store, PINNED_IMAGE)
                proc = _fake_popen(3)
                with mock.patch.object(launch.subprocess, "Popen", return_value=proc) as popen:
                    self.assertEqual(launch.run_smoke(code, store, manifest_in), 3)
                self.assertEqual(popen.call_args[0][0], launch.container_command())
                _, kwargs = popen.call_args
                self.assertFalse(kwargs.get("shell", False))
                self.assertEqual([k for k in (kwargs.get("env", {}) or {}) if k.startswith("FREQTRADE__")], [])
                payload = json.loads(_runs(store)[-1].read_text(encoding="utf-8"))
                self.assertEqual(payload["exit_code"], 3)
                self.assertEqual(str(payload["status"]).upper(), "FAILED")
                with mock.patch.object(launch.subprocess, "Popen", side_effect=OSError("boom")), \
                        self.assertRaises(Exception):
                    launch.run_smoke(code, store, manifest_in)
                with mock.patch.object(launch, "_module_hash", return_value="0" * 64), \
                        mock.patch.object(launch.subprocess, "Popen", return_value=_fake_popen(0)) as popen3, \
                        self.assertRaises(ValueError):
                    launch.run_smoke(code, store, manifest_in)
                self.assertFalse(popen3.called, "launch/health alterados frenan antes del Popen")
                (Path(code) / "configs" / "smoke.json").write_bytes(b'{"tampered": true}')
                with mock.patch.object(launch.subprocess, "Popen", return_value=_fake_popen(0)) as popen2, \
                        self.assertRaises((ValueError, RuntimeError, SystemExit)):
                    launch.run_smoke(code, store, manifest_in)
                self.assertFalse(popen2.called, "config alterada frena antes del Popen")
                self.assertFalse([p for p in _runs(store)
                                  if str(json.loads(p.read_text(encoding="utf-8")).get("status", "")).upper() == "SUCCEEDED"])

    def test_run_never_overwrites_input_immutable(self):
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            _write_tree(code)
            with _roots(code, store), _git_image():
                first = launch.prepare_smoke(code, store, PINNED_IMAGE)
                frozen = Path(first).read_bytes()
                with mock.patch.object(launch.subprocess, "Popen", return_value=_fake_popen(0)):
                    launch.run_smoke(code, store, first)
                self.assertEqual(Path(first).read_bytes(), frozen, "input inmutable")
                second = launch.prepare_smoke(code, store, PINNED_IMAGE)
                self.assertNotEqual(Path(second), Path(first), "segundo prepare con ruta unica")
                self.assertEqual(Path(first).read_bytes(), frozen, "segundo prepare no toca el primero")
                with mock.patch.object(launch.subprocess, "Popen", return_value=_fake_popen(0)):
                    launch.run_smoke(code, store, second)
            runs = _runs(store)
            self.assertGreaterEqual(len(runs), 2, runs)
            ids = [json.loads(p.read_text(encoding="utf-8")).get("run_id") or p.name for p in runs]
            self.assertEqual(len(set(ids)), len(ids))

    def test_cli_smoke_sin_roots_ni_env(self):
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store, \
                tempfile.TemporaryDirectory() as other:
            _write_tree(code)
            _write_tree(other)
            with _git_image():
                manifest_real = launch.prepare_smoke(code, store, PINNED_IMAGE)
            with mock.patch.object(launch.subprocess, "Popen", return_value=_fake_popen(0)):
                with self.assertRaises(SystemExit):
                    launch.main(["smoke", "--code-root", other, "--storage-root", store,
                                 "--input", manifest_real])
            with _roots(code, store), _git_image():
                manifest_fx = launch.prepare_smoke(code, store, PINNED_IMAGE)
            bogus = {"LAB_CODE_ROOT": "/bogus/code", "LAB_STORAGE_ROOT": "/bogus/store",
                     "LAB_STORAGE": "/bogus/lab"}
            with _roots(code, store), mock.patch.dict(os.environ, bogus), \
                    mock.patch.object(launch.subprocess, "Popen", return_value=_fake_popen(0)) as popen:
                try:
                    rc = launch.main(["smoke", "--input", manifest_fx])
                except Exception as exc:
                    self.fail(f"RED: env redirige paths de smoke: {exc!r}")
                self.assertEqual(rc, 0)
                argv = popen.call_args[0][0]
                self.assertEqual(argv, launch.container_command())
                self.assertNotIn("/bogus", " ".join(argv))

    def test_run_roots_mismatch_falla_antes_popen(self):
        with tempfile.TemporaryDirectory() as code_a, tempfile.TemporaryDirectory() as code_b, \
                tempfile.TemporaryDirectory() as store_a, tempfile.TemporaryDirectory() as store_b:
            _write_tree(code_a)
            _write_tree(code_b)
            with _git_image():
                manifest_in = launch.prepare_smoke(code_a, store_a, PINNED_IMAGE)
            for label, c, s in [("code", code_b, store_a), ("storage", code_a, store_b)]:
                with self.subTest(label=label), \
                        mock.patch.object(launch.subprocess, "Popen", return_value=_fake_popen(0)) as popen, \
                        self.assertRaises((ValueError, RuntimeError, SystemExit), msg=label):
                    launch.run_smoke(c, s, manifest_in)
                self.assertFalse(popen.called, label)

    def test_run_roots_antes_de_leer_input(self):
        with mock.patch.object(Path, "read_text",
                               side_effect=AssertionError("no debe leer sin roots")):
            with self.assertRaises(ValueError, msg="roots invalidos primero"):
                launch.run_smoke("/invalid/code", "/invalid/store", "/invalid/input.json")

    def test_exit130_espontaneo_es_failed(self):
        rc, payload = _real_smoke([sys.executable, "-c", "import sys; sys.exit(130)"])
        self.assertEqual(rc, 130)
        self.assertEqual(payload["exit_code"], 130)
        self.assertEqual(str(payload["status"]).upper(), "FAILED")

    def test_child_self_sigterm_es_failed(self):
        script = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"]
        rc, payload = _real_smoke(script)
        self.assertEqual(rc, -signal.SIGTERM)
        self.assertEqual(payload["exit_code"], -signal.SIGTERM)
        self.assertEqual(str(payload["status"]).upper(), "FAILED")

    def test_parent_sigterm_forwarded_es_cancelled(self):
        proc = _fake_popen(130)
        proc.poll.return_value = None
        fired = []

        def _wait(timeout=None):
            if not fired:
                fired.append(True)
                os.kill(os.getpid(), signal.SIGTERM)
                deadline = time.monotonic() + 5.0
                while not proc.send_signal.called and time.monotonic() < deadline:
                    time.sleep(0.01)
            return 130

        proc.wait.side_effect = _wait
        with tempfile.TemporaryDirectory() as code, tempfile.TemporaryDirectory() as store:
            _write_tree(code)
            with _roots(code, store), _git_image():
                manifest_in = launch.prepare_smoke(code, store, PINNED_IMAGE)
                with mock.patch.object(launch.subprocess, "Popen", return_value=proc):
                    rc = launch.run_smoke(code, store, manifest_in)
                payload = json.loads(_runs(store)[-1].read_text(encoding="utf-8"))
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        self.assertEqual(rc, 130)
        self.assertEqual(payload["exit_code"], 130)
        self.assertEqual(str(payload["status"]).upper(), "CANCELLED")


class HealthCase(unittest.TestCase):
    def test_healthy(self):
        now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
        res = health.evaluate_health(
            _inspect(now - timedelta(seconds=300)),
            _heartbeat(now - timedelta(seconds=30)), 50 * 1024**3, now)
        self.assertEqual(res["status"], "healthy", res)
        self.assertIsInstance(res["reasons"], list)

    def test_not_healthy(self):
        now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
        started = now - timedelta(seconds=300)
        fresh = _heartbeat(now - timedelta(seconds=30))
        full = _inspect(started)["State"]
        no_oom = {"State": {k: v for k, v in full.items() if k != "OOMKilled"}, "RestartCount": 0}
        no_status = {"State": {k: v for k, v in full.items() if k != "Status"}, "RestartCount": 0}
        no_restarts = {"State": dict(full)}
        bad_started = {"State": {**full, "StartedAt": "nonsense"}, "RestartCount": 0}
        cases = {
            "detenido": (_inspect(started, running=False), fresh, 50 * 1024**3, "unhealthy"),
            "stale": (_inspect(started), _heartbeat(now - timedelta(seconds=600)), 50 * 1024**3, "unhealthy"),
            "futuro": (_inspect(started), _heartbeat(now + timedelta(seconds=60)), 50 * 1024**3, "unknown"),
            "no-heartbeat": (_inspect(started), "", 50 * 1024**3, "unknown"),
            "reciente-no-heartbeat": (_inspect(started), _other_line(now - timedelta(seconds=30)), 50 * 1024**3, "unknown"),
            "fecha-cualquiera-no-prueba": (_inspect(started), f"{(now - timedelta(seconds=30)).isoformat()} engine heartbeat\n", 50 * 1024**3, "unknown"),
            "heartbeat-previo-a-StartedAt": (_inspect(now - timedelta(seconds=60)), _heartbeat(now - timedelta(seconds=120)), 50 * 1024**3, "unhealthy"),
            "oom": (_inspect(started, oom=True), fresh, 50 * 1024**3, "unhealthy"),
            "oom-ausente": (no_oom, fresh, 50 * 1024**3, "unhealthy"),
            "status-ausente": (no_status, fresh, 50 * 1024**3, "unhealthy"),
            "started-ilegible": (bad_started, fresh, 50 * 1024**3, "unknown"),
            "restarts": (_inspect(started, restarts=2), fresh, 50 * 1024**3, "unhealthy"),
            "restarts-ausente": (no_restarts, fresh, 50 * 1024**3, "unknown"),
            "disco-0": (_inspect(started), fresh, 0, "unhealthy"),
            "espacio-nan": (_inspect(started), fresh, float("nan"), "unhealthy"),
            "espacio-None": (_inspect(started), fresh, None, "unknown"),
            "inspect-None": (None, fresh, 50 * 1024**3, "unknown"),
            "malformado": (_inspect(started), "basura sin formato", 50 * 1024**3, "unknown"),
        }
        for label, (insp, log, free, expected) in cases.items():
            with self.subTest(label=label):
                res = health.evaluate_health(insp, log, free, now)
                self.assertEqual(res["status"], expected, (label, res))
                self.assertTrue(res["reasons"], label)


if __name__ == "__main__":
    unittest.main()
