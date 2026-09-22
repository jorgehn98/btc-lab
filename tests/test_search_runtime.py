"""T05 ultimo chunk RED: runtime IO del runner search (borde nativo).

Fuente: operations/search.py REAL (screen/finalists/validation/test, sin
placeholders) + research/state + research/evaluation + grants de
operations.history/market.history. Solo tests en raiz; sin prod/commits/
agentes/datos reales ni campaign full. Mocks SOLO en el borde nativo
(`_run_native_batch`, que escribe ZIPs reales y devuelve rc); todo lo demas
es codigo real: argv/batches<=6, parse exacto, ledger/BH/agregacion,
records, selection pura, state/budget/artefactos reales en tmp.

Requiere pandas+freqtrade solo el screen completo (ledger sobre feathers
reales) y se omite en host sin esas libs; el resto corre en host stdlib.
Runtime de aceptacion: imagen fijada (ver AGENTS.md).

Run host: python3 -m unittest tests.test_search_runtime -v
Run imagen: docker compose --profile tools run --rm \
  --volume "$PWD/tests:/opt/btc-lab/tests:ro,z" \
  --entrypoint python engine -m unittest tests.test_search_runtime -v
"""

import contextlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operations import launch
from operations import search as S

ROOT = Path(__file__).resolve().parents[1]
PINNED_IMAGE = launch.PINNED_IMAGE
COMMIT = "ab" * 20
IMAGE_ID = "sha256:" + "ab" * 32

try:
    import pandas as pd  # noqa: F401

    HAS_PANDAS = True
except Exception:
    pd = None
    HAS_PANDAS = False

PAIR5 = S._pair_feather(S.PAIR, S.TIMEFRAME_5M)
PAIR1 = S._pair_feather(S.PAIR, S.TIMEFRAME_1H)
CODE_TARGETS = (
    S.SEARCH_REL, S.EVALUATION_REL, S.CAMPAIGN_REL, S.SELECTION_REL,
    S.STATE_REL, S.CANDIDATES_REL, S.CONTROL_REL, S.SEARCH_CONFIG_REL,
    S.EQUITY_REL, S.PARTITIONS_REL, S.TRAIN_HELPER_REL, S.HISTORY_REL,
    S.RESEARCH_REL, Path("operations/launch.py"), Path("operations/health.py"),
)


def _code_fixture(tmp):
    """Copia temporal de los ficheros hasheados (nunca toca el checkout real)."""
    code = Path(tmp) / "code"
    for rel in CODE_TARGETS:
        src = ROOT / rel
        dst = code / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    return code
WIN = {"year": 2019, "seg_dir": "seg00",
       "start": "2019-01-01T00:00:00+00:00",
       "end_exclusive": "2019-01-03T00:00:00+00:00"}


@contextlib.contextmanager
def _git_image():
    """Git limpio + imagen fijada (mismo patron que test_operations)."""
    def fake_run(argv, **kwargs):
        head = argv[0] if isinstance(argv, list) and argv else ""
        text = " ".join(argv) if isinstance(argv, list) else str(argv)
        if head == "git":
            if "status" in text:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout=COMMIT + "\n", stderr="")
        if head == "docker":
            meta = json.dumps([{"Id": IMAGE_ID, "RepoDigests": [PINNED_IMAGE]}])
            return mock.Mock(returncode=0, stdout=meta, stderr="")
        raise AssertionError(f"consulta subprocess inesperada: {argv!r}")

    with mock.patch.object(launch.subprocess, "run", side_effect=fake_run):
        yield


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _snap_manifest(snaps, dirname, seg_defs, whole5, whole1, role="train",
                   rs="2017-08-17T04:00:00+00:00", re_="2023-01-01T00:00:00+00:00"):
    """Manifiesto snapshot fabricado con hashes reales sobre bytes arbitrarios."""
    snap_dir = Path(snaps) / dirname
    files = {}
    for rel, payload in ((PAIR5, whole5), (PAIR1, whole1)):
        _write(snap_dir / rel, payload)
        files[rel] = launch.file_hash(snap_dir / rel)
    metas = []
    for meta in seg_defs:
        seg = snap_dir / meta["seg_dir"]
        mfiles = {}
        for rel, payload in ((PAIR5, meta["p5"]), (PAIR1, meta["p1"])):
            _write(seg / rel, payload)
            mfiles[rel] = launch.file_hash(seg / rel)
        metas.append({"index": meta["index"], "seg_dir": meta["seg_dir"],
                      "file_5m_sha256": mfiles[PAIR5],
                      "file_1h_sha256": mfiles[PAIR1],
                      "start": meta["start"], "end_exclusive": meta["end_exclusive"],
                      "eval_start": meta["eval_start"]})
    manifest = {"kind": "btc-lab-history-snapshot", "status": "FROZEN",
                "role": role, "range_start": rs, "range_end": re_,
                "snapshot_id": "snap-test", "snapshot_dir": dirname,
                "whole_5m_sha256": files[PAIR5], "whole_1h_sha256": files[PAIR1],
                "rows_5m": 2, "rows_1h": 1, "segments": len(metas),
                "segments_meta": metas}
    return manifest


def _live_snapshot_ref(manifest_path, manifest):
    """Ref EXACTA como la recomputa _verify_current_against_input."""
    return {"manifest": Path(manifest_path).name,
            "manifest_sha256": launch.file_hash(manifest_path),
            "snapshot_id": str(manifest["snapshot_id"]),
            "snapshot_dir": str(manifest["snapshot_dir"]),
            "whole_5m_sha256": str(manifest["whole_5m_sha256"]),
            "whole_1h_sha256": str(manifest["whole_1h_sha256"]),
            "range_start": str(manifest["range_start"]),
            "range_end": str(manifest["range_end"]),
            "rows_5m": int(manifest.get("rows_5m", 0)),
            "rows_1h": int(manifest.get("rows_1h", 0)),
            "segments": int(manifest.get("segments", 0))}


def _screen_env(root, seg_defs):
    """Arbol tmp search completo con bindings reales (input+estado+datos)."""
    from research.campaign import generate_variants
    from research.state import new_state

    search_root = Path(root) / "search"
    snaps = Path(root) / "snaps"
    manifest = _snap_manifest(snaps, "snap-train", seg_defs, b"WHOLE5M", b"WHOLE1H")
    manifest_path = snaps / "train-manifest.json"
    _write(manifest_path, json.dumps(manifest, sort_keys=True))
    variants = generate_variants()
    rendered = S._rendered_source_for(variants)
    gen_sha = S._sha256_bytes(rendered.encode("utf-8"))
    gen_dir = search_root / "generated"
    _write(gen_dir / S.GENERATED_FILENAME, rendered)
    cfg_hash = launch.file_hash(ROOT / "configs" / "search.json")
    ref = _live_snapshot_ref(manifest_path, manifest)
    definition = S.build_definition(ref, gen_sha, cfg_hash)
    definition_hash = S.definition_hash_for(definition)
    code_hashes = S._code_hashes(ROOT)
    manifest_in = {"kind": S.INPUT_KIND, "status": "PREPARED",
                   "input_id": "in-test-01", "commit": COMMIT,
                   "image_ref": PINNED_IMAGE, "image_id": "img-test",
                   "campaign_id": S.CAMPAIGN_ID, "definition_hash": definition_hash,
                   "definition": definition, "generated_source_sha256": gen_sha,
                   "config_hash": cfg_hash, **code_hashes}
    input_path = search_root / "input.json"
    _write(input_path, json.dumps(manifest_in, sort_keys=True))
    state = new_state(S.CAMPAIGN_ID, definition_hash)
    state_path = search_root / "control" / f"campaign-{S.CAMPAIGN_ID}.json"
    _write(state_path, json.dumps(state, sort_keys=True))
    snap_dir = snaps / "snap-train"
    return {"search": search_root, "input": input_path, "gen": gen_dir,
            "snapdir": snap_dir, "snapmanifest": manifest_path,
            "manifest_in": manifest_in, "state_path": state_path,
            "definition": definition, "definition_hash": definition_hash}


@contextlib.contextmanager
def _search_paths(env):
    with mock.patch.object(S, "CONTAINER_SEARCH", str(env["search"])), \
         mock.patch.object(S, "CONTAINER_CODE", str(ROOT)), \
         mock.patch.object(S, "CONTAINER_INPUT", str(env["input"])), \
         mock.patch.object(S, "CONTAINER_GENERATED", str(env["gen"])), \
         mock.patch.object(S, "CONTAINER_SNAP_TRAIN", str(env["snapdir"])), \
         mock.patch.object(S, "CONTAINER_SNAP_TRAIN_MANIFEST",
                            str(env["snapmanifest"])):
        yield


def _names_from_argv(argv):
    argv = list(argv)
    if "--strategy-list" in argv:
        return argv[argv.index("--strategy-list") + 1:argv.index("--strategy-path")]
    return [argv[argv.index("--strategy") + 1]]


class _NativeStub:
    """Borde nativo: escribe ZIPs reales, devuelve rc. Resto 100% real."""

    def __init__(self, mode="zero"):
        self.mode = mode
        self.calls = []

    def __call__(self, argv, timeout_s, log_path):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("stub\n", encoding="utf-8")
        export_dir = Path(argv[argv.index("--export-directory") + 1])
        names = _names_from_argv(argv)
        if self.mode == "exit1":
            return (1, False)
        if self.mode == "badzip":
            (export_dir / "r.zip").write_text("no es un zip", encoding="utf-8")
            return (0, False)
        body = {"strategy": {}}
        for n in names:
            if self.mode == "count":
                body["strategy"][n] = {"profit_total_abs": 1.0, "profit_total": 0.001,
                                       "total_trades": 2, "trades": [{"x": 1}]}
            elif self.mode == "notrades":
                body["strategy"][n] = {"profit_total_abs": 0.0, "profit_total": 0.0,
                                       "total_trades": 0}
            else:
                body["strategy"][n] = {"profit_total_abs": 0.0, "profit_total": 0.0,
                                       "total_trades": 0, "trades": []}
        with zipfile.ZipFile(str(export_dir / "r.zip"), "w") as bundle:
            bundle.writestr("backtest-result.json", json.dumps(body))
        return (0, False)


def _seg_one():
    return {"index": 0, "seg_dir": "seg00", "p5": b"SEG5M", "p1": b"SEG1H",
            "start": WIN["start"], "end_exclusive": WIN["end_exclusive"],
            "eval_start": WIN["start"]}


class ScreenNativeCase(unittest.TestCase):
    def test_screen_runs_native_batches_then_no_candidate_and_resumes_free(self):
        if not HAS_PANDAS:
            self.skipTest("screen completo exige pandas/imagen fijada")
        import pandas as pd

        from research.campaign import generate_variants

        with tempfile.TemporaryDirectory() as tmp:
            # Feathers reales primero: el manifiesto hashea los bytes vivos.
            seg_dir = Path(tmp) / "snaps" / "snap-train" / "seg00"
            seg_dir.mkdir(parents=True)
            idx5 = pd.date_range(start="2019-01-01", periods=576, freq="5min", tz="UTC")
            pd.DataFrame({"date": idx5, "open": 100.0, "high": 100.5,
                          "low": 99.5, "close": 100.0, "volume": 10.0}
                         ).to_feather(str(seg_dir / PAIR5))
            idx1 = pd.date_range(start="2019-01-01", periods=48, freq="1h", tz="UTC")
            pd.DataFrame({"date": idx1, "open": 100.0, "high": 100.5,
                          "low": 99.5, "close": 100.0, "volume": 10.0}
                         ).to_feather(str(seg_dir / PAIR1))
            seg = dict(_seg_one(), p5=(seg_dir / PAIR5).read_bytes(),
                       p1=(seg_dir / PAIR1).read_bytes())
            env = _screen_env(tmp, [seg])
            state_path = env["state_path"]
            stub = _NativeStub("zero")
            with _search_paths(env), \
                    mock.patch.object(S, "_run_native_batch", side_effect=stub):
                rc = S.cmd_screen()
            self.assertEqual(rc, 1, "cero real => NO_CANDIDATE terminal")
            # 1 ventana x 2 fees x (12 lotes de 6 + 1 control) = 26 llamadas.
            self.assertEqual(len(stub.calls), 26, "screen invoca nativo, no solo planea")
            listed = [len(_names_from_argv(a)) for a in stub.calls if "--strategy-list" in a]
            self.assertTrue(listed and max(listed) <= 6, "lotes <=6")
            singles = [a for a in stub.calls if "--strategy-list" not in a]
            self.assertTrue(singles, "control single con --strategy")
            reports = sorted((env["search"] / "sessions").glob("screen-*/report.json"))
            self.assertEqual(len(reports), 1)
            payload = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(payload.get("verdict"), "NO_CANDIDATE")
            # Sin holdout abierto ni consumo TEST.
            self.assertEqual(list((env["search"] / "sessions").glob("validation-*")), [])
            self.assertEqual(list((env["search"] / "sessions").glob("test-*")), [])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertGreater(state.get("consumed", 0), 0, "budget con cargo real")
            self.assertFalse(state.get("test_consumed"), "TEST no consumido")
            # Resume: 0 llamadas nuevas con mismo hash de exito.
            stub2 = _NativeStub("zero")
            with _search_paths(env), \
                    mock.patch.object(S, "_run_native_batch", side_effect=stub2):
                rc2 = S.cmd_screen()
            self.assertEqual(rc2, 1)
            self.assertEqual(len(stub2.calls), 0, "resume reusa exitos mismo hash")
            state2 = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state2.get("consumed"), state.get("consumed"))


class MatrixFailureCase(unittest.TestCase):
    def _matrix(self, tmp, stub, state=None):
        from research.state import new_state

        session = Path(tmp) / "sess"
        batch = session / "batches"
        session.mkdir(parents=True)
        batch.mkdir(parents=True)
        state = state if state is not None else new_state("camp-m", "def-m")
        state_path = Path(tmp) / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        from research.campaign import generate_variants

        names = [str(v["class_name"]) for v in generate_variants()[:6]]
        groups = [{"names": names, "use_list": True, "spath": str(Path(tmp) / "sp"),
                   "params": {}, "suffix": "t00"}]
        with mock.patch.object(S, "CONTAINER_SEARCH", str(Path(tmp))), \
                mock.patch.object(S, "_run_native_batch", side_effect=stub):
            results, stats = S._execute_native_matrix(
                phase="screen", role="train", windows=[dict(WIN)], fees=[0.002],
                groups=groups, snap_root=str(Path(tmp)), session_dir=session,
                batch_dir=batch, state=state, state_path=state_path,
                manifest_in={"image_id": "img", "generated_source_sha256": "gen",
                             "config_hash": "cfg"},
                code_hashes={"search_hash": "s", "evaluation_hash": "e",
                             "campaign_hash": "c", "selection_hash": "l",
                             "candidates_hash": "k", "control_hash": "o",
                             "equity_hash": "q"},
                seg_meta_by_dir={"seg00": {"file_5m_sha256": "a", "file_1h_sha256": "b"}},
                by_class_params={})
        return results, stats, state

    def test_failures_persist_failed_charge_once(self):
        for mode, cause_prefix in (("exit1", "exit:"), ("badzip", "parse:"),
                                   ("count", "count:")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                stub = _NativeStub(mode)
                _results, stats, state = self._matrix(tmp, stub)
                self.assertEqual(stats["failed"], 1, mode)
                self.assertEqual(stats["succeeded"], 0, mode)
                key = next(a["run_key"] for a in state["attempts"])
                matching = [a for a in state["attempts"] if a["run_key"] == key]
                self.assertEqual(len(matching), 1, f"{mode}: un solo cargo")
                self.assertEqual(matching[0]["status"], "FAILED", mode)
                self.assertEqual(state["consumed"], matching[0]["elapsed_seconds"],
                                 f"{mode}: budget carga lo real una vez")
                batch_files = list((Path(tmp) / "sess" / "batches").glob("*.json"))
                self.assertEqual(len(batch_files), 1, mode)
                payload = json.loads(batch_files[0].read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], "FAILED", mode)
                self.assertTrue(str(payload.get("cause", "")).startswith(cause_prefix),
                                f"{mode}: causa {payload.get('cause')}")

    def test_budget_exhausted_prevents_popen(self):
        from research.state import new_state

        for label, consumed in (("agotado", S.BUDGET_SECONDS),
                                ("sin resto para timeout", S.BUDGET_SECONDS - 100)):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                stub = _NativeStub("zero")
                state = new_state("camp-m", "def-m")
                state["consumed"] = consumed
                with self.assertRaises(ValueError, msg=label):
                    self._matrix(tmp, stub, state=state)
                self.assertEqual(len(stub.calls), 0, f"{label}: sin Popen")


class ExecutorRoleCase(unittest.TestCase):
    def test_val_role_uses_val_datadir_not_train(self):
        from research.campaign import generate_variants
        from research.state import new_state

        with tempfile.TemporaryDirectory() as tmp:
            val_root = Path(tmp) / "val-snap"
            (Path(tmp) / "train-snap").mkdir(parents=True)
            session = Path(tmp) / "sess"
            batch = session / "batches"
            session.mkdir(parents=True)
            batch.mkdir(parents=True)
            state = new_state("camp-v", "def-v")
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            names = [str(v["class_name"]) for v in generate_variants()[:2]]
            groups = [{"names": names, "use_list": True, "spath": str(Path(tmp) / "sp"),
                       "params": {}, "suffix": "v00"}]
            window = {"year": 2023, "seg_dir": "seg00",
                      "start": "2023-01-02T00:00:00+00:00",
                      "end_exclusive": "2023-01-04T00:00:00+00:00"}
            stub = _NativeStub("exit1")
            with mock.patch.object(S, "CONTAINER_SEARCH", str(Path(tmp))), \
                    mock.patch.object(S, "_run_native_batch", side_effect=stub):
                _results, stats = S._execute_native_matrix(
                    phase="validation", role="validation", windows=[window], fees=[0.002],
                    groups=groups, snap_root=str(val_root), session_dir=session,
                    batch_dir=batch, state=state, state_path=state_path,
                    manifest_in={"image_id": "img", "generated_source_sha256": "gen",
                                 "config_hash": "cfg"},
                    code_hashes={"search_hash": "s", "evaluation_hash": "e",
                                 "campaign_hash": "c", "selection_hash": "l",
                                 "candidates_hash": "k", "control_hash": "o",
                                 "equity_hash": "q"},
                    seg_meta_by_dir={"seg00": {"file_5m_sha256": "a", "file_1h_sha256": "b"}},
                    by_class_params={})
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(len(stub.calls), 1, "una ventana/fee/grupo => un Popen")
            argv = stub.calls[0]
            datadir = argv[argv.index("--datadir") + 1]
            self.assertEqual(datadir, str(val_root / "seg00"),
                             "argv usa el snap_root VAL de la ventana")
            self.assertNotIn("train", datadir, "nunca el datadir TRAIN en rol VAL")
            payload = json.loads(next(iter((batch).glob("*.json"))).read_text(encoding="utf-8"))
            self.assertIn("|validation|", payload["run_key"], "rol en la identidad")


class DefinitionBindingCase(unittest.TestCase):
    def test_definition_binds_base_implementation(self):
        from research.campaign import generate_variants

        with tempfile.TemporaryDirectory() as tmp:
            code = _code_fixture(tmp)
            ref = {"manifest": "m.json", "manifest_sha256": "a" * 64,
                   "snapshot_id": "s", "snapshot_dir": "d",
                   "whole_5m_sha256": "b" * 64, "whole_1h_sha256": "c" * 64,
                   "range_start": "2017-08-17T04:00:00+00:00",
                   "range_end": "2023-01-01T00:00:00+00:00"}
            rendered = S._rendered_source_for(generate_variants())
            gen_sha = S._sha256_bytes(rendered.encode("utf-8"))
            cfg_hash = launch.file_hash(code / "configs" / "search.json")
            hashes = S._code_hashes(code)
            definition = S.build_definition(ref, gen_sha, cfg_hash, hashes)
            self.assertEqual(len(definition.get("variants") or []), 72)
            base_hash = launch.file_hash(
                code / "strategies" / "search" / "SpotCandidates.py")
            self.assertEqual((definition.get("code_hashes") or {}).get("candidates_hash"),
                             base_hash, "definition ata el hash base real")
            self.assertIn(base_hash, json.dumps(definition, sort_keys=True),
                          "hash base inmutable dentro de la definicion")


class PrepareTamperCase(unittest.TestCase):
    def test_tampered_base_fails_state_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = _code_fixture(tmp)
            store = str(Path(tmp) / "store")
            snaps = Path(store) / "history" / "snapshots"
            manifest = _snap_manifest(snaps, "snap-prep",
                                      [{"index": 0, "seg_dir": "seg00", "p5": b"A",
                                        "p1": b"B", "start": WIN["start"],
                                        "end_exclusive": WIN["end_exclusive"],
                                        "eval_start": WIN["start"]}],
                                      b"W5", b"W1")
            base = "train-snap-prep.json"
            (snaps / base).write_text(json.dumps(manifest, sort_keys=True),
                                      encoding="utf-8")
            with _git_image():
                first = S.prepare_search(str(code), store, PINNED_IMAGE, base)
            self.assertTrue(Path(first).is_file())
            state_path = (Path(store) / "search" / "control"
                          / f"campaign-{S.CAMPAIGN_ID}.json")
            frozen = state_path.read_bytes()
            target = code / "strategies" / "search" / "SpotCandidates.py"
            from research.campaign import generate_variants as _gen

            rendered_before = S._sha256_bytes(
                S._rendered_source_for(_gen()).encode("utf-8"))
            target.write_bytes(target.read_bytes() + b"\n# probe-tamper\n")
            rendered_after = S._sha256_bytes(
                S._rendered_source_for(_gen()).encode("utf-8"))
            self.assertEqual(rendered_before, rendered_after,
                             "misma salida del renderer con base mutada")
            with _git_image(), self.assertRaises(
                    ValueError, msg="base mutada invalida definition/grants"):
                S.prepare_search(str(code), store, PINNED_IMAGE, base)
            self.assertEqual(state_path.read_bytes(), frozen,
                             "estado preparado intacto tras preflight fallido")


class StaleStateCase(unittest.TestCase):
    def test_stale_prelock_state_not_overwritten(self):
        import contextlib

        with tempfile.TemporaryDirectory() as tmp:
            env = _screen_env(tmp, [_seg_one()])
            real_lock = S._container_locked
            state_path = env["state_path"]

            @contextlib.contextmanager
            def _poisoned_lock():
                with real_lock():
                    concurrent = json.loads(state_path.read_text(encoding="utf-8"))
                    concurrent["consumed"] = S.BUDGET_SECONDS
                    state_path.write_text(json.dumps(concurrent), encoding="utf-8")
                    yield

            stub = _NativeStub("exit1")
            with _search_paths(env), \
                    mock.patch.object(S, "_container_locked", _poisoned_lock), \
                    mock.patch.object(S, "_run_native_batch", side_effect=stub):
                S.cmd_screen()
            self.assertEqual(len(stub.calls), 0,
                             "estado recargado bajo lock frena antes de Popen")
            live = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(live.get("consumed"), S.BUDGET_SECONDS,
                             "sin overwrite del consumo concurrente")


class HoldoutGateCase(unittest.TestCase):
    def test_bare_authorize_cannot_unlock_history(self):
        from market.history import authorize_partition
        from operations.history import verify_holdout_grant

        bare = authorize_partition("test", {"candidate": "V000", "consumed": False})
        with self.assertRaises(ValueError, msg="dict puro no abre holdout"):
            verify_holdout_grant("test", dict(bare), "defhash", ["V000"], "rep-sha")

    def test_role_metadata_and_mounts_reject_before_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            snaps = Path(tmp) / "snaps"
            manifest = _snap_manifest(snaps, "snap-train", [_seg_one()], b"W5", b"W1")
            with self.subTest(check="rol erroneo"):
                with self.assertRaises(ValueError):
                    S._verify_snapshot_scope(dict(manifest), "validation")
            with self.subTest(check="hash ausente"):
                broken = dict(manifest)
                broken.pop("whole_5m_sha256")
                with self.assertRaises(ValueError):
                    S._verify_snapshot_scope(broken, "train")
            with self.subTest(check="fuera de raiz"):
                evil = Path(tmp) / "evil"
                (evil / "seg00").mkdir(parents=True)
                for rel, payload in ((PAIR5, b"E5"), (PAIR1, b"E1"),
                                     ("seg00/" + PAIR5, b"ES5"),
                                     ("seg00/" + PAIR1, b"ES1")):
                    _write(evil / rel, payload)
                trav = dict(manifest, snapshot_dir="../evil",
                            whole_5m_sha256=launch.file_hash(evil / PAIR5),
                            whole_1h_sha256=launch.file_hash(evil / PAIR1))
                trav["segments_meta"] = [dict(manifest["segments_meta"][0],
                                              file_5m_sha256=launch.file_hash(evil / "seg00" / PAIR5),
                                              file_1h_sha256=launch.file_hash(evil / "seg00" / PAIR1))]
                with self.assertRaises(ValueError, msg="archivos fuera de raiz rechazan"):
                    S._verify_snapshot_data_files(snaps, trav)


class ParseAggregateCase(unittest.TestCase):
    def test_parse_rejects_missing_trades_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = {"strategy": {"SpotCandidateV000": {
                "profit_total_abs": 0.0, "profit_total": 0.0, "total_trades": 0}}}
            zpath = Path(tmp) / "r.zip"
            with zipfile.ZipFile(str(zpath), "w") as bundle:
                bundle.writestr("backtest-result.json", json.dumps(body))
            from research.evaluation import parse_native_batch

            parsed, err = parse_native_batch(zpath, ["SpotCandidateV000"])
            self.assertIsNone(parsed, "lista trades ausente rechaza")
            self.assertIsNotNone(err)

    def test_parse_accepts_complete_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = {"strategy": {"SpotCandidateV000": {
                "profit_total_abs": 0.0, "profit_total": 0.0, "total_trades": 0,
                "trades": []}}}
            zpath = Path(tmp) / "r.zip"
            with zipfile.ZipFile(str(zpath), "w") as bundle:
                bundle.writestr("backtest-result.json", json.dumps(body))
            from research.evaluation import parse_native_batch

            parsed, err = parse_native_batch(zpath, ["SpotCandidateV000"])
            self.assertIsNone(err, f"lote completo parsea: {err}")
            self.assertIn("SpotCandidateV000", parsed)

    def test_aggregation_missing_window_is_invalid_not_dropped(self):
        from research.evaluation import aggregate_year_record

        rec = aggregate_year_record("V000", "train", 2019, 0.002,
                                    [{"days": 10.0}], expected_windows=2)
        self.assertFalse(rec["valid"], "ventana faltante => valid False")
        for field in ("variant_id", "role", "year", "fee", "valid", "days", "g",
                      "bh_g", "max_drawdown_pct", "trades_nonforced", "turnover",
                      "g_without_positive_forced"):
            self.assertIn(field, rec, f"registro conserva {field}")


if __name__ == "__main__":
    unittest.main()
