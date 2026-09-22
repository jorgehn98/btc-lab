"""T05 PR4 blockers: 8 regression tests antes del fix (solo tests).

Fuente: revision PR4 sobre operations/search.py REAL + research/state +
research/evaluation + grants operations.history/market.history. Sin prod,
commits, agentes, datos reales ni backtests completos. Mocks SOLO en el
borde nativo (`_run_native_batch` escribe ZIPs reales); el resto es codigo
real con fixtures sinteticas en tmp (0 ficheros de holdout reales leidos).

Casos:
1. cmd_test con economia PASS y tecnica falsa/ausente => NONPASS y CERO
   bundles (el bool tecnico es exacto `is True`).
2. La promocion usa la referencia canonica path+SHA persistida en la
   transicion, nunca glob-last-positive: reporte editado a otra VAL valida
   => denegado ANTES de reserve/abrir TEST; un NO_CANDIDATE nuevo no
   rescata un positivo antiguo. Requiere helper canonico propuesto
   `resolve_phase_report` (el implementador lo cablea; sin el, RED que
   muestra el hueco glob).
3. Grants VAL/TEST solo valen registrados en la autoridad canonica
   (`phase_reports{phase:{path,sha256,status,verdict}}`,
   `grants{role:grant}`, `test_grant`/`test_consumed`; runtime lee RO
   `/lab-search-authority/control/campaign-<id>.json`, constante
   `AUTHORITY_ROOT` parcheable). Grant valido en formato SIN registro,
   consumed False y `market.authorize` puro deben rechazar; mismo grant
   registrado con consumed True permite resume. Requiere helpers
   propuestos `load_authority`/`authorize_holdout` (sin ellos, RED que
   muestra que el grant auto-afirmado pasa forma).
4. ZIP/payload de cache modificado tras el primer exito => no reuse
   SUCCEEDED (verificar SHA del ZIP contra el digest persistido, no solo
   el cache auto-reportado; reparsear nombres, count/ledger).
5. Error de ledger del control o resultado de control ausente => FAILED,
   no short+continue silencioso.
6. Nativo completa pero parse/open del ZIP lanza OSError => UN solo
   intento FAILED con cargo (sin computo gratis); 3 misma causa paran.
7. Fallo de escritura del paper bundle con economia PASS => FAILED
   operacional persistido, sin artefacto PASS listo; `paper_ready` False.
8. El wrapper del control importa la base real: su hash entra en la
   definicion; mutar solo la base invalida input/cache.

Run host (stdlib): python3 -m unittest tests.test_search_regression -v
Run imagen (casos 1 y 7 exigen pandas/ledger):
  docker compose --profile tools run --rm \
    --volume "$PWD/tests:/opt/btc-lab/tests:ro,z" \
    --entrypoint python engine -m unittest tests.test_search_regression -v
"""

import contextlib
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operations import launch
from operations import search as S
from tests.test_search_runtime import (
    _git_image,
    _NativeStub,
    _snap_manifest,
    _write,
    CODE_TARGETS,
    COMMIT,
    HAS_PANDAS,
    IMAGE_ID,
    PAIR1,
    PAIR5,
)

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
_MISSING = object()


def _names_from_argv(argv):
    argv = [str(a) for a in argv]
    if "--strategy-list" in argv:
        return argv[argv.index("--strategy-list") + 1:argv.index("--strategy-path")]
    return [argv[argv.index("--strategy") + 1]]


class _TestWinStub:
    """Nativo simulado para cmd_test: fills reales + profit por fee del argv."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout_s, log_path):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("stub\n", encoding="utf-8")
        export_dir = Path(argv[argv.index("--export-directory") + 1])
        fee = float(argv[argv.index("--fee") + 1])
        start = datetime.strptime(argv[argv.index("--timerange") + 1].split("-")[0],
                                  "%Y%m%dT%H%M").replace(tzinfo=UTC)
        body = {"strategy": {}}
        for name in _names_from_argv(argv):
            if name == S.CONTROL_STRATEGY:
                body["strategy"][name] = {"profit_total_abs": 0.0,
                                          "profit_total": 0.0,
                                          "total_trades": 0, "trades": []}
                continue
            trades = []
            for i in range(30):
                o = start + timedelta(minutes=5 * (2 * i))
                c = o + timedelta(minutes=5)
                trades.append({"amount": 1.0, "open_rate": 90.0,
                               "close_rate": 100.0, "fee_open": fee,
                               "fee_close": fee, "open_date": o.isoformat(),
                               "close_date": c.isoformat(),
                               "exit_reason": "exit_signal"})
            body["strategy"][name] = {"profit_total_abs": 30.0 * (10.0 - 190.0 * fee),
                                      "profit_total": 0.001,
                                      "total_trades": 30, "trades": trades}
        with zipfile.ZipFile(str(export_dir / "r.zip"), "w") as bundle:
            bundle.writestr("backtest-result.json", json.dumps(body))
        return (0, False)


def _test_env(root, bias_value=_MISSING):
    """Entorno TEST completo con economia PASS (falsos library dixits)."""
    from market.history import partition_bounds
    from research.campaign import generate_variants
    from research.state import (
        freeze_train_selection,
        freeze_validation_selection,
        new_state,
        reserve_test,
    )

    variants = generate_variants()
    by_id = {str(v["id"]): v for v in variants}
    train_ids = [str(v["id"]) for v in variants[:9]]
    val_ids = [train_ids[0], train_ids[3], train_ids[6]]
    candidate = val_ids[0]
    search_root = Path(root) / "search"
    snaps = Path(root) / "snaps"
    tmanifest = _snap_manifest(
        snaps, "snap-train",
        [{"index": 0, "seg_dir": "seg00", "p5": b"T5", "p1": b"T1",
          "start": "2017-08-17T04:00:00+00:00",
          "end_exclusive": "2017-08-18T00:00:00+00:00",
          "eval_start": "2017-08-17T04:00:00+00:00"}],
        b"TW5", b"TW1")
    tmanifest_path = snaps / "train-manifest.json"
    _write(tmanifest_path, json.dumps(tmanifest, sort_keys=True))
    tstart, tend = partition_bounds("test")
    seg_defs = []
    for idx, year in ((0, 2025), (1, 2026)):
        start = f"{year}-01-01T00:00:00+00:00"
        end = f"{year}-01-03T00:00:00+00:00"
        if HAS_PANDAS:
            import pandas as pd

            seg = snaps / "snap-test" / f"seg{idx:02d}"
            seg.mkdir(parents=True)
            idx5 = pd.date_range(start=start, periods=576, freq="5min", tz="UTC")
            pd.DataFrame({"date": idx5, "open": 100.0, "high": 100.5,
                          "low": 99.5, "close": 100.0, "volume": 10.0}
                         ).to_feather(str(seg / PAIR5))
            idx1 = pd.date_range(start=start, periods=48, freq="1h", tz="UTC")
            pd.DataFrame({"date": idx1, "open": 100.0, "high": 100.5,
                          "low": 99.5, "close": 100.0, "volume": 10.0}
                         ).to_feather(str(seg / PAIR1))
            p5 = (seg / PAIR5).read_bytes()
            p1 = (seg / PAIR1).read_bytes()
        else:
            p5, p1 = b"V5", b"V1"
        seg_defs.append({"index": idx, "seg_dir": f"seg{idx:02d}", "p5": p5,
                         "p1": p1, "start": start, "end_exclusive": end,
                         "eval_start": start})
    vmanifest = _snap_manifest(snaps, "snap-test", seg_defs, b"VW5", b"VW1",
                               role="test", rs=tstart.isoformat(),
                               re_=tend.isoformat())
    vmanifest_path = snaps / "test-manifest.json"
    _write(vmanifest_path, json.dumps(vmanifest, sort_keys=True))
    rendered = S._rendered_source_for(variants)
    gen_sha = S._sha256_bytes(rendered.encode("utf-8"))
    cfg_hash = launch.file_hash(ROOT / "configs" / "search.json")
    code_hashes = S._code_hashes(ROOT)
    ref = {"manifest": tmanifest_path.name,
           "manifest_sha256": launch.file_hash(tmanifest_path),
           "snapshot_id": str(tmanifest["snapshot_id"]),
           "snapshot_dir": str(tmanifest["snapshot_dir"]),
           "whole_5m_sha256": str(tmanifest["whole_5m_sha256"]),
           "whole_1h_sha256": str(tmanifest["whole_1h_sha256"]),
           "range_start": str(tmanifest["range_start"]),
           "range_end": str(tmanifest["range_end"]),
           "rows_5m": int(tmanifest.get("rows_5m", 0)),
           "rows_1h": int(tmanifest.get("rows_1h", 0)),
           "segments": int(tmanifest.get("segments", 0))}
    definition = S.build_definition(ref, gen_sha, cfg_hash, code_hashes)
    definition_hash = S.definition_hash_for(definition)
    gen_dir = search_root / "generated"
    _write(gen_dir / S.GENERATED_FILENAME, rendered)
    manifest_in = {"kind": S.INPUT_KIND, "status": "PREPARED",
                   "input_id": "in-test-01", "commit": COMMIT,
                   "image_ref": launch.PINNED_IMAGE, "image_id": "img-test",
                   "campaign_id": S.CAMPAIGN_ID, "definition_hash": definition_hash,
                   "definition": definition, "generated_source_sha256": gen_sha,
                   "config_hash": cfg_hash, **code_hashes}
    input_path = search_root / "input.json"
    _write(input_path, json.dumps(manifest_in, sort_keys=True))
    state = new_state(S.CAMPAIGN_ID, definition_hash)
    freeze_train_selection(state, train_ids)
    freeze_validation_selection(state, val_ids)
    grant0 = reserve_test(state, candidate)
    state_path = search_root / "control" / f"campaign-{S.CAMPAIGN_ID}.json"
    bias = {} if bias_value is _MISSING else {candidate: bias_value}
    frep_dir = search_root / "sessions" / "finalists-r1"
    frep = {"status": "SUCCEEDED", "definition_hash": definition_hash,
            "created_at": "2026-01-01T00:00:00Z", "verdict": "TOP3",
            "validation_ids": sorted(val_ids), "bias_verdicts": bias}
    _write(frep_dir / "report.json", json.dumps(frep, sort_keys=True))
    vrep_dir = search_root / "sessions" / "validation-r1"
    vrep = {"status": "SUCCEEDED", "definition_hash": definition_hash,
            "created_at": "2026-02-01T00:00:00Z", "verdict": "CANDIDATE",
            "chosen_test_candidate": candidate}
    _write(vrep_dir / "report.json", json.dumps(vrep, sort_keys=True))
    # Refs canonicas REALES (path+SHA de los artefactos) antes de cmd_test.
    state["phase_reports"] = {
        "finalists": {"path": "sessions/finalists-r1/report.json",
                      "sha256": launch.file_hash(frep_dir / "report.json"),
                      "status": "SUCCEEDED", "verdict": "TOP3"},
        "validation": {"path": "sessions/validation-r1/report.json",
                       "sha256": launch.file_hash(vrep_dir / "report.json"),
                       "status": "SUCCEEDED", "verdict": "CANDIDATE"},
    }
    _write(state_path, json.dumps(state, sort_keys=True))
    vrep_sha = launch.file_hash(vrep_dir / "report.json")
    grant = {"campaign_id": S.CAMPAIGN_ID, "candidate_id": candidate,
             "grant_id": grant0["grant_id"], "definition_hash": definition_hash,
             "phase": "test", "validation_report_sha256": vrep_sha}
    _write(search_root / "control" / f"test-grant-{grant['grant_id'][:8]}.json",
           json.dumps(grant, sort_keys=True))
    binding = {"kind": S.SNAP_BIND_KIND, "grant_id": grant["grant_id"],
               "definition_hash": definition_hash, "expected_ids": [candidate],
               "validation_report_sha256": vrep_sha,
               "manifest": vmanifest_path.name,
               "manifest_sha256": launch.file_hash(vmanifest_path),
               "snapshot_id": str(vmanifest["snapshot_id"]),
               "snapshot_dir": str(vmanifest["snapshot_dir"]),
               "whole_5m_sha256": str(vmanifest["whole_5m_sha256"]),
               "whole_1h_sha256": str(vmanifest["whole_1h_sha256"]),
               "range_start": str(vmanifest["range_start"]),
               "range_end": str(vmanifest["range_end"])}
    _write(search_root / "control" / f"test-snapshot-{grant['grant_id'][:8]}.json",
           json.dumps(binding, sort_keys=True))
    return {"search": search_root, "input": input_path, "gen": gen_dir,
            "snapdir": snaps / "snap-train", "snapmanifest": tmanifest_path,
            "valdir": snaps / "snap-test", "valmanifest": vmanifest_path,
            "candidate": candidate, "by_id": by_id, "state_path": state_path,
            "grant": grant, "definition_hash": definition_hash,
            "val_ids": val_ids}


@contextlib.contextmanager
def _test_paths(env):
    with mock.patch.object(S, "CONTAINER_SEARCH", str(env["search"])), \
         mock.patch.object(S, "CONTAINER_CODE", str(ROOT)), \
         mock.patch.object(S, "CONTAINER_INPUT", str(env["input"])), \
         mock.patch.object(S, "CONTAINER_GENERATED", str(env["gen"])), \
         mock.patch.object(S, "CONTAINER_SNAP_TRAIN", str(env["snapdir"])), \
         mock.patch.object(S, "CONTAINER_SNAP_TRAIN_MANIFEST",
                            str(env["snapmanifest"])), \
         mock.patch.object(S, "CONTAINER_SNAP_TEST", str(env["valdir"])), \
         mock.patch.object(S, "CONTAINER_SNAP_TEST_MANIFEST",
                            str(env["valmanifest"])):
        yield


def _latest_test_report(env):
    reports = sorted((env["search"] / "sessions").glob("test-*/report.json"))
    assert len(reports) == 1, f"una sesion test, fue {reports}"
    return json.loads(reports[0].read_text(encoding="utf-8"))


def _require_pandas(test):
    if not HAS_PANDAS:
        test.skipTest("cmd_test exige pandas/imagen fijada (ledger)")


def _require_pandas(test):
    if not HAS_PANDAS:
        test.skipTest("cmd_test exige pandas/imagen fijada (ledger)")


class TechnicalGateCase(unittest.TestCase):
    def test_economics_pass_without_technical_is_nonpass_no_bundle(self):
        _require_pandas(self)
        for label, bias in (("control-True", True), ("False", False),
                            ("ausente", _MISSING), ("truthy-1", 1)):
            with self.subTest(bias=label), tempfile.TemporaryDirectory() as tmp:
                env = _test_env(tmp, bias)
                stub = _TestWinStub()
                with _test_paths(env), \
                        mock.patch.object(S, "_run_native_batch", side_effect=stub):
                    rc = S.cmd_test()
                payload = _latest_test_report(env)
                bundles = list((env["search"] / "sessions").glob("test-*/paper-bundle.json"))
                bundles += list((env["search"] / "control").glob("paper-bundle-*.json"))
                if label == "control-True":
                    self.assertEqual(rc, 0, "tecnica exacta True + economia PASS activa")
                    self.assertEqual(payload.get("verdict"), "PASS")
                    self.assertTrue(bundles, "bundle sellado en PASS")
                    calls = len(stub.calls)
                    with _test_paths(env), mock.patch.object(
                            S, "_run_native_batch", side_effect=stub):
                        self.assertEqual(S.cmd_test(), 0, "PASS publicado reanuda")
                    self.assertEqual(len(stub.calls), calls, "resume no recalcula")
                    state = json.loads(env["state_path"].read_text(encoding="utf-8"))
                    state["paper_ready"] = False
                    _write(env["state_path"], json.dumps(state, sort_keys=True))
                    with _test_paths(env), mock.patch.object(
                            S, "_run_native_batch", side_effect=stub):
                        self.assertEqual(S.cmd_test(), 1,
                                         "PASS sin publicacion no reanuda como exito")
                    continue
                self.assertEqual(rc, 1, f"{label}: NONPASS terminal")
                self.assertNotEqual(payload.get("verdict"), "PASS",
                                    f"{label}: tecnica no exacta no activa")
                self.assertEqual(bundles, [], f"{label}: CERO bundles")


class CanonicalRefCase(unittest.TestCase):
    def _reports(self, tmp, chosen_old="V000", chosen_new="V001"):
        from research.campaign import generate_variants

        ids = [str(v["id"]) for v in generate_variants()]
        a, b = chosen_old, chosen_new
        assert a in ids and b in ids and a != b
        sessions = Path(tmp) / "sessions"
        old = {"status": "SUCCEEDED", "definition_hash": "dh",
               "created_at": "2026-01-01T00:00:00Z", "verdict": "CANDIDATE",
               "chosen_test_candidate": a}
        new = {"status": "SUCCEEDED", "definition_hash": "dh",
               "created_at": "2026-02-01T00:00:00Z", "verdict": "NO_CANDIDATE"}
        _write(sessions / "validation-111" / "report.json",
               json.dumps(old, sort_keys=True))
        _write(sessions / "validation-222" / "report.json",
               json.dumps(new, sort_keys=True))
        state = {"definition_hash": "dh", "validation_ids": [a, b]}
        return state

    def test_promotion_uses_persisted_ref_not_glob(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._reports(tmp)
            # Hueco vigente: el glob devuelve la positiva antigua aunque la
            # fase finalizo NO_CANDIDATE (sin fallback permitido).
            old = S._load_validation_report_verified(state, search_root=tmp)
            self.assertEqual(old[0].get("chosen_test_candidate"), "V000",
                             "hueco glob: rescata positiva superada")
            resolve = getattr(S, "resolve_phase_report", None)
            self.assertIsNotNone(
                resolve,
                "RED: falta resolucion canonica path+SHA persistida en la "
                f"transicion (glob acepta {old[0].get('chosen_test_candidate')!r} "
                "pese a NO_CANDIDATE final; la promocion no puede denegar)")
            # Referencia canonica al reporte editado => SHA difiere => deniega.
            r1 = Path(tmp) / "sessions" / "validation-111" / "report.json"
            sha_r1 = launch.file_hash(r1)
            tampered = json.loads(r1.read_text(encoding="utf-8"))
            tampered["chosen_test_candidate"] = "V001"
            r1.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")
            ref_state = dict(state, phase_reports={
                "validation": {"path": str(r1), "sha256": sha_r1,
                               "status": "SUCCEEDED", "verdict": "CANDIDATE"}})
            with self.assertRaises(ValueError, msg="editado deniega antes de reserve"):
                resolve(ref_state, "validation", search_root=tmp)
            # Referencia al NO_CANDIDATE nuevo => lo devuelve, sin fallback.
            r2 = Path(tmp) / "sessions" / "validation-222" / "report.json"
            ref_state2 = dict(state, phase_reports={
                "validation": {"path": str(r2),
                               "sha256": launch.file_hash(r2),
                               "status": "SUCCEEDED", "verdict": "NO_CANDIDATE"}})
            payload, _sha = resolve(ref_state2, "validation", search_root=tmp)
            self.assertEqual(payload.get("verdict"), "NO_CANDIDATE")
            self.assertNotIn("chosen_test_candidate", payload,
                             "sin fallback a positiva antigua")


class AuthorityCase(unittest.TestCase):
    def _grant_ok(self, defhash, ids):
        return {"campaign_id": S.CAMPAIGN_ID, "grant_id": "ab" * 16,
                "definition_hash": defhash, "phase": "validation",
                "validation_ids": list(ids), "train_report_sha256": "tr" * 32}

    def test_unregistered_grant_and_consumed_rules(self):
        from market.history import verify_phase_grant

        defhash, ids = "dh", ["V000", "V001"]
        grant = self._grant_ok(defhash, ids)
        # Hueco vigente: el grant auto-afirmado pasa la forma sin autoridad.
        checked = verify_phase_grant("validation", dict(grant), defhash, ids)
        load_auth = getattr(S, "load_authority", None)
        authz = getattr(S, "authorize_holdout", None)
        self.assertIsNotNone(
            load_auth,
            f"RED: sin autoridad canonica RO; hoy grant auto-afirmado pasa "
            f"forma: {checked}")
        self.assertIsNotNone(
            authz,
            "RED: sin authorize_holdout contra grants registrados en estado")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "authority" / "control"
            root.mkdir(parents=True)
            auth_path = root / f"campaign-{S.CAMPAIGN_ID}.json"
            base = {"campaign_id": S.CAMPAIGN_ID, "definition_hash": defhash,
                    "phase_reports": {}, "grants": {}, "test_grant": None,
                    "test_consumed": False}
            _write(auth_path, json.dumps(base, sort_keys=True))
            with mock.patch.object(S, "AUTHORITY_ROOT", str(Path(tmp) / "authority"),
                                   create=True):
                authority = load_auth()
            self.assertEqual(authority.get("definition_hash"), defhash)
            with self.subTest(regla="no registrado rechaza"):
                with self.assertRaises(ValueError):
                    authz(authority, "validation", dict(grant))
            with self.subTest(regla="grant completo sin phase_report niega"):
                nochain = dict(base, validation_ids=ids,
                               grants={"validation": dict(grant)})
                with self.assertRaises(ValueError, msg="cadena source obligatoria"):
                    authz(nochain, "validation", dict(grant))
            with self.subTest(regla="veredicto NO_CANDIDATE en cadena niega"):
                badverdict = dict(base, validation_ids=ids,
                                  grants={"validation": dict(grant)},
                                  phase_reports={"finalists": {
                                      "sha256": "tr" * 32, "status": "SUCCEEDED",
                                      "verdict": "NO_CANDIDATE"}})
                with self.assertRaises(ValueError, msg="cadena exige TOP3/CANDIDATE"):
                    authz(badverdict, "validation", dict(grant))
            with self.subTest(regla="consumed False rechaza TEST"):
                closed = dict(base, grants={"test": dict(
                    grant, phase="test", candidate_id="V000",
                    validation_report_sha256="vr" * 32)},
                    test_grant={"candidate_id": "V000", "grant_id": "ab" * 16},
                    test_consumed=False,
                    phase_reports={"validation": {
                        "sha256": "vr" * 32, "status": "SUCCEEDED",
                        "verdict": "CANDIDATE"}})
                with self.assertRaises(ValueError):
                    authz(closed, "test", dict(grant, phase="test",
                                              candidate_id="V000",
                                              validation_report_sha256="vr" * 32))
            with self.subTest(regla="mismo registrado + consumed True resume"):
                same = dict(base, grants={"test": dict(
                    grant, phase="test", candidate_id="V000",
                    validation_report_sha256="vr" * 32)},
                    test_grant={"candidate_id": "V000", "grant_id": "ab" * 16},
                    test_consumed=True,
                    phase_reports={"validation": {
                        "sha256": "vr" * 32, "status": "SUCCEEDED",
                        "verdict": "CANDIDATE"}})
                self.assertTrue(authz(same, "test", dict(
                    grant, phase="test", candidate_id="V000",
                    validation_report_sha256="vr" * 32)))
            with self.subTest(regla="market.authorize nunca es autoridad"):
                from market.history import authorize_partition

                bare = authorize_partition("test", {"candidate": "V000",
                                                    "consumed": False})
                with self.assertRaises(ValueError):
                    authz(base, "validation", dict(bare, phase="validation",
                                                   grant_id="ab" * 16,
                                                   definition_hash=defhash,
                                                   validation_ids=ids,
                                                   train_report_sha256="tr" * 32))

    def test_validation_binding_uses_finalists_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ids = ["V000", "V001", "V002"]
            state = {"definition_hash": "dh", "train_ids": [f"V{i:03d}" for i in range(9)],
                     "validation_ids": ids, "phase_reports": {}}
            screen = {"status": "SUCCEEDED", "definition_hash": "dh",
                      "verdict": "TOP9", "top9": state["train_ids"]}
            screen_path = root / "sessions" / "screen-1" / "report.json"
            _write(screen_path, json.dumps(screen, sort_keys=True))
            screen_sha = launch.file_hash(screen_path)
            finalists = {"status": "SUCCEEDED", "definition_hash": "dh",
                         "verdict": "TOP3", "validation_ids": ids,
                         "screen_report_sha256": screen_sha}
            finalists_path = root / "sessions" / "finalists-1" / "report.json"
            _write(finalists_path, json.dumps(finalists, sort_keys=True))
            finalists_sha = launch.file_hash(finalists_path)
            state["phase_reports"] = {
                "screen": {"path": "sessions/screen-1/report.json",
                           "sha256": screen_sha, "status": "SUCCEEDED",
                           "verdict": "TOP9"},
                "finalists": {"path": "sessions/finalists-1/report.json",
                              "sha256": finalists_sha, "status": "SUCCEEDED",
                              "verdict": "TOP3"},
            }
            grant = {"campaign_id": S.CAMPAIGN_ID, "grant_id": "ab" * 16,
                     "definition_hash": "dh", "phase": "validation",
                     "validation_ids": ids, "train_report_sha256": finalists_sha}
            control = root / "control"
            _write(control / "validation-grant-abababab.json",
                   json.dumps(grant, sort_keys=True))
            binding = {"kind": S.SNAP_BIND_KIND, "grant_id": grant["grant_id"],
                       "definition_hash": "dh", "expected_ids": ids,
                       "train_report_sha256": finalists_sha}
            _write(control / "validation-snapshot-abababab.json",
                   json.dumps(binding, sort_keys=True))
            with mock.patch.object(S, "CONTAINER_SEARCH", str(root)):
                resolved, resolved_grant = S._resolve_role_binding("validation", state)
            self.assertEqual(resolved, binding)
            self.assertEqual(resolved_grant, grant)


class CacheTamperCase(unittest.TestCase):
    def test_corrupt_cache_not_reused_succeeded(self):
        from research.state import new_state

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session1 = root / "sessions" / "screen-r1"
            batch1 = session1 / "batches"
            session1.mkdir(parents=True)
            batch1.mkdir(parents=True)
            state = new_state("camp-cache", "def-cache")
            state_path = root / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            from research.campaign import generate_variants

            names = [str(v["class_name"]) for v in generate_variants()[:6]]
            groups = [{"names": names, "use_list": True,
                       "spath": str(root / "sp"), "params": {}, "suffix": "t00"}]
            window = {"year": 2019, "seg_dir": "seg00",
                      "start": "2019-01-01T00:00:00+00:00",
                      "end_exclusive": "2019-01-03T00:00:00+00:00"}
            kwargs = dict(phase="screen", role="train", windows=[window],
                          fees=[0.002], groups=groups, snap_root=str(root),
                          manifest_in={"image_id": "img",
                                       "generated_source_sha256": "gen",
                                       "config_hash": "cfg"},
                          code_hashes={"search_hash": "s", "evaluation_hash": "e",
                                       "campaign_hash": "c", "selection_hash": "l",
                                       "candidates_hash": "k", "control_hash": "o",
                                       "equity_hash": "q"},
                          seg_meta_by_dir={"seg00": {"file_5m_sha256": "a",
                                                    "file_1h_sha256": "b"}},
                          by_class_params={})
            stub = _NativeStub("zero")
            with mock.patch.object(S, "CONTAINER_SEARCH", str(root)), \
                    mock.patch.object(S, "_run_native_batch", side_effect=stub):
                _res1, stats1 = S._execute_native_matrix(
                    session_dir=session1, batch_dir=batch1, state=state,
                    state_path=state_path, **kwargs)
            self.assertEqual(stats1["succeeded"], 1)
            self.assertEqual(len(stub.calls), 1)
            zips = list(session1.glob("native_*/r.zip"))
            self.assertEqual(len(zips), 1)
            with open(str(zips[0]), "ab") as handle:
                handle.write(b"TAMPER")
            session2 = root / "sessions" / "screen-r2"
            batch2 = session2 / "batches"
            session2.mkdir(parents=True)
            batch2.mkdir(parents=True)
            state2 = json.loads(state_path.read_text(encoding="utf-8"))
            with mock.patch.object(S, "CONTAINER_SEARCH", str(root)), \
                    mock.patch.object(S, "_run_native_batch", side_effect=stub):
                _res2, stats2 = S._execute_native_matrix(
                    session_dir=session2, batch_dir=batch2, state=state2,
                    state_path=state_path, **kwargs)
            self.assertTrue(len(stub.calls) == 2 or stats2["failed"] > 0,
                            "cache corrupta no se reusa como SUCCEEDED")


class ControlGateCase(unittest.TestCase):
    def test_control_requires_ledger_success(self):
        window = {"year": 2025, "seg_dir": "seg00",
                  "start": "2025-01-01T00:00:00+00:00",
                  "end_exclusive": "2025-01-03T00:00:00+00:00"}
        with self.subTest("ausente"):
            with self.assertRaises(ValueError, msg="control ausente falla la fase"):
                S._control_coverage({}, [window], [0.002], "/nonexistent")
        if not HAS_PANDAS:
            self.skipTest("ledger-error del control exige imagen (pandas)")
        with tempfile.TemporaryDirectory() as tmp, self.subTest("ledger-error"):
            key = (2025, "seg00", 0.002, S.CONTROL_STRATEGY)
            with self.assertRaises(ValueError, msg="ledger del control falla la fase"):
                S._control_coverage({key: {"trades": []}}, [window], [0.002],
                                    str(Path(tmp) / "vacio"))


class NativeOSErrorCase(unittest.TestCase):
    def _matrix(self, tmp, stub, monitor=None):
        from research.state import new_state

        session = Path(tmp) / "sess"
        batch = session / "batches"
        session.mkdir(parents=True)
        batch.mkdir(parents=True)
        state = new_state("camp-io", "def-io")
        state_path = Path(tmp) / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        from research.campaign import generate_variants

        names = [str(v["class_name"]) for v in generate_variants()[:6]]
        groups = [{"names": names, "use_list": True, "spath": str(Path(tmp) / "sp"),
                   "params": {}, "suffix": "t00"}]
        window = {"year": 2019, "seg_dir": "seg00",
                  "start": "2019-01-01T00:00:00+00:00",
                  "end_exclusive": "2019-01-03T00:00:00+00:00"}
        with mock.patch.object(S, "CONTAINER_SEARCH", str(Path(tmp))), \
                mock.patch.object(S, "_run_native_batch", side_effect=stub), \
                (monitor or contextlib.ExitStack()):
            return S._execute_native_matrix(
                phase="screen", role="train", windows=[window], fees=[0.002],
                groups=groups, snap_root=str(Path(tmp)), session_dir=session,
                batch_dir=batch, state=state, state_path=state_path,
                manifest_in={"image_id": "img", "generated_source_sha256": "gen",
                             "config_hash": "cfg"},
                code_hashes={"search_hash": "s", "evaluation_hash": "e",
                             "campaign_hash": "c", "selection_hash": "l",
                             "candidates_hash": "k", "control_hash": "o",
                             "equity_hash": "q"},
                seg_meta_by_dir={"seg00": {"file_5m_sha256": "a",
                                           "file_1h_sha256": "b"}},
                by_class_params={}), state

    def test_oserror_charges_one_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _NativeStub("zero")
            with mock.patch.object(launch, "file_hash",
                                   side_effect=OSError("disk")):
                try:
                    (results, stats), state = self._matrix(tmp, stub, None)
                except OSError:
                    self.fail("RED: OSError escapo sin cargar intento FAILED")
            key = next(a["run_key"] for a in state["attempts"])
            matching = [a for a in state["attempts"] if a["run_key"] == key]
            self.assertEqual(len(matching), 1, "UN solo cargo")
            self.assertEqual(matching[0]["status"], "FAILED")
            self.assertEqual(stats["failed"], 1)

    def test_same_cause_three_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _NativeStub("exit1")
            from research.campaign import generate_variants

            from research.state import new_state

            session = Path(tmp) / "sess"
            batch = session / "batches"
            session.mkdir(parents=True)
            batch.mkdir(parents=True)
            state = new_state("camp-io3", "def-io3")
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            names = [str(v["class_name"]) for v in generate_variants()[:6]]
            groups = [{"names": names, "use_list": True, "spath": str(Path(tmp) / "sp"),
                       "params": {}, "suffix": "t00"}]
            windows = [dict(year=y, seg_dir="seg00",
                            start=f"{y}-01-01T00:00:00+00:00",
                            end_exclusive=f"{y}-01-03T00:00:00+00:00")
                       for y in (2019, 2020, 2021)]
            with mock.patch.object(S, "CONTAINER_SEARCH", str(Path(tmp))), \
                    mock.patch.object(S, "_run_native_batch", side_effect=stub):
                with self.assertRaises(ValueError, msg="3 misma causa paran"):
                    S._execute_native_matrix(
                        phase="screen", role="train", windows=windows, fees=[0.002],
                        groups=groups, snap_root=str(Path(tmp)), session_dir=session,
                        batch_dir=batch, state=state, state_path=state_path,
                        manifest_in={"image_id": "img",
                                     "generated_source_sha256": "gen",
                                     "config_hash": "cfg"},
                        code_hashes={"search_hash": "s", "evaluation_hash": "e",
                                     "campaign_hash": "c", "selection_hash": "l",
                                     "candidates_hash": "k", "control_hash": "o",
                                     "equity_hash": "q"},
                        seg_meta_by_dir={"seg00": {"file_5m_sha256": "a",
                                                  "file_1h_sha256": "b"}},
                        by_class_params={})
            failed = [a for a in state["attempts"] if a["status"] == "FAILED"]
            self.assertEqual(len(failed), 3, "3 cargos antes del stop")


class BundleFailCase(unittest.TestCase):
    def test_bundle_write_failure_is_operational_failed(self):
        _require_pandas(self)
        with tempfile.TemporaryDirectory() as tmp:
            env = _test_env(tmp, True)
            sabotage = (env["search"] / "control"
                        / f"paper-bundle-{env['candidate']}"
                        f"-{env['grant']['grant_id'][:8]}.json")
            sabotage.mkdir(parents=True)
            stub = _TestWinStub()
            with _test_paths(env), \
                    mock.patch.object(S, "_run_native_batch", side_effect=stub):
                rc = S.cmd_test()
            self.assertEqual(rc, 1, "bundle roto => rc 1")
            payload = _latest_test_report(env)
            self.assertEqual(payload.get("status"), "FAILED",
                             "FAILED operacional persistido")
            self.assertNotEqual(payload.get("verdict"), "PASS",
                                "sin artefacto PASS listo")
            state = json.loads(env["state_path"].read_text(encoding="utf-8"))
            self.assertIs(state.get("paper_ready"), False)


class BaselineBaseCase(unittest.TestCase):
    def test_control_identity_binds_real_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp) / "code"
            for rel in list(CODE_TARGETS) + [Path("strategies/baseline/SmaCrossBaseline.py")]:
                dst = code / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes((ROOT / rel).read_bytes())
            hashes = S._code_hashes(code)
            base_hash = launch.file_hash(
                code / "strategies" / "baseline" / "SmaCrossBaseline.py")
            self.assertIn(base_hash, json.dumps(hashes, sort_keys=True),
                          "identidad del control ata la base real")
            with tempfile.TemporaryDirectory() as store:
                snaps = Path(store) / "history" / "snapshots"
                manifest = _snap_manifest(
                    snaps, "snap-prep",
                    [{"index": 0, "seg_dir": "seg00", "p5": b"A", "p1": b"B",
                      "start": "2019-01-01T00:00:00+00:00",
                      "end_exclusive": "2019-01-03T00:00:00+00:00",
                      "eval_start": "2019-01-01T00:00:00+00:00"}],
                    b"W5", b"W1")
                base = "train-snap-prep.json"
                (snaps / base).write_text(json.dumps(manifest, sort_keys=True),
                                          encoding="utf-8")
                with _git_image():
                    S.prepare_search(str(code), store, launch.PINNED_IMAGE, base)
                target = code / "strategies" / "baseline" / "SmaCrossBaseline.py"
                target.write_bytes(target.read_bytes() + b"\n# probe-base\n")
                with _git_image(), self.assertRaises(
                        ValueError, msg="solo la base mutada invalida"):
                    S.prepare_search(str(code), store, launch.PINNED_IMAGE, base)

    def test_missing_base_file_fails_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp) / "code"
            for rel in list(CODE_TARGETS):
                if rel == S.BASELINE_BASE_REL:
                    continue
                dst = code / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes((ROOT / rel).read_bytes())
            with tempfile.TemporaryDirectory() as store:
                snaps = Path(store) / "history" / "snapshots"
                manifest = _snap_manifest(
                    snaps, "snap-prep",
                    [{"index": 0, "seg_dir": "seg00", "p5": b"A", "p1": b"B",
                      "start": "2019-01-01T00:00:00+00:00",
                      "end_exclusive": "2019-01-03T00:00:00+00:00",
                      "eval_start": "2019-01-01T00:00:00+00:00"}],
                    b"W5", b"W1")
                base = "train-snap-prep.json"
                (snaps / base).write_text(json.dumps(manifest, sort_keys=True),
                                          encoding="utf-8")
                with _git_image(), self.assertRaises(
                        ValueError, msg="base ausente no se acepta condicional"):
                    S.prepare_search(str(code), store, launch.PINNED_IMAGE, base)


if __name__ == "__main__":
    unittest.main()
