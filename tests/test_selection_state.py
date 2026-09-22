"""Contratos de seleccion pura y estado de campana (stdlib).

Fuente: tasks/05.md + PRD (72 spot, DD 15% MTM, presupuesto 12h, TEST unico).
Solo tests en raiz; sin prod/docs/commits/work/agentes, sin datos ni runners
nuevos. Reutiliza el registro canonico real (research.campaign) y no
reimplementa vecinos: usa neighbors() para el gate. Sin mocks ni IO.

Contratos cubiertos:
- research/selection.py (stdlib): walk_forward_select(records, evaluation_year),
  choose_train_finalists(records), choose_validation_candidates(records,
  train_ids, bias_verdicts), choose_test_candidate(validation_records,
  validation_ids), test_verdict(records, candidate_id).
- research/state.py (stdlib, ValueError, sin framework): new_state(campaign_id,
  definition_hash), record_attempt(state, run_key, elapsed_seconds, status,
  evidence_hash), can_reuse_run(state, key, evidence_hash),
  freeze_train_selection(state, ids), freeze_validation_selection(state, ids),
  reserve_test(state, candidate_id), authorize_test_resume(state, candidate_id,
  grant_id, definition_hash).

Esquema de registro exacto (cada registro agrega episodios/ano con ledger;
varianza/dias ya vienen calculados, solo funciones puras seleccionan):
  {variant_id:str, role:"train"|"validation"|"test", year:int, fee:float,
   valid:bool, days:float>0, g:float, bh_g:float, max_drawdown_pct:float 0..1,
   trades_nonforced:int>=0, turnover:float>=0, g_without_positive_forced:float}
Turnover por ano = notional total/10000 (no tasa diaria); en periodo se suma.
Trades WF: >=30 ACUMULADOS del prefijo TRAIN; DD de ranking: maximo de todo
TRAIN 2017-22 a 0.002.
Roles/anos/fees PRD: TRAIN 2017-2022 (final 2019-2022), VALIDATION 2023-2024,
TEST 2025-2026 observado; fee primario 0.002, stress 0.003; DD<=0.15 MTM pct;
bias_verdicts es dict variant_id->bool (True=PASS); test_verdict devuelve
{"verdict": PASS|FAIL|INCONCLUSIVE, "reasons": [...]}.

Fixture: sintetico completo en registros (72 variantes en positivo por defecto,
luego mutaciones de ano futuro/DD); ano ausente/invalido => INCONCLUSIVE o
rechazo, nunca descarte silencioso; cero trades con g=0 cuenta en el score.
Persistencia runtime y montajes RO se prueban con CLI real cuando exista;
aqui solo estado puro (consumo marcado sincrono, sin IO simulada).

Run host (stdlib): python3 -m unittest tests.test_selection_state -v.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.campaign import MAX_DRAWDOWN_PCT, generate_variants, neighbors

FEE = 0.002
FEE_STRESS = 0.003
TRAIN_ALL = (2017, 2018, 2019, 2020, 2021, 2022)
TRAIN_SEL = (2019, 2020, 2021, 2022)
VAL_YEARS = (2023, 2024)
TEST_YEARS = (2025, 2026)
DD_LIMIT = 0.15
BUDGET_SECONDS = 12 * 3600
RECORD_FIELDS = (
    "variant_id", "role", "year", "fee", "valid", "days", "g", "bh_g",
    "max_drawdown_pct", "trades_nonforced", "turnover",
    "g_without_positive_forced",
)

assert abs(float(MAX_DRAWDOWN_PCT) - DD_LIMIT) < 1e-12, "PRD lookup: DD 15%"


def _selection():
    try:
        import research.selection as sel  # type: ignore
    except Exception as exc:
        raise AssertionError(
            "RED: research/selection.py ausente "
            "(walk_forward_select/choose_train_finalists/"
            "choose_validation_candidates/choose_test_candidate/test_verdict): "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return sel


def _state_mod():
    try:
        import research.state as st  # type: ignore
    except Exception as exc:
        raise AssertionError(
            "RED: research/state.py ausente "
            "(new_state/record_attempt/can_reuse_run/freeze_*/reserve_test/"
            "authorize_test_resume): "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return st


def _by_id():
    return {v["id"]: v for v in generate_variants()}


def _pick(family, seed_index, stop, risk):
    for v in generate_variants():
        if (v["family"] == family and int(v["seed_index"]) == seed_index
                and abs(float(v["stop"]) - stop) < 1e-12
                and v["risk_profile"] == risk):
            return v
    raise AssertionError(f"fixture sin variante {family}/{seed_index}/{stop}/{risk}")


def _default_records():
    """72 variantes x anos x fees 0.002/0.003, todo positivo por defecto."""
    recs = []
    for v in generate_variants():
        vid = v["id"]
        for role, years in (("train", TRAIN_ALL), ("validation", VAL_YEARS),
                            ("test", TEST_YEARS)):
            n = 30 if role == "train" else 20
            for year in years:
                for fee in (FEE, FEE_STRESS):
                    recs.append({
                        "variant_id": vid,
                        "role": role,
                        "year": int(year),
                        "fee": float(fee),
                        "valid": True,
                        "days": 365.0,
                        "g": 0.002,
                        "bh_g": 0.0,
                        "max_drawdown_pct": 0.05,
                        "trades_nonforced": int(n),
                        "turnover": 1.0,
                        "g_without_positive_forced": 0.002,
                    })
    return recs


def _set(records, vid, role, year, fee, **over):
    found = False
    for r in records:
        if (r["variant_id"] == vid and r["role"] == role
                and r["year"] == year and abs(float(r["fee"]) - fee) < 1e-12):
            r.update(over)
            found = True
    if not found:
        raise AssertionError(f"fixture sin registro {vid}/{role}/{year}/{fee}")
    return records


def _drop(records, vid, role, year, fee):
    kept = [r for r in records
            if not (r["variant_id"] == vid and r["role"] == role
                    and r["year"] == year and abs(float(r["fee"]) - fee) < 1e-12)]
    if len(kept) == len(records):
        raise AssertionError(f"fixture sin registro a borrar {vid}/{role}/{year}/{fee}")
    return kept


class WalkForwardCase(unittest.TestCase):
    def test_uses_only_past_never_evaluation_year(self):
        sel = _selection()
        recs = _default_records()
        by_id = _by_id()
        lows = [vid for vid, v in by_id.items() if v["risk_profile"] == "low"]
        past_best = _pick("trend", 0, 0.02, "low")["id"]
        future_star = _pick("breakout", 3, 0.04, "low")["id"]
        # Pasado: past_best fuerte, resto flojo; futuro 2020: estrella retrospectiva.
        for vid in lows:
            for y in (2017, 2018, 2019):
                g = 0.006 if vid == past_best else 0.0005
                _set(recs, vid, "train", y, FEE, g=g,
                     g_without_positive_forced=g)
        _set(recs, future_star, "train", 2020, FEE, g=0.05,
             g_without_positive_forced=0.05)
        _set(recs, past_best, "train", 2020, FEE, g=0.0004,
             g_without_positive_forced=0.0004)
        got = sel.walk_forward_select(recs, 2020)
        self.assertEqual(set(got), {"low", "medium", "high"})
        self.assertEqual(got["low"], past_best,
                         "el ganador del ano evaluado no se elige a si mismo")
        self.assertNotEqual(got["low"], future_star)

    def test_ineligible_past_is_cash_not_retrospective(self):
        sel = _selection()
        for label, mutate in [
            ("pocos trades acumulados (<30 en prefijo)", lambda r, v: [
                _set(r, v, "train", y, FEE, trades_nonforced=5)
                for y in (2017, 2018, 2019, 2020)]),
            ("DD pasado", lambda r, v: [
                _set(r, v, "train", 2018, FEE, max_drawdown_pct=0.20)]),
            ("g pasado no positivo", lambda r, v: [
                _set(r, v, "train", y, FEE, g=-0.001,
                     g_without_positive_forced=-0.001) for y in (2017, 2018, 2019)]),
            ("solo fee stress positivo", lambda r, v: ([
                _set(r, v, "train", y, FEE, g=-0.001,
                     g_without_positive_forced=-0.001) for y in (2017, 2018, 2019)] + [
                _set(r, v, "train", y, FEE_STRESS, g=0.009,
                     g_without_positive_forced=0.009) for y in (2017, 2018, 2019)])),
        ]:
            with self.subTest(label=label):
                recs = _default_records()
                by_id = _by_id()
                star = _pick("trend", 0, 0.02, "low")["id"]
                for vid in [v for v, m in by_id.items() if m["risk_profile"] == "low"]:
                    mutate(recs, vid)
                _set(recs, star, "train", 2021, FEE, g=0.06,
                     g_without_positive_forced=0.06)
                got = sel.walk_forward_select(recs, 2021)
                self.assertIsNone(got["low"], f"{label}: pasado inelegible queda en efectivo")

    def test_prefix_trades_accumulate_not_per_year(self):
        # PRD: >=30 trades ACUMULADOS del prefijo, no 30 cada ano.
        sel = _selection()
        recs = _default_records()
        star = _pick("trend", 0, 0.02, "low")["id"]
        for y, n in ((2017, 10), (2018, 10), (2019, 10), (2020, 0)):
            g = 0.004 if n else 0.0
            _set(recs, star, "train", y, FEE, trades_nonforced=n, g=g,
                 g_without_positive_forced=g)
        got = sel.walk_forward_select(recs, 2021)
        self.assertEqual(got["low"], star,
                         "30 acumulados en prefijo eligen, sin exigir 30 cada ano")


class TrainFinalistsCase(unittest.TestCase):
    def test_gates_reject_without_dropping(self):
        sel = _selection()
        target = _pick("trend", 1, 0.02, "low")
        vid = target["id"]
        cases = [
            ("cien trades", lambda r: [
                _set(r, vid, "train", y, FEE, trades_nonforced=10) for y in TRAIN_SEL]),
            ("tres de cuatro", lambda r: [
                _set(r, vid, "train", y, FEE, g=-0.002,
                     g_without_positive_forced=-0.002) for y in (2019, 2020)]),
            ("mediana exceso", lambda r: [
                _set(r, vid, "train", y, FEE, bh_g=0.008) for y in TRAIN_SEL]),
            ("DD todo TRAIN (2018 manda)", lambda r: [
                _set(r, vid, "train", 2018, FEE, max_drawdown_pct=0.20)]),
            ("stress forzados", lambda r: [
                _set(r, vid, "train", y, FEE, g_without_positive_forced=-0.001)
                for y in TRAIN_SEL]),
            ("vecinos", lambda r: [
                _set(r, n, "train", y, FEE, g=-0.005,
                     g_without_positive_forced=-0.005)
                for n in neighbors(vid)[:2] for y in TRAIN_SEL]),
            ("registro invalido", lambda r: [
                _set(r, vid, "train", 2022, FEE, valid=False)]),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                recs = _default_records()
                # El objetivo ganaria su grupo por defecto (g mayor): los gates
                # deben rechazarlo de verdad, no heredar un `not in` trivial.
                for y in TRAIN_SEL:
                    _set(recs, vid, "train", y, FEE, g=0.006,
                         g_without_positive_forced=0.006)
                mutate(recs)
                got = sel.choose_train_finalists(recs)
                self.assertNotIn(vid, got, f"{label}: debe rechazar, no rescatar")
        # Control positivo: con g mayor y sin mutaciones lidera su grupo.
        recs = _default_records()
        for y in TRAIN_SEL:
            _set(recs, vid, "train", y, FEE, g=0.006,
                 g_without_positive_forced=0.006)
        got = sel.choose_train_finalists(recs)
        self.assertIn(vid, got, "control positivo en tabla")

    def test_table_closed_ranked_train_only(self):
        sel = _selection()
        recs = _default_records()
        by_id = _by_id()
        # Nueve como maximo, uno por familia/riesgo, ranking solo TRAIN 2019-22.
        got = sel.choose_train_finalists(recs)
        self.assertLessEqual(len(got), 9)
        slots = [(by_id[i]["family"], by_id[i]["risk_profile"]) for i in got]
        self.assertEqual(len(set(slots)), len(slots), "uno por familia/riesgo")
        # g manda sobre ID: sube a un ID alto dentro de su grupo.
        group = [v["id"] for v in generate_variants()
                 if v["family"] == "trend" and v["risk_profile"] == "low"]
        low_default = min(group)
        high = max(group)
        for y in TRAIN_SEL:
            _set(recs, high, "train", y, FEE, g=0.008,
                 g_without_positive_forced=0.008)
            _set(recs, high, "train", y, FEE, turnover=1.0)
        # Ruido futuro/stress que no debe alterar el ranking TRAIN 0.002.
        for y in VAL_YEARS:
            _set(recs, low_default, "validation", y, FEE, g=0.09,
                 g_without_positive_forced=0.09)
        for y in TRAIN_SEL:
            _set(recs, low_default, "train", y, FEE_STRESS, g=0.09,
                 g_without_positive_forced=0.09)
        got = sel.choose_train_finalists(recs)
        trend_low = [i for i in got if by_id[i]["family"] == "trend"
                     and by_id[i]["risk_profile"] == "low"]
        self.assertEqual(trend_low, [high], "ranking usa g TRAIN 0.002, no futuro ni stress")
        # Desempate determinista: igual g, menor DD gana; luego turnover; luego ID.
        # El DD del desempate es el maximo de TODO TRAIN 2017-22 a 0.002.
        recs2 = _default_records()
        for y in TRAIN_SEL:
            _set(recs2, high, "train", y, FEE, g=0.002,
                 g_without_positive_forced=0.002, max_drawdown_pct=0.09)
            _set(recs2, low_default, "train", y, FEE, g=0.002,
                 g_without_positive_forced=0.002, max_drawdown_pct=0.01)
        for y in (2017, 2018):
            _set(recs2, high, "train", y, FEE, max_drawdown_pct=0.01)
            _set(recs2, low_default, "train", y, FEE, max_drawdown_pct=0.14)
        # Aisla la comparacion: el resto del grupo a 0.12 (elegible, sin ganar).
        rest = [i for i in group if i not in (high, low_default)]
        self.assertTrue(rest, "grupo trend/low con mas candidatos")
        for oid in rest:
            for y in TRAIN_ALL:
                _set(recs2, oid, "train", y, FEE, max_drawdown_pct=0.12)
        got2 = sel.choose_train_finalists(recs2)
        trend_low2 = [i for i in got2 if by_id[i]["family"] == "trend"
                      and by_id[i]["risk_profile"] == "low"]
        self.assertEqual(trend_low2, [high], "desempate DD usa maximo de todo TRAIN")
        # Turnover por ano es notional total/10000 (no tasa diaria): se suma.
        recs3 = _default_records()
        for y in TRAIN_SEL:
            _set(recs3, high, "train", y, FEE, turnover=0.5)
            _set(recs3, low_default, "train", y, FEE, turnover=5.0)
        got3 = sel.choose_train_finalists(recs3)
        trend_low3 = [i for i in got3 if by_id[i]["family"] == "trend"
                      and by_id[i]["risk_profile"] == "low"]
        self.assertEqual(trend_low3, [high], "desempate por menor turnover sumado")


class ValidationCase(unittest.TestCase):
    def test_no_backfill_stress_bias(self):
        sel = _selection()
        recs = _default_records()
        train_ids = sel.choose_train_finalists(recs)
        self.assertEqual(len(train_ids), 9, "control: tabla TRAIN completa")
        by_id = _by_id()
        bias = {vid: True for vid in train_ids}
        # Estresa un finalista low y veta otro por sesgo.
        lows = [i for i in train_ids if by_id[i]["risk_profile"] == "low"]
        self.assertEqual(len(lows), 3, "un low por familia")
        stressed = (lows[0], lows[2])
        biased_out = lows[1]
        for s_id in stressed:
            for y in TRAIN_SEL:
                _set(recs, s_id, "train", y, FEE_STRESS, max_drawdown_pct=0.25)
        bias[biased_out] = False
        got = sel.choose_validation_candidates(recs, train_ids, bias)
        self.assertLessEqual(len(got), 2, "slot low vacio sin backfill")
        self.assertNotIn(stressed[0], got, "stress 0.3 excluye sin sustituir")
        self.assertNotIn(stressed[1], got, "stress 0.3 excluye sin sustituir")
        self.assertNotIn(biased_out, got, "sesgo excluye sin sustituir")
        for i in got:
            self.assertIn(i, train_ids, "sin backfill fuera de tabla")
        risks = [by_id[i]["risk_profile"] for i in got]
        self.assertEqual(len(set(risks)), len(risks), "uno por perfil")


class TestCandidateCase(unittest.TestCase):
    def test_val_only_no_train_rescue(self):
        sel = _selection()
        recs = _default_records()
        train_ids = sel.choose_train_finalists(recs)
        bias = {vid: True for vid in train_ids}
        val_ids = sel.choose_validation_candidates(recs, train_ids, bias)
        self.assertEqual(len(val_ids), 3)
        by_id = _by_id()
        # TRAIN-fuerte pero VAL-debil debe caer; VAL-fuerte con TRAIN flojo gana.
        train_star = val_ids[0]
        challenger = [i for i in val_ids if i != train_star][0]
        for y in VAL_YEARS:
            _set(recs, train_star, "validation", y, FEE, g=-0.004,
                 g_without_positive_forced=-0.004)
            _set(recs, challenger, "validation", y, FEE, g=0.003,
                 g_without_positive_forced=0.003)
        # Nota: TRAIN del retador se hunde, no debe importar.
        for y in TRAIN_SEL:
            _set(recs, challenger, "train", y, FEE, g=0.0001,
                 g_without_positive_forced=0.0001)
        val_recs = [r for r in recs if r["role"] == "validation"]
        got = sel.choose_test_candidate(val_recs, val_ids)
        self.assertIsNotNone(got)
        self.assertNotEqual(got, train_star, "VAL manda, TRAIN no rescata")
        # DD stress 0.3 en VAL excluye aunque 0.2 pase.
        recs2 = _default_records()
        only = val_ids[0]
        for other in val_ids[1:]:
            for y in VAL_YEARS:
                _set(recs2, other, "validation", y, FEE, g=-0.004,
                     g_without_positive_forced=-0.004)
        for y in VAL_YEARS:
            _set(recs2, only, "validation", y, FEE_STRESS, max_drawdown_pct=0.30)
        val_recs2 = [r for r in recs2 if r["role"] == "validation"]
        self.assertIsNone(sel.choose_test_candidate(val_recs2, val_ids),
                          "stress 0.3 en VAL bloquea")


class VerdictCase(unittest.TestCase):
    def test_pass_fail_inconclusive(self):
        sel = _selection()
        recs = _default_records()
        cand = sel.choose_train_finalists(recs)[0]
        ok = sel.test_verdict(recs, cand)
        self.assertEqual(ok["verdict"], "PASS", ok.get("reasons"))
        # DD en TEST falla.
        bad = _default_records()
        for y in TEST_YEARS:
            _set(bad, cand, "test", y, FEE, max_drawdown_pct=0.30)
        fail = sel.test_verdict(bad, cand)
        self.assertEqual(fail["verdict"], "FAIL")
        self.assertTrue(fail["reasons"], "FAIL con motivos")
        # Ano ausente no se descarta: INCONCLUSIVE.
        missing = _drop(_default_records(), cand, "test", 2026, FEE)
        inc = sel.test_verdict(missing, cand)
        self.assertEqual(inc["verdict"], "INCONCLUSIVE")
        self.assertTrue(inc["reasons"])
        # Pocos trades: INCONCLUSIVE, no candidata apta.
        few = _default_records()
        for y in TEST_YEARS:
            _set(few, cand, "test", y, FEE, trades_nonforced=5)
        self.assertEqual(sel.test_verdict(few, cand)["verdict"], "INCONCLUSIVE")
        # Cero trades con g=0 cuenta: FAIL positivo, no INCONCLUSIVE.
        zero = _default_records()
        _set(zero, cand, "test", 2025, FEE, trades_nonforced=0, g=0.0,
             bh_g=0.0, g_without_positive_forced=0.0)
        _set(zero, cand, "test", 2026, FEE, trades_nonforced=40)
        self.assertEqual(sel.test_verdict(zero, cand)["verdict"], "FAIL")
        # Sin segundo candidato de repuesto en el dictamen.
        self.assertNotIn("alternative", ok, "TEST no sustituye")


class RecordSchemaCase(unittest.TestCase):
    def test_malformed_record_raises(self):
        sel = _selection()
        recs = _default_records()
        cand = sel.choose_train_finalists(recs)[0]
        bad = _default_records()
        # Roto con rol validation: es el unico que sobrevive al filtro de
        # test_candidate y todas las funciones validan cada registro de entrada.
        broken = dict(next(r for r in bad if r["role"] == "validation"))
        broken.pop("max_drawdown_pct")
        train_ids = sel.choose_train_finalists(_default_records())
        bias_all = {v: True for v in train_ids}
        val_ids = sel.choose_validation_candidates(_default_records(), train_ids, bias_all)
        val_recs = [x for x in _default_records() if x["role"] == "validation"]
        funcs = [
            ("walk_forward", lambda r: sel.walk_forward_select(r, 2020)),
            ("train_finalists", lambda r: sel.choose_train_finalists(r)),
            ("validation", lambda r: sel.choose_validation_candidates(r, train_ids, bias_all)),
            ("test_candidate", lambda r: sel.choose_test_candidate(
                [x for x in r if x["role"] == "validation"], val_ids)),
            ("verdict", lambda r: sel.test_verdict(r, cand)),
        ]
        for name, fn in funcs:
            with self.subTest(func=name):
                with self.assertRaises(ValueError, msg="registro sin gate field rechaza"):
                    fn([broken] + bad[1:])

    def test_duplicate_key_contradictory_raises(self):
        sel = _selection()
        recs = _default_records()
        clone = dict(next(r for r in recs if r["role"] == "train"))
        clone["g"] = float(clone["g"]) + 0.05
        with self.assertRaises(ValueError, msg="clave duplicada contradictoria rechaza"):
            sel.choose_train_finalists(recs + [clone])


class StateSchemaCase(unittest.TestCase):
    def test_new_state_budget_stage(self):
        st = _state_mod()
        s = st.new_state("camp-01", "hash-abc")
        self.assertEqual(s["campaign_id"], "camp-01")
        self.assertEqual(s["definition_hash"], "hash-abc")
        self.assertEqual(s["budget_limit"], BUDGET_SECONDS)
        self.assertEqual(s["consumed"], 0)
        self.assertEqual(s["stage"], "REGISTERED")
        self.assertEqual(s["native_runs"], {})


class BudgetCase(unittest.TestCase):
    def test_attempts_persist_failed_and_stop_at_limit(self):
        st = _state_mod()
        s = st.new_state("camp-01", "hash-abc")
        st.record_attempt(s, "run-a", 3600, "SUCCEEDED", "ev-a")
        st.record_attempt(s, "run-b", 60, "FAILED", "ev-b")
        self.assertIn("run-a", s["native_runs"])
        self.assertIn("run-b", s["native_runs"], "fallidos visibles, no ocultos")
        self.assertEqual(s["consumed"], 3660, "presupuesto sin reset")
        self.assertTrue(st.can_reuse_run(s, "run-a", "ev-a"))
        self.assertFalse(st.can_reuse_run(s, "run-b", "ev-b"), "fallido no reutiliza")
        self.assertFalse(st.can_reuse_run(s, "run-a", "otro-hash"))
        with self.assertRaises(ValueError, msg="tope 12h detiene, no concede en silencio"):
            st.record_attempt(s, "run-c", BUDGET_SECONDS, "SUCCEEDED", "ev-c")


class FreezeCase(unittest.TestCase):
    def test_train_then_validation_subset(self):
        st = _state_mod()
        sel = _selection()
        s = st.new_state("camp-01", "hash-abc")
        train_ids = sel.choose_train_finalists(_default_records())
        st.freeze_train_selection(s, train_ids)
        self.assertEqual(sorted(s["train_ids"]), sorted(train_ids))
        self.assertNotEqual(s["stage"], "REGISTERED")
        with self.assertRaises(ValueError, msg="mas de 9 frena"):
            st.freeze_train_selection(st.new_state("c", "h"), train_ids + ["V999"])
        two = train_ids[:2]
        st.freeze_validation_selection(s, two)
        self.assertEqual(sorted(s["validation_ids"]), sorted(two))
        outsider = [v["id"] for v in generate_variants() if v["id"] not in train_ids][0]
        with self.assertRaises(ValueError, msg="fuera de tabla frena"):
            st.freeze_validation_selection(s, [outsider])
        with self.assertRaises(ValueError, msg="mas de 3 frena"):
            st.freeze_validation_selection(s, train_ids[:4])


class TestGrantCase(unittest.TestCase):
    def test_reserve_consumes_before_read_single_grant(self):
        st = _state_mod()
        sel = _selection()
        recs = _default_records()
        train_ids = sel.choose_train_finalists(recs)
        bias = {vid: True for vid in train_ids}
        val_ids = sel.choose_validation_candidates(recs, train_ids, bias)
        val_recs = [r for r in recs if r["role"] == "validation"]
        cand = sel.choose_test_candidate(val_recs, val_ids)
        self.assertIsNotNone(cand)
        s = st.new_state("camp-01", "hash-abc")
        st.freeze_train_selection(s, train_ids)
        st.freeze_validation_selection(s, val_ids)
        grant = st.reserve_test(s, cand)
        self.assertEqual(grant["candidate_id"], cand)
        self.assertTrue(grant["grant_id"], "grant inmutable con id")
        self.assertTrue(s.get("test_consumed") or s.get("stage") == "TEST_RESERVED",
                        "consumo marcado sincrono, antes de lectura")
        with self.assertRaises(ValueError, msg="segunda candidata frena"):
            st.reserve_test(s, [i for i in val_ids if i != cand][0])
        self.assertTrue(st.authorize_test_resume(s, cand, grant["grant_id"], "hash-abc"))
        self.assertFalse(st.authorize_test_resume(s, cand, "otro-grant", "hash-abc"))
        self.assertFalse(st.authorize_test_resume(s, cand, grant["grant_id"], "otro-hash"),
                         "mutar definition_hash falla")


if __name__ == "__main__":
    unittest.main()
