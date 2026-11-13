"""Exact DNG CFA calibration adapter for the pinned seven-frame engine.

This module is authored glue.  It does not contain decompiled vendor code.
The stock Windows decoder supplies uint16 Bayer samples normalized using its
native black levels.  We recover those integer DN values, then apply the
original DNG's four black levels and four noise models independently.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

import numpy as np


SCHEMA = "JSR_CFA_CALIBRATION_V1"
WHITE_HEADROOM_DN = 5.0
# Float32 normalization of any uint16 DN incurs < .01 DN round-trip error.
# A larger discrepancy indicates that this is not the pinned integer RAW path.
MAX_INTEGER_RECOVERY_ERROR_DN = 0.02


class CalibrationError(ValueError):
    """Calibration payload or native-decoder contract is inconsistent."""


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalibrationError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise CalibrationError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise CalibrationError(f"{label} must be a finite number")
    return result


def _integer(value: Any, label: str, lower: int, upper: int) -> int:
    result = _finite(value, label)
    if result != int(result) or not lower <= result <= upper:
        raise CalibrationError(f"{label} is outside the supported integer range")
    return int(result)


def _four(value: Any, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise CalibrationError(f"{label} requires four row-major CFA values")
    return tuple(_finite(v, label) for v in value)


def _array_equal(value: Any, expected: np.ndarray, label: str) -> np.ndarray:
    """Audit JSON numeric arrays before they are packed down to float32."""
    try:
        actual = np.asarray(value, dtype=np.float64)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CalibrationError(f"Invalid {label}") from exc
    if (actual.shape != expected.shape or not np.isfinite(actual).all() or
            not np.allclose(actual, expected, rtol=1e-12, atol=0)):
        raise CalibrationError(f"{label} does not match verified DNG CFA calibration")
    return actual


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _stamp(path: Path) -> tuple[int, int, int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _digest(path: Path) -> tuple[str, tuple[int, int, int, int]]:
    before = _stamp(path)
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    if _stamp(path) != before:
        raise CalibrationError(f"Input changed during verification: {path.name}")
    return h.hexdigest(), before


def _unique_keys(items: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise CalibrationError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict:
    if path.stat().st_size > 1024 * 1024:
        raise CalibrationError("Calibration/manifest JSON is too large")
    result = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_keys)
    if not isinstance(result, dict):
        raise CalibrationError("Calibration/manifest must be a JSON object")
    return result


def _write_json(path: Path, value: dict) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, name = tempfile.mkstemp(prefix=".cfa-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass(frozen=True)
class FrameCalibration:
    source: Path
    sha256: str
    stamp: tuple[int, int, int, int]
    width: int
    height: int
    cfa_pattern: tuple[int, ...]
    black: tuple[float, ...]
    white: tuple[float, ...]
    noise_s: tuple[float, ...]
    noise_o: tuple[float, ...]
    iso: int
    shutter_seconds: float

    def unchanged(self) -> None:
        if _stamp(self.source) != self.stamp:
            raise CalibrationError(f"Input changed after verification: {self.source.name}")

    def engine_noise(self) -> np.ndarray:
        black = np.asarray(self.black, dtype=np.float64)
        original_span = np.asarray(self.white, dtype=np.float64) - black
        working_span = original_span - WHITE_HEADROOM_DN
        scale = original_span / working_span
        result = np.zeros((4, 4), dtype=np.float64)
        result[:, 0] = np.asarray(self.noise_s) * scale
        result[:, 1] = np.asarray(self.noise_o) * scale * scale
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            packed = result.astype(np.float32)
        if (not np.isfinite(packed).all() or np.any(packed[:, 0] <= 0) or
                np.any((result[:, 1] > 0) & (packed[:, 1] == 0))):
            raise CalibrationError(f"Noise model exceeds float32 range: {self.source.name}")
        return result


def load_payload(payload: dict | str | Path) -> dict[str, FrameCalibration]:
    if isinstance(payload, (str, Path)):
        payload = _read_json(Path(payload).resolve(strict=True))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise CalibrationError(f"Expected {SCHEMA} payload")
    rows = payload.get("frames")
    if not isinstance(rows, list) or len(rows) != 7:
        raise CalibrationError("CFA calibration requires exactly seven frames")
    frames: dict[str, FrameCalibration] = {}
    digests: set[str] = set()
    first: FrameCalibration | None = None
    for row in rows:
        if not isinstance(row, dict):
            raise CalibrationError("Every frame calibration must be an object")
        source_text = row.get("source")
        if not isinstance(source_text, str) or not Path(source_text).is_absolute():
            raise CalibrationError("Calibration source must be an absolute DNG path")
        source = Path(source_text).resolve(strict=True)
        if not source.is_file() or source.suffix.lower() != ".dng":
            raise CalibrationError(f"Calibration source is not a DNG file: {source}")
        key = _key(source)
        if key in frames:
            raise CalibrationError(f"Duplicate input path: {source.name}")
        expected_hash = row.get("sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
            raise CalibrationError(f"Missing/invalid input SHA-256: {source.name}")
        actual_hash, stamp = _digest(source)
        if expected_hash.lower() != actual_hash:
            raise CalibrationError(f"Input SHA-256 mismatch: {source.name}")
        if actual_hash in digests:
            raise CalibrationError(f"Duplicate input content: {source.name}")
        width = _integer(row.get("width"), "width", 2, 32768)
        height = _integer(row.get("height"), "height", 2, 32768)
        if width % 2 or height % 2 or width * height > 200_000_000:
            raise CalibrationError("CFA image requires bounded even dimensions")
        cfa = tuple(_integer(v, "CFA color", 0, 2) for v in _four(row.get("cfa_pattern"), "CFA pattern"))
        if sorted(cfa) != [0, 1, 1, 2] or cfa.index(0) ^ cfa.index(2) != 3:
            raise CalibrationError("CFA pattern must be a 2x2 Bayer lattice")
        black = _four(row.get("black_levels_dn"), "BlackLevel")
        white = _four(row.get("white_levels_dn"), "WhiteLevel")
        if any(not 0 <= b < w - WHITE_HEADROOM_DN or w > 65535 for b, w in zip(black, white)):
            raise CalibrationError("Black/white levels leave no positive range after native headroom")
        noise_s = _four(row.get("noise_s"), "NoiseProfile S")
        noise_o = _four(row.get("noise_o"), "NoiseProfile O")
        if any(not 0 < v <= 1 for v in noise_s) or any(not 0 <= v < 1 for v in noise_o):
            raise CalibrationError("NoiseProfile coefficients are outside the supported range")
        iso = _integer(row.get("iso"), "ISO", 1, 1_000_000)
        shutter = _finite(row.get("shutter_seconds"), "shutter_seconds")
        if not 0 < shutter <= 3600:
            raise CalibrationError("Shutter time is outside the supported range")
        frame = FrameCalibration(source, actual_hash, stamp, width, height, cfa,
                                 black, white, noise_s, noise_o, iso, shutter)
        frame.engine_noise()  # Validate normalized coefficients before decoding.
        if first is not None:
            if (width, height, cfa, iso) != (first.width, first.height, first.cfa_pattern, first.iso):
                raise CalibrationError("Frames must use one Bayer mode and one ISO")
            if not math.isclose(shutter, first.shutter_seconds, rel_tol=1e-6, abs_tol=1e-12):
                raise CalibrationError("Frames must use one shutter time")
        first = first or frame
        frames[key] = frame
        digests.add(actual_hash)
    return frames


def _phase_index(metadata: dict, frame: FrameCalibration) -> np.ndarray:
    value = metadata.get("cfa_index")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise CalibrationError("Native decoder did not supply its CFA ordering")
    index = np.asarray([_integer(v, "native CFA index", 0, 3) for v in value], dtype=np.int32)
    red, blue = frame.cfa_pattern.index(0), frame.cfa_pattern.index(2)
    if index.tolist() != [red, red ^ 1, blue ^ 1, blue]:
        raise CalibrationError("DNG/native CFA patterns disagree")
    return index


def normalize_native(raw: np.ndarray, metadata: dict, frame: FrameCalibration) -> tuple[np.ndarray, dict]:
    """Replace native integer black levels with exact DNG phase levels."""
    raw = np.asarray(raw)
    if raw.ndim != 2 or raw.shape != (frame.height, frame.width) or not np.issubdtype(raw.dtype, np.floating):
        raise CalibrationError("Native decoder shape/type does not match the calibrated DNG")
    if metadata.get("source_hw", metadata.get("native_hw")) != [frame.height, frame.width]:
        raise CalibrationError("Native decoder source dimensions do not match payload")
    crop = metadata.get("crop_xywh", [0, 0, frame.width, frame.height])
    if crop != [0, 0, frame.width, frame.height]:
        raise CalibrationError("Cropped CFA origins require a separate calibration mapping")
    index = _phase_index(metadata, frame)
    old_black = np.asarray(metadata.get("black"), dtype=np.float64)
    old_span = np.asarray(metadata.get("denominator"), dtype=np.float64)
    if (old_black.shape != (2, 2) or old_span.shape != (2, 2) or
            not np.isfinite(old_black).all() or not np.isfinite(old_span).all() or np.any(old_span <= 0)):
        raise CalibrationError("Native decoder black/white calibration is invalid")
    exact_black = np.asarray(frame.black, dtype=np.float64).reshape(2, 2)
    exact_white = np.asarray(frame.white, dtype=np.float64).reshape(2, 2)
    working_white = exact_white - WHITE_HEADROOM_DN
    span = working_white - exact_black
    result = np.empty(raw.shape, dtype=np.float32)
    maximum_recovery_error = 0.0
    for y in range(2):
        for x in range(2):
            dn_float = raw[y::2, x::2].astype(np.float64) * old_span[y, x] + old_black[y, x]
            if not np.isfinite(dn_float).all():
                raise CalibrationError("Native RAW contains non-finite samples")
            dn = np.rint(dn_float)
            error = float(np.max(np.abs(dn_float - dn)))
            if error > MAX_INTEGER_RECOVERY_ERROR_DN or np.any(dn < 0) or np.any(dn > 65535):
                raise CalibrationError("Native RAW is not the expected uint16 normalization path")
            maximum_recovery_error = max(maximum_recovery_error, error)
            result[y::2, x::2] = ((dn - exact_black[y, x]) / span[y, x]).astype(np.float32)
    if not np.isfinite(result).all():
        raise CalibrationError("DNG normalization exceeds float32 range")
    updated = dict(metadata)
    updated.update(
        original_decoder_black=old_black.tolist(),
        original_native_decoder_white=metadata.get("original_decoder_white"),
        original_native_denominator=old_span.tolist(),
        original_dng_black=exact_black.tolist(),
        original_dng_white=exact_white.tolist(),
        black=exact_black.tolist(), white=working_white.tolist(), denominator=span.tolist(),
        original_decoder_white=exact_white.tolist(), white_headroom_dn=WHITE_HEADROOM_DN,
        black_source="exact DNG per-CFA BlackLevel", iso=frame.iso,
        exposure_seconds=frame.shutter_seconds,
        cfa_calibration={
            "schema": SCHEMA, "source_sha256": frame.sha256,
            "phase_order": "row-major 2x2 Bayer", "black_levels_dn": list(frame.black),
            "white_levels_dn": list(frame.white), "noise_s": list(frame.noise_s),
            "noise_o": list(frame.noise_o), "engine_noise_row_major": frame.engine_noise().tolist(),
            "canonical_cfa_index": index.tolist(),
            "uint16_recovery_max_error_dn": maximum_recovery_error,
            "black_averaged": False,
        },
    )
    return result, updated


class CalibrationSession:
    def __init__(self, payload: dict | str | Path):
        self.frames = load_payload(payload)
        self.decoded: set[str] = set()

    def lookup(self, path: str | Path) -> FrameCalibration:
        key = _key(Path(path).resolve(strict=True))
        if key not in self.frames:
            raise CalibrationError(f"RAW is absent from calibration payload: {path}")
        return self.frames[key]

    def decoder(self, original: Callable, _compatibility_black: float) -> Callable:
        def decode(path: str | Path):
            frame = self.lookup(path)
            frame.unchanged()
            raw, metadata = original(path)
            frame.unchanged()
            result = normalize_native(raw, metadata, frame)
            self.decoded.add(_key(frame.source))
            return result
        return decode

    def calibrate(self, manifest: str | Path, _rn: float, _black: float, _fullwell: float) -> Path:
        from v15_2 import physical
        if getattr(physical, "_JSR_CFA_CALIBRATION_V1", False) is not True:
            raise CalibrationError("Exact CFA calibration requires the installed physical adapter")
        path = Path(manifest).resolve(strict=True)
        document = _read_json(path)
        frames, records = document.get("frames"), document.get("raw_records")
        if (not isinstance(frames, list) or not isinstance(records, list) or
                len(frames) != 7 or len(records) != 7 or self.decoded != set(self.frames)):
            raise CalibrationError("Manifest requires all seven exactly calibrated decodes")
        seen: set[str] = set()
        audit = []
        for entry, metadata in zip(frames, records):
            if not isinstance(entry, dict) or not isinstance(metadata, dict):
                raise CalibrationError("Malformed frame/RAW metadata in manifest")
            frame = self.lookup(metadata.get("source", ""))
            key = _key(frame.source)
            if key in seen or self.lookup(entry.get("source_id", "")).source != frame.source:
                raise CalibrationError("Manifest source identities do not match calibration")
            frame.unchanged()
            native_audit = metadata.get("cfa_calibration", {})
            if native_audit.get("schema") != SCHEMA or native_audit.get("source_sha256") != frame.sha256:
                raise CalibrationError("Manifest is missing the exact CFA decode receipt")
            if metadata.get("black") != np.asarray(frame.black).reshape(2, 2).tolist():
                raise CalibrationError("Manifest black levels changed after exact CFA decode")
            index = _phase_index(metadata, frame)
            white = np.asarray(frame.white).reshape(2, 2) - WHITE_HEADROOM_DN
            span = white - np.asarray(frame.black).reshape(2, 2)
            if metadata.get("white") != white.tolist() or metadata.get("denominator") != span.tolist():
                raise CalibrationError("Manifest normalization changed after exact CFA decode")
            if entry.get("cfa_index") != index.tolist():
                raise CalibrationError("Manifest/native CFA order changed after exact decode")
            calibration = frame.engine_noise()[index]
            # The scalar entry only satisfies legacy structure validators. The
            # physical adapter must consume calibration_cfa for all GPU work.
            entry["calibration"] = calibration[0].tolist()
            entry["calibration_cfa"] = calibration.tolist()
            entry["calibration_cfa_order"] = "R,G1,G2,B"
            raw_path = Path(entry.get("raw", ""))
            raw_path = (path.parent / raw_path).resolve(strict=True)
            if raw_path.suffix.lower() != ".npy" or not raw_path.is_file():
                raise CalibrationError("Manifest RAW array is missing")
            entry["raw"] = str(raw_path)
            seen.add(key)
            audit.append({"source": str(frame.source), **native_audit,
                          "calibration_cfa": calibration.tolist()})
        if seen != set(self.frames):
            raise CalibrationError("Manifest/payload frame sets differ")
        reference = _integer(document.get("reference_index"), "reference index", 0, 6)
        document["calibration"] = frames[reference]["calibration"]
        document["cfa_calibration"] = {
            "schema": SCHEMA, "enabled": True, "black_averaged": False,
            "noise_averaged": False, "white_headroom_dn": WHITE_HEADROOM_DN,
            "noise_units": "shot/read variance after per-CFA white-minus-black normalization",
            "scalar_calibration_role": "legacy validation placeholder; CFA adapter required",
            "frames": audit,
        }
        # Do not let downstream reporting describe this as scalar manual DN.
        document.pop("developer_calibration", None)
        _write_json(path, document)
        _write_json(path.parent.parent / "CFA_CALIBRATION.json", document["cfa_calibration"])
        return path

    def audit_raw_units(self, manifest: dict, selection: dict) -> dict:
        """Verify the exact CFA contract, including the DNG-to-working domain change.

        ``selection`` is the old parser's scalar FWC selection. It is retained
        as provenance, but is not the physical model sent to the GPU.
        """
        from v15_2 import physical
        if getattr(physical, "_JSR_CFA_CALIBRATION_V1", False) is not True:
            raise CalibrationError("Exact CFA unit audit requires the installed physical adapter")
        contract = manifest.get("cfa_calibration", {})
        if (not isinstance(contract, dict) or contract.get("schema") != SCHEMA or
                contract.get("enabled") is not True or contract.get("black_averaged") is not False or
                contract.get("noise_averaged") is not False or
                contract.get("white_headroom_dn") != WHITE_HEADROOM_DN):
            raise CalibrationError("Missing or inconsistent exact CFA unit contract")
        frames, records = manifest.get("frames"), manifest.get("raw_records")
        if not isinstance(frames, list) or not isinstance(records, list) or len(frames) != 7 or len(records) != 7:
            raise CalibrationError("CFA unit audit requires seven frame/RAW metadata pairs")
        result, seen = [], set()
        for number, (entry, metadata) in enumerate(zip(frames, records)):
            if not isinstance(entry, dict) or not isinstance(metadata, dict):
                raise CalibrationError("Invalid frame/RAW metadata for CFA unit audit")
            frame = self.lookup(metadata.get("source", ""))
            key = _key(frame.source)
            if key in seen or self.lookup(entry.get("source_id", "")).source != frame.source:
                raise CalibrationError("CFA unit audit source identities do not match payload")
            frame.unchanged()
            seen.add(key)
            index = _phase_index(metadata, frame)
            if entry.get("cfa_index") != index.tolist() or entry.get("calibration_cfa_order") != "R,G1,G2,B":
                raise CalibrationError("CFA unit audit phase order does not match native geometry")
            black = np.asarray(frame.black).reshape(2, 2)
            declared_white = np.asarray(frame.white).reshape(2, 2)
            white = declared_white - WHITE_HEADROOM_DN
            span, original_span = white - black, declared_white - black
            for field, expected in (("black", black), ("white", white), ("denominator", span),
                                    ("original_dng_black", black), ("original_dng_white", declared_white),
                                    ("original_decoder_white", declared_white)):
                _array_equal(metadata.get(field), expected, field)
            if metadata.get("white_headroom_dn") != WHITE_HEADROOM_DN:
                raise CalibrationError("CFA white headroom changed after decode")
            receipt = metadata.get("cfa_calibration", {})
            if (not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA or
                    receipt.get("source_sha256") != frame.sha256 or receipt.get("black_averaged") is not False or
                    receipt.get("canonical_cfa_index") != index.tolist()):
                raise CalibrationError("CFA unit audit decode receipt is missing or inconsistent")
            row_major = frame.engine_noise()
            for field, expected in (("black_levels_dn", np.asarray(frame.black)),
                                    ("white_levels_dn", np.asarray(frame.white)),
                                    ("noise_s", np.asarray(frame.noise_s)), ("noise_o", np.asarray(frame.noise_o)),
                                    ("engine_noise_row_major", row_major)):
                _array_equal(receipt.get(field), expected, "decode receipt " + field)
            recovery = _finite(receipt.get("uint16_recovery_max_error_dn"), "uint16 recovery error")
            if not 0 <= recovery <= MAX_INTEGER_RECOVERY_ERROR_DN:
                raise CalibrationError("CFA decode receipt violates integer recovery tolerance")
            canonical = _array_equal(entry.get("calibration_cfa"), row_major[index], "working CFA noise")
            _array_equal(entry.get("calibration"), canonical[0], "legacy scalar placeholder")
            _array_equal(entry.get("underflow"), (-black / span).ravel()[index], "RAW underflow")
            if _finite(entry.get("white"), "normalized white") != 1:
                raise CalibrationError("CFA normalized saturation threshold must be one")
            if not math.isclose(_finite(entry.get("exposure"), "relative exposure"), 1, rel_tol=1e-12):
                raise CalibrationError("Same-ISO/shutter CFA payload requires equal relative exposures")
            # Independently verify that the two normalization domains describe
            # the same DN-space variance model, for every phase and frame.
            shot_dn = row_major[:, 0].reshape(2, 2) * span
            floor_dn2 = row_major[:, 1].reshape(2, 2) * span ** 2
            _array_equal(shot_dn, np.asarray(frame.noise_s).reshape(2, 2) * original_span,
                         "shot variance DN-domain invariant")
            _array_equal(floor_dn2, np.asarray(frame.noise_o).reshape(2, 2) * original_span ** 2,
                         "read variance DN-domain invariant")
            result.append({
                "frame": number, "source": str(frame.source), "source_sha256": frame.sha256,
                "black_dn_phase": black.tolist(), "white_dn_phase": white.tolist(),
                "declared_white_dn_phase": declared_white.tolist(),
                "decoder_white_headroom_dn": WHITE_HEADROOM_DN, "range_dn_phase": span.tolist(),
                "dng_range_dn_phase": original_span.tolist(),
                "dng_noise_s_phase": list(frame.noise_s), "dng_noise_o_phase": list(frame.noise_o),
                "normalization_scale_phase": (original_span / span).tolist(),
                "model_calibration_cfa": canonical.tolist(), "model_cfa_order": "R,G1,G2,B",
                "native_cfa_index": index.tolist(), "shot_dn_phase": shot_dn.tolist(),
                "read_plus_adc_dn2_phase": floor_dn2.tolist(), "prnu_variance_phase": [0.0] * 4,
                "integer_recovery_max_error_dn": recovery,
            })
        if seen != set(self.frames):
            raise CalibrationError("CFA unit audit frame set differs from the verified payload")
        reference = _integer(manifest.get("reference_index"), "reference index", 0, 6)
        _array_equal(manifest.get("calibration"), np.asarray(frames[reference]["calibration"]),
                     "manifest scalar placeholder")
        if not isinstance(selection, dict):
            raise CalibrationError("Legacy calibration selection audit must be an object")
        return {
            "contract": SCHEMA, "validation": "passed",
            "noise_source": "original DNG NoiseProfile metadata; not measured PTC",
            "normalization": "S_work=S_dng*(W-B)/(W-5-B); O_work=O_dng*((W-B)/(W-5-B))^2",
            "frames": result, "phase_order": "row-major native CFA; model rows are R,G1,G2,B",
            "black_averaged": False, "noise_averaged": False, "dark_floor_normalized": None,
            "legacy_selection_role": "CLI compatibility placeholder; not GPU calibration",
            "legacy_selection": {k: v for k, v in selection.items() if k != "raw_units"},
        }

    def audit_wrapper(self, original: Callable) -> Callable:
        def audit_raw_units(manifest: dict, selection: dict):
            if ("cfa_calibration" in manifest or
                    any("calibration_cfa" in frame for frame in manifest.get("frames", []) if isinstance(frame, dict))):
                return self.audit_raw_units(manifest, selection)
            return original(manifest, selection)
        return audit_raw_units


def install(payload: dict | str | Path | None = None) -> CalibrationSession | None:
    """Install only for an explicit verified payload; stock usage is untouched."""
    if payload is None:
        return None
    session = CalibrationSession(payload)
    import developer
    import ptc
    developer.decoder = session.decoder
    developer.calibrate = session.calibrate
    ptc.audit_raw_units = session.audit_wrapper(ptc.audit_raw_units)
    return session
