"""Synthetic checks for exact CFA DN recovery and noise-domain conversion."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import numpy as np

from engine_patch.calibration import (
    CalibrationError, CalibrationSession, MAX_INTEGER_RECOVERY_ERROR_DN,
    SCHEMA, install, load_payload, normalize_native,
)


def payload_fixture(folder, *, cfa=(0, 1, 1, 2)):
    rows = []
    for i in range(7):
        source = folder / f"frame-{i}.dng"
        source.write_bytes(b"opaque synthetic RAW identity\x00" + bytes([i]))
        rows.append({
            "source": str(source.resolve()),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "width": 4, "height": 4, "cfa_pattern": list(cfa),
            "black_levels_dn": [64.06, 64.0, 64.0, 64.06],
            "white_levels_dn": [1023.0] * 4,
            "noise_s": [0.0003, 0.0004, 0.0005, 0.0006],
            "noise_o": [0.000001, 0.000002, 0.000003, 0.000004],
            "iso": 640, "shutter_seconds": 0.04,
        })
    return {"schema": SCHEMA, "frames": rows}


def native_fixture(frame, dn=None):
    if dn is None:
        dn = np.array([[0, 0, 64, 64], [0, 0, 64, 64],
                       [1018, 1018, 1023, 1023], [1018, 1018, 1023, 1023]], dtype=np.uint16)
    black = np.floor(np.asarray(frame.black)).reshape(2, 2).astype(np.float32)
    white = np.asarray(frame.white).reshape(2, 2).astype(np.float32)
    span = white - np.float32(5) - black
    raw = np.empty(dn.shape, dtype=np.float32)
    for y in range(2):
        for x in range(2):
            raw[y::2, x::2] = (dn[y::2, x::2].astype(np.float32) - black[y, x]) / span[y, x]
    red, blue = frame.cfa_pattern.index(0), frame.cfa_pattern.index(2)
    metadata = {
        "source": str(frame.source), "source_hw": [frame.height, frame.width],
        "native_hw": [frame.height, frame.width],
        "crop_xywh": [0, 0, frame.width, frame.height],
        "cfa_index": [red, red ^ 1, blue ^ 1, blue],
        "black": black.tolist(), "white": (white - 5).tolist(),
        "denominator": span.tolist(), "original_decoder_white": white.tolist(),
    }
    return raw, metadata, dn


def expected_normalization(frame, dn):
    output = np.empty(dn.shape, dtype=np.float32)
    for p in range(4):
        y, x = divmod(p, 2)
        output[y::2, x::2] = ((dn[y::2, x::2].astype(np.float64) - frame.black[p]) /
                              (frame.white[p] - 5 - frame.black[p])).astype(np.float32)
    return output


def installed_physical(enabled=True):
    package = ModuleType("v15_2")
    physical = ModuleType("v15_2.physical")
    physical._JSR_CFA_CALIBRATION_V1 = enabled
    package.physical = physical
    return patch.dict(sys.modules, {"v15_2": package, "v15_2.physical": physical})


class ExactCfaCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jsr-cfa-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.payload = payload_fixture(self.folder)
        self.frame = next(iter(load_payload(self.payload).values()))

    def test_recovers_exact_dn_and_keeps_negative_zero_and_saturation(self):
        raw, metadata, dn = native_fixture(self.frame)
        original_raw, original_metadata = raw.copy(), deepcopy(metadata)
        output, updated = normalize_native(raw, metadata, self.frame)
        np.testing.assert_array_equal(output, expected_normalization(self.frame, dn))
        self.assertLess(output[0, 2], 0)  # DN=64 is below the fractional red black.
        self.assertEqual(output[0, 3], 0)  # Green black is exactly 64 DN.
        self.assertLess(output[0, 0], 0)  # No pre-fusion zero clipping.
        self.assertGreater(output[2, 2], 1)  # No pre-fusion highlight clipping.
        self.assertEqual(updated["black"], [[64.06, 64.0], [64.0, 64.06]])
        self.assertEqual(updated["original_decoder_black"], [[64.0, 64.0], [64.0, 64.0]])
        self.assertFalse(updated["cfa_calibration"]["black_averaged"])
        np.testing.assert_array_equal(raw, original_raw)
        self.assertEqual(metadata, original_metadata)

    def test_all_uint16_codes_roundtrip_in_every_phase(self):
        frame = replace(self.frame, width=512, height=512,
                        black=(64.06, 65.25, 66.50, 67.75),
                        white=(1023.0, 4095.0, 16383.0, 65535.0))
        codes = np.arange(65536, dtype=np.uint16).reshape(256, 256)
        dn = np.empty((512, 512), dtype=np.uint16)
        for y in range(2):
            for x in range(2):
                dn[y::2, x::2] = codes
        raw, metadata, _ = native_fixture(frame, dn)
        output, updated = normalize_native(raw, metadata, frame)
        np.testing.assert_array_equal(output, expected_normalization(frame, dn))
        self.assertLess(updated["cfa_calibration"]["uint16_recovery_max_error_dn"],
                        MAX_INTEGER_RECOVERY_ERROR_DN)

    def test_noise_changes_normalization_domain_without_channel_averaging(self):
        noise = self.frame.engine_noise()
        for i in range(4):
            ratio = ((self.frame.white[i] - self.frame.black[i]) /
                     (self.frame.white[i] - 5 - self.frame.black[i]))
            self.assertAlmostEqual(noise[i, 0], self.frame.noise_s[i] * ratio, places=18)
            self.assertAlmostEqual(noise[i, 1], self.frame.noise_o[i] * ratio ** 2, places=20)
        self.assertEqual(len(set(noise[:, 0])), 4)
        np.testing.assert_array_equal(noise[:, 2:], 0)

    def test_noninteger_native_data_is_rejected_instead_of_approximated(self):
        raw, metadata, _ = native_fixture(self.frame)
        raw[0, 0] += np.float32(0.25 / metadata["denominator"][0][0])
        with self.assertRaisesRegex(CalibrationError, "uint16 normalization"):
            normalize_native(raw, metadata, self.frame)

    def test_nonfinite_and_geometry_mismatches_are_rejected(self):
        raw, metadata, _ = native_fixture(self.frame)
        for field, value in (("crop_xywh", [2, 0, 4, 4]), ("source_hw", [4, 6]),
                             ("cfa_index", [0, 2, 1, 3]),
                             ("denominator", [[0, 954], [954, 954]])):
            with self.subTest(field=field):
                changed = deepcopy(metadata)
                changed[field] = value
                with self.assertRaises(CalibrationError):
                    normalize_native(raw, changed, self.frame)
        raw[0, 0] = np.nan
        with self.assertRaisesRegex(CalibrationError, "non-finite"):
            normalize_native(raw, metadata, self.frame)

    def test_payload_missing_fields_mixed_group_and_invalid_bounds_fail_closed(self):
        cases = [
            ("sha256", "0" * 64), ("black_levels_dn", [1018.0] * 4),
            ("white_levels_dn", [65536.0] * 4), ("noise_s", [0.0] * 4),
            ("noise_o", [float("nan")] * 4), ("noise_o", [1e-100] * 4),
            ("width", 5), ("width", 10 ** 1000), ("cfa_pattern", [0, 2, 1, 1]),
            ("iso", 800), ("shutter_seconds", 0.05),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=str(value)[:40]):
                changed = deepcopy(self.payload)
                changed["frames"][1][field] = value
                with self.assertRaises(CalibrationError):
                    load_payload(changed)
        changed = deepcopy(self.payload)
        changed["frames"][0].pop("noise_s")
        with self.assertRaises(CalibrationError):
            load_payload(changed)
        with self.assertRaises(CalibrationError):
            load_payload({"schema": SCHEMA, "frames": self.payload["frames"][:6]})

    def test_duplicate_paths_and_content_are_rejected(self):
        changed = deepcopy(self.payload)
        changed["frames"][1] = changed["frames"][0].copy()
        with self.assertRaisesRegex(CalibrationError, "Duplicate input path"):
            load_payload(changed)
        duplicate = Path(self.payload["frames"][1]["source"])
        duplicate.write_bytes(self.frame.source.read_bytes())
        changed = deepcopy(self.payload)
        changed["frames"][1]["sha256"] = self.frame.sha256
        with self.assertRaisesRegex(CalibrationError, "Duplicate input content"):
            load_payload(changed)

    def test_json_duplicate_keys_are_rejected(self):
        path = self.folder / "invalid.json"
        path.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
        with self.assertRaisesRegex(CalibrationError, "Duplicate JSON"):
            load_payload(path)

    def test_source_changed_after_payload_is_rejected_before_decoding(self):
        session = CalibrationSession(self.payload)
        self.frame.source.write_bytes(b"changed")
        calls = []
        decode = session.decoder(lambda path: calls.append(path), 999)
        with self.assertRaisesRegex(CalibrationError, "changed after verification"):
            decode(self.frame.source)
        self.assertEqual(calls, [])

    def make_manifest(self, *, cfa=(0, 1, 1, 2)):
        payload = deepcopy(self.payload)
        for row in payload["frames"]:
            row["cfa_pattern"] = list(cfa)
        session = CalibrationSession(payload)
        sensor = self.folder / "work" / "sensor"
        sensor.mkdir(parents=True)
        document = {"schema": "TRANSFORMER_JSR_INPUT_V1", "frames": [],
                    "raw_records": [], "reference_index": 3,
                    "developer_calibration": {"enabled": True}}

        def native_decode(path):
            raw, metadata, _ = native_fixture(session.lookup(path))
            return raw, metadata

        decode = session.decoder(native_decode, 999.0)
        for i, frame in enumerate(session.frames.values()):
            raw, metadata = decode(frame.source)
            np.save(sensor / f"raw-{i}.npy", raw)
            index = metadata["cfa_index"]
            underflow = (-np.asarray(metadata["black"]) /
                         np.asarray(metadata["denominator"])).ravel()[index].tolist()
            document["frames"].append({"raw": f"raw-{i}.npy", "source_id": str(frame.source),
                                       "exposure": 1.0, "white": 1.0, "underflow": underflow,
                                       "cfa_index": index})
            document["raw_records"].append(metadata)
        path = sensor / "input.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return session, path, document

    def test_manifest_keeps_exact_channel_model_and_audit_for_bggr(self):
        session, path, before = self.make_manifest(cfa=(2, 1, 1, 0))
        with installed_physical():
            result = session.calibrate(path, 9.0, 999.0, 99.0)
        # Windows CI may expose TEMP through an 8.3 alias (RUNNER~1).
        # The calibration API returns the canonical, resolved manifest path.
        self.assertEqual(result, path.resolve())
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("developer_calibration", document)
        self.assertFalse(document["cfa_calibration"]["noise_averaged"])
        self.assertEqual(document["calibration"], document["frames"][3]["calibration"])
        for i, frame in enumerate(session.frames.values()):
            entry = document["frames"][i]
            expected = frame.engine_noise()[[3, 2, 1, 0]]
            np.testing.assert_array_equal(entry["calibration_cfa"], expected)
            self.assertEqual(entry["calibration_cfa_order"], "R,G1,G2,B")
            np.testing.assert_array_equal(entry["calibration"], expected[0])
            self.assertEqual(entry["underflow"], before["frames"][i]["underflow"])
            self.assertTrue(Path(entry["raw"]).is_absolute())
            self.assertEqual(hashlib.sha256(frame.source.read_bytes()).hexdigest(), frame.sha256)
        self.assertTrue((path.parent.parent / "CFA_CALIBRATION.json").is_file())

    def test_missing_gpu_adapter_rejects_before_manifest_mutation(self):
        session, path, _ = self.make_manifest()
        before = path.read_bytes()
        with installed_physical(False), self.assertRaisesRegex(CalibrationError, "physical adapter"):
            session.calibrate(path, 0.0, 0.0, 1.0)
        self.assertEqual(path.read_bytes(), before)

    def test_each_frame_keeps_its_own_black_and_noise_model(self):
        for i, row in enumerate(self.payload["frames"]):
            row["black_levels_dn"] = [b + i * 0.1 for b in row["black_levels_dn"]]
            row["noise_s"] = [s * (1 + i * 0.1) for s in row["noise_s"]]
            row["noise_o"] = [o * (1 + i * 0.1) for o in row["noise_o"]]
        session, path, _ = self.make_manifest()
        with installed_physical():
            session.calibrate(path, 0.0, 0.0, 1.0)
        document = json.loads(path.read_text(encoding="utf-8"))
        for i, frame in enumerate(session.frames.values()):
            np.testing.assert_array_equal(document["frames"][i]["calibration_cfa"], frame.engine_noise())
            self.assertEqual(document["raw_records"][i]["black"],
                             np.asarray(frame.black).reshape(2, 2).tolist())
        self.assertEqual(len({entry["calibration_cfa"][0][0] for entry in document["frames"]}), 7)

    def test_manifest_missing_or_mismatched_decode_receipt_is_rejected(self):
        session, path, original = self.make_manifest()
        for mode in ("receipt", "source", "black", "denominator", "cfa", "decoded"):
            with self.subTest(mode=mode):
                document = deepcopy(original)
                session.decoded = set(session.frames)
                if mode == "receipt":
                    document["raw_records"][0].pop("cfa_calibration")
                elif mode == "source":
                    document["frames"][0]["source_id"] = document["frames"][1]["source_id"]
                elif mode == "black":
                    document["raw_records"][0]["black"][0][0] = 64.03
                elif mode == "denominator":
                    document["raw_records"][0]["denominator"][0][0] += 1
                elif mode == "cfa":
                    document["frames"][0]["cfa_index"] = [0, 2, 1, 3]
                else:
                    session.decoded.pop()
                path.write_text(json.dumps(document), encoding="utf-8")
                before = path.read_bytes()
                with installed_physical(), self.assertRaises(CalibrationError):
                    session.calibrate(path, 0.0, 0.0, 1.0)
                self.assertEqual(path.read_bytes(), before)

    def test_no_payload_preserves_original_usage_and_explicit_payload_installs(self):
        developer = ModuleType("developer")
        ptc = ModuleType("ptc")
        old_audit = lambda manifest, selection: {"original": True}
        ptc.audit_raw_units = old_audit
        old_decoder, old_calibrate = object(), object()
        developer.decoder, developer.calibrate = old_decoder, old_calibrate
        with patch.dict(sys.modules, {"developer": developer, "ptc": ptc}):
            self.assertIsNone(install())
            self.assertIs(developer.decoder, old_decoder)
            self.assertIs(developer.calibrate, old_calibrate)
            self.assertIs(ptc.audit_raw_units, old_audit)
            session = install(self.payload)
            self.assertIs(developer.decoder.__self__, session)
            self.assertIs(developer.calibrate.__self__, session)
            self.assertIsNot(ptc.audit_raw_units, old_audit)
            self.assertEqual(ptc.audit_raw_units({"frames": []}, {}), {"original": True})

    def test_unit_audit_verifies_domain_conversion_and_preserves_legacy_provenance(self):
        session, path, _ = self.make_manifest(cfa=(2, 1, 1, 0))
        selection = {"selection": "manual_fullwell", "shot": self.frame.noise_s[0], "prnu": 0.0}
        with installed_physical():
            session.calibrate(path, 0.0, 0.0, 1.0)
            document = json.loads(path.read_text(encoding="utf-8"))
            audit = session.audit_wrapper(lambda *_: self.fail("CFA contract used scalar audit"))(document, selection)
        self.assertEqual(audit["validation"], "passed")
        self.assertEqual(audit["legacy_selection"], selection)
        self.assertEqual(len(audit["frames"]), 7)
        self.assertNotEqual(audit["frames"][0]["model_calibration_cfa"][0][0], selection["shot"])
        for result, frame in zip(audit["frames"], session.frames.values()):
            original_span = np.asarray(frame.white) - np.asarray(frame.black)
            np.testing.assert_allclose(np.asarray(result["shot_dn_phase"]).ravel(),
                                       np.asarray(frame.noise_s) * original_span, rtol=1e-14)
            np.testing.assert_allclose(np.asarray(result["read_plus_adc_dn2_phase"]).ravel(),
                                       np.asarray(frame.noise_o) * original_span ** 2, rtol=1e-14)

    def test_unit_audit_rejects_missing_or_changed_physical_facts(self):
        session, path, _ = self.make_manifest()
        with installed_physical():
            session.calibrate(path, 0.0, 0.0, 1.0)
        original = json.loads(path.read_text(encoding="utf-8"))
        for mode in ("contract", "unconverted_s", "noise_o", "span", "underflow", "order", "source"):
            with self.subTest(mode=mode):
                document = deepcopy(original)
                if mode == "contract":
                    document["cfa_calibration"]["schema"] = "unknown"
                elif mode == "unconverted_s":
                    document["frames"][0]["calibration_cfa"][0][0] = self.frame.noise_s[0]
                elif mode == "noise_o":
                    document["raw_records"][0]["cfa_calibration"]["noise_o"][0] *= 2
                elif mode == "span":
                    document["raw_records"][0]["denominator"][0][0] += 1
                elif mode == "underflow":
                    document["frames"][0]["underflow"][0] = 0
                elif mode == "order":
                    document["frames"][0]["calibration_cfa_order"] = "B,G2,G1,R"
                else:
                    document["frames"][0]["source_id"] = document["frames"][1]["source_id"]
                with installed_physical(), self.assertRaises(CalibrationError):
                    session.audit_wrapper(lambda *_: self.fail("invalid CFA contract fell back to scalar audit"))(document, {})


if __name__ == "__main__":
    unittest.main()
