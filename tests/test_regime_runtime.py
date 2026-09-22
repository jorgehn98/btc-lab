"""Filesystem isolation and fail-closed native fills for the new TRAIN study."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operations import launch
from tests.test_search_runtime import (
    PAIR1, PAIR5, _NativeStub, _code_fixture, _git_image, _seg_one, _snap_manifest,
)

ROOT = Path(__file__).resolve().parents[1]

try:
    import pandas as pd
except ImportError:
    pd = None


class RegimePrepareCase(unittest.TestCase):
    def test_new_campaign_never_overwrites_first_search(self):
        from operations.regime import prepare_regime

        with tempfile.TemporaryDirectory() as tmp:
            code = _code_fixture(tmp)
            for rel in ("operations/regime.py", "research/regime.py",
                        "research/regime_selection.py", "strategies/regime/SpotRegime.py"):
                target = code / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / rel).read_bytes())
            store = Path(tmp) / "store"
            sentinel = store / "search/control/campaign-btc-strategy-search-pr02.json"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text('{"original":"unchanged"}', encoding="utf-8")
            gen = store / "search/generated/SpotCandidatesGenerated.py"
            gen.parent.mkdir(parents=True)
            gen.write_text("old source\n", encoding="utf-8")
            snaps = store / "history/snapshots"
            manifest = _snap_manifest(snaps, "snap-train", [_seg_one()], b"W5", b"W1")
            base = snaps / "train-snap.json"
            base.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            with _git_image():
                input_path = Path(prepare_regime(str(code), str(store),
                                                 launch.PINNED_IMAGE, base.name))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"original":"unchanged"}')
            self.assertEqual(gen.read_text(encoding="utf-8"), "old source\n")
            self.assertEqual(json.loads(input_path.read_text(encoding="utf-8"))["campaign_id"],
                             "btc-spot-regime-study-pr01")
            self.assertEqual(json.loads(next((store / "regime/control").glob("campaign-*.json"))
                                        .read_text(encoding="utf-8"))["consumed"], 0)

            from operations import regime

            mounts = store / "regime/snapshots"
            mounts.mkdir(parents=True)
            (mounts / "train").symlink_to(snaps / "snap-train", target_is_directory=True)
            (mounts / "train-manifest.json").symlink_to(base)
            with mock.patch.multiple(regime, ROOT=store / "regime", CODE=code,
                                     INPUT=input_path, TRAIN=mounts / "train",
                                     TRAIN_MANIFEST=mounts / "train-manifest.json"):
                self.assertEqual(regime.status_regime(), 0,
                                 "el alias RO del mount conserva la identidad del snapshot")


class RegimeLedgerCase(unittest.TestCase):
    def test_native_inverted_force_exit_remains_inconclusive(self):
        if pd is None:
            self.skipTest("ledger requiere pandas de imagen")
        from research.regime import generate_variants
        from operations import search

        prices = pd.DataFrame({
            "date": pd.date_range("2020-12-31T23:00:00Z", periods=24, freq="5min", tz="UTC"),
            "open": [100.0] * 24, "high": [101.0] * 24,
            "low": [99.0] * 24, "close": [100.0] * 24,
            "volume": [10.0] * 24,
        })
        inverted = {"amount": 1.0, "open_rate": 100.0, "close_rate": 100.0,
                    "fee_open": 0.002, "fee_close": 0.002,
                    "open_date": "2021-01-01T00:05:00+00:00",
                    "close_date": "2021-01-01T00:00:00+00:00",
                    "exit_reason": "force_exit"}
        window = {"start": "2020-12-31T23:00:00+00:00",
                  "end_exclusive": "2021-01-01T01:00:00+00:00",
                  "year": 2021, "seg_dir": "seg00", "days": 2 / 24}
        with mock.patch.object(search, "_load_prices_for_window",
                               return_value=(prices, prices, 100.0, 100.0)):
            episode = search._episode_for_variant(generate_variants()[0],
                                                  [inverted], 0.0, window, 0.002,
                                                  "/unused")
        self.assertIs(episode["valid"], False)
        self.assertIn("close_date anterior a open_date", episode["reason"])


class RegimeScreenCase(unittest.TestCase):
    def test_train_screen_uses_only_new_root_and_ends_no_candidate(self):
        if pd is None:
            self.skipTest("screen requiere pandas de imagen")
        from operations import regime, search

        with tempfile.TemporaryDirectory() as tmp:
            code = _code_fixture(tmp)
            for rel in ("operations/regime.py", "research/regime.py",
                        "research/regime_selection.py", "strategies/regime/SpotRegime.py"):
                target = code / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / rel).read_bytes())
            store = Path(tmp) / "store"
            snaps = store / "history/snapshots"
            seg = snaps / "snap-train/seg00"
            seg.mkdir(parents=True)
            idx5 = pd.date_range("2019-01-01", periods=288 * 12, freq="5min", tz="UTC")
            idx1 = pd.date_range("2019-01-01", periods=288, freq="1h", tz="UTC")
            for path, idx in ((seg / PAIR5, idx5), (seg / PAIR1, idx1)):
                pd.DataFrame({"date": idx, "open": 100.0, "high": 100.5,
                              "low": 99.5, "close": 100.0, "volume": 10.0}
                             ).to_feather(str(path))
            seg_def = {"index": 0, "seg_dir": "seg00",
                       "p5": (seg / PAIR5).read_bytes(),
                       "p1": (seg / PAIR1).read_bytes(),
                       "start": "2019-01-01T00:00:00+00:00",
                       "eval_start": "2019-01-09T09:00:00+00:00",
                       "end_exclusive": "2019-01-13T00:00:00+00:00"}
            manifest = _snap_manifest(snaps, "snap-train", [seg_def],
                                      seg_def["p5"], seg_def["p1"])
            base = snaps / "train-snap.json"
            base.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            with _git_image():
                input_path = Path(regime.prepare_regime(
                    str(code), str(store), launch.PINNED_IMAGE, base.name))
            study = store / "regime"
            mounts = study / "snapshots"
            mounts.mkdir(parents=True)
            (mounts / "train").symlink_to(snaps / "snap-train", target_is_directory=True)
            (mounts / "train-manifest.json").symlink_to(base)
            stub = _NativeStub()
            with mock.patch.multiple(regime, ROOT=study, CODE=code, INPUT=input_path,
                                     TRAIN=mounts / "train",
                                     TRAIN_MANIFEST=mounts / "train-manifest.json"), \
                    mock.patch.object(search, "_run_native_batch", side_effect=stub):
                self.assertEqual(regime.screen_regime(), 1)
                self.assertEqual(regime.screen_regime(), 1, "resume sin nuevo calculo")
            self.assertEqual(len(stub.calls), 10, "2 fees × (4 batches + control)")
            self.assertTrue(all("/regime/" in a[a.index("--export-directory") + 1]
                                for a in stub.calls))
            self.assertEqual(list((store / "search/sessions").glob("screen-*")), [])
            report_path = next((study / "sessions").glob("screen-*/report.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual((report["status"], report["verdict"], len(report["records"])),
                             ("SUCCEEDED", "NO_CANDIDATE", 48))
            state = json.loads(next((study / "control").glob("campaign-*.json"))
                               .read_text(encoding="utf-8"))
            self.assertGreater(state["consumed"], 0)
            self.assertEqual(state["grants"], {})
            self.assertFalse(state["test_consumed"])


if __name__ == "__main__":
    unittest.main()
