"""Neutral display rendering for the original JSR 18.3.1 seven-frame engine.

This is an independently authored adapter.  ``install`` changes only three
callables: v124_dng.baseline_exposure (original line 12),
v124_dng.dng_thumbnail (line 131), and output_chain._display_rgb (line 28).
It also corrects one audit-text constant in write_fullwell_dng16 (line 51).
The original DNG writer's bytecode, camera-linear pixels, and profile tags are
unchanged.  The original thumbnail_matrix supplies the as-shot color transform
from ColorMatrix1, CameraCalibration1, AnalogBalance and AsShotNeutral.

Both display routes start from the same clipped, rounded 16-bit camera samples
that the DNG writer stores.  Clipping precedes reduction, so a small saturated
highlight cannot brighten its neighbours by being averaged before clipping.
INTER_AREA on uint16 adds at most half a DN of rounding during reduction and
avoids allocating a second full-resolution floating-point image.

The result is an sRGB matrix preview, without Adobe look tables or tone curves.
Adobe DNG Converter may regenerate a thumbnail; Camera Raw/Photoshop can apply
their own profiles and rendering.  Neither is promised to match this preview.
Linear TIFF remains a camera-linear export; it is not an sRGB display image.
"""

import importlib
import math

import numpy as np


UINT16_MAX = 65535
RENDERING_POLICY = "neutral-camera-srgb-v1"
BASELINE_POLICY = "-log2(encoding_gain); reference-camera units"
_LEGACY_BASELINE_POLICY = "2 EV - log2(encoding_gain); reference-camera units"


def _positive_gain(encoding_gain):
    gain = float(encoding_gain)
    if not math.isfinite(gain) or gain <= 0:
        raise ValueError("DNG encoding gain must be finite and positive")
    return gain


def neutral_baseline_exposure(encoding_gain):
    """Undo container scaling, without the original additional +2 EV.

    Keep the engine's two-decimal metadata convention: for an unclipped sample,
    g * 2**baseline differs from unity by no more than 0.005 EV.  A literal zero
    would be incorrect whenever the raw container uses a non-unit gain.
    """
    return round(-math.log2(_positive_gain(encoding_gain)), 2)


def _rgb_array(rgb):
    array = np.asarray(rgb)
    if array.ndim != 3 or array.shape[2] != 3 or min(array.shape[:2]) < 1:
        raise ValueError("Camera RGB must be a non-empty H x W x 3 array")
    if not np.issubdtype(array.dtype, np.number) or np.iscomplexobj(array):
        raise ValueError("Camera RGB must contain real numbers")
    return array


def encode_camera_u16(rgb, encoding_gain, *, block_rows=128):
    """Mirror the existing DNG quantizer for display only; never write a file.

    The engine writes float32(rgb) * float32(65535 * gain), clipped and rounded
    to uint16.  Only a strip-sized float32 temporary is needed here.  The input
    array is not modified.  Invalid values fail before they can become a black
    pixel through an unsafe integer cast.
    """
    source = _rgb_array(rgb)
    gain = _positive_gain(encoding_gain)
    if not isinstance(block_rows, int) or block_rows < 1:
        raise ValueError("block_rows must be a positive integer")
    scale64 = UINT16_MAX * gain
    if not math.isfinite(scale64) or scale64 > np.finfo(np.float32).max:
        raise ValueError("DNG encoding gain exceeds float32 quantizer range")
    scale = np.float32(scale64)
    if scale <= 0:
        raise ValueError("DNG encoding gain is below float32 quantizer range")
    encoded = np.empty(source.shape, dtype=np.uint16)
    for start in range(0, source.shape[0], block_rows):
        band = np.asarray(source[start:start + block_rows], dtype=np.float32)
        if not np.isfinite(band).all():
            raise ValueError("Camera RGB must contain finite values")
        with np.errstate(over="ignore"):
            samples = band * scale
        np.clip(samples, 0, UINT16_MAX, out=samples)
        np.rint(samples, out=samples)
        encoded[start:start + block_rows] = samples.astype(np.uint16)
    return encoded


def linear_to_srgb(linear):
    """Encode display-linear sRGB, clipping out-of-gamut display components."""
    values = np.asarray(linear, dtype=np.float64)
    if not np.isfinite(values).all():
        raise ValueError("Display RGB must contain finite values")
    values = np.clip(values, 0.0, 1.0)
    return np.where(values <= 0.0031308,
                    12.92 * values,
                    1.055 * np.power(values, 1.0 / 2.4) - 0.055)


def render_camera_rgb(rgb, camera_to_srgb, *, encoding_gain=1.0,
                      baseline=None, max_side=2560, bits=8,
                      white_level=UINT16_MAX):
    """Render camera-linear RGB through one quantization/color/EV convention.

    ``camera_to_srgb`` must already incorporate the actual reference camera's
    as-shot neutral.  In the engine both adapters obtain it from the unchanged
    original DNG profile helper.  Tests may supply a known analytical matrix.
    The returned RGB array is uint8 or uint16, with no implicit BGR conversion.
    """
    gain = _positive_gain(encoding_gain)
    baseline = neutral_baseline_exposure(gain) if baseline is None else float(baseline)
    if not math.isfinite(baseline):
        raise ValueError("Baseline exposure must be finite")
    if bits not in (8, 16):
        raise ValueError("Display bit depth must be 8 or 16")
    side = float(max_side)
    if not math.isfinite(side) or side <= 0:
        raise ValueError("max_side must be finite and positive")
    white = float(white_level)
    if not math.isfinite(white) or not 0 < white <= UINT16_MAX:
        raise ValueError("Invalid DNG thumbnail white")
    matrix = np.asarray(camera_to_srgb, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("Camera-to-sRGB matrix must be finite and 3 x 3")
    try:
        exposure = 2.0 ** baseline
    except OverflowError as exc:
        raise ValueError("Baseline exposure is outside display range") from exc
    if not math.isfinite(exposure) or exposure <= 0:
        raise ValueError("Baseline exposure is outside display range")

    camera = encode_camera_u16(rgb, gain)
    height, width = camera.shape[:2]
    ratio = min(1.0, side / max(height, width))
    target = (max(1, round(width * ratio)), max(1, round(height * ratio)))
    if target != (width, height):
        import cv2
        camera = cv2.resize(camera, target, interpolation=cv2.INTER_AREA)
    camera = np.clip(camera.astype(np.float64) / white, 0.0, 1.0)
    linear = (camera @ matrix.T) * exposure
    display = linear_to_srgb(linear)
    display_white = (1 << int(bits)) - 1
    return np.rint(display * display_white).astype(np.uint16 if bits == 16 else np.uint8)


def _profile_matrix(profile):
    # Retain the original tag parsing, camera calibration and Bradford adapter.
    # Both output paths must use this same helper, including AsShotNeutral.
    return importlib.import_module("v124_dng").thumbnail_matrix(profile)


def dng_thumbnail(rgb, profile, *, encoding_gain, baseline,
                  max_side=256, white_level=UINT16_MAX):
    """Drop-in replacement with the original DNG thumbnail signature."""
    return render_camera_rgb(rgb, _profile_matrix(profile),
                             encoding_gain=encoding_gain, baseline=baseline,
                             max_side=max_side, bits=8, white_level=white_level)


def _display_rgb(rgb, profile, max_side=2560, bits=8, encoding_gain=1.0):
    """Drop-in replacement for standalone RGB preview/export rendering."""
    gain = _positive_gain(encoding_gain)
    baseline = neutral_baseline_exposure(gain)
    display = render_camera_rgb(rgb, _profile_matrix(profile),
                                encoding_gain=gain, baseline=baseline,
                                max_side=max_side, bits=bits)
    return display, {
        "curve": "DNG uint16 samples + as-shot camera matrix + neutral baseline + sRGB",
        "rendering_policy": RENDERING_POLICY,
        "encoding_gain": gain,
        "baseline_exposure_ev": baseline,
        "baseline_exposure_policy": BASELINE_POLICY,
        "extra_exposure_ev": 0.0,
        "shape": list(display.shape),
        "bits_per_sample": int(bits),
        "camera_white_balance_baked": True,
        "clipping_before_resize": True,
        "adobe_rendering_match_claimed": False,
    }


def install():
    """Install before the original entry point imports its processing route.

    Import and validate both modules before changing either.  Repeated calls
    are idempotent.  No image is opened, processed or rewritten by installation.
    """
    dng_module = importlib.import_module("v124_dng")
    output_module = importlib.import_module("output_chain")
    expected = ((dng_module, "baseline_exposure"),
                (dng_module, "thumbnail_matrix"),
                (dng_module, "dng_thumbnail"),
                (output_module, "_display_rgb"),
                (output_module, "write_fullwell_dng16"))
    if any(not callable(getattr(module, name, None)) for module, name in expected):
        raise RuntimeError("Unsupported JSR rendering module contract")
    writer = output_module.write_fullwell_dng16
    code = getattr(writer, "__code__", None)
    if code is None:
        raise RuntimeError("Unsupported JSR DNG writer audit contract")
    constants = code.co_consts
    old_count = sum(isinstance(item, str) and item == _LEGACY_BASELINE_POLICY
                    for item in constants)
    new_count = sum(isinstance(item, str) and item == BASELINE_POLICY
                    for item in constants)
    if (old_count, new_count) == (1, 0):
        constants = tuple(BASELINE_POLICY if isinstance(item, str)
                          and item == _LEGACY_BASELINE_POLICY else item
                          for item in constants)
        audit_code = code.replace(co_consts=constants)
    elif (old_count, new_count) == (0, 1):
        audit_code = code  # Already installed.
    else:
        raise RuntimeError("Unsupported JSR DNG writer baseline audit text")
    # Only a literal in the sidecar audit changes; instructions and nested
    # strip-writing code objects are byte-for-byte the original objects.
    writer.__code__ = audit_code
    dng_module.baseline_exposure = neutral_baseline_exposure
    dng_module.dng_thumbnail = dng_thumbnail
    output_module._display_rgb = _display_rgb
    return {
        "rendering_policy": RENDERING_POLICY,
        "patched_functions": ["v124_dng.baseline_exposure",
                              "v124_dng.dng_thumbnail", "output_chain._display_rgb"],
        "audit_text_corrected": "output_chain.write_fullwell_dng16.baseline_exposure_policy",
        "extra_exposure_ev": 0.0,
        "dng_quantizer_changed": False,
    }
