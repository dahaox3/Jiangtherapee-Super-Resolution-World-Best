"""Per-CFA noise propagation for the pinned JSR 18.3.1 K7 engine.

This is an authored extension, not a replacement reconstruction network.  The
first thirteen metadata floats retain their original meaning.  Four independent
S/O/Q/PRNU rows follow them, in canonical R/G1/G2/B order (the same order as
entry['index']).  Scalar legacy calibrations are repeated, never re-estimated.
"""
from __future__ import annotations

import functools
import hashlib
import importlib
import math
import re
import sys

import numpy as np

METADATA_STRIDE = 29
CFA_ORDER = ("R", "G1", "G2", "B")
_INSTALLED = False
_ORIGINALS = {}

# Both executable bytecode and all immediate string constants are pinned.
# In particular, changing a WGSL constant without changing its Python bytecode
# must not silently pass this compatibility check.
_GUARDS = {
    "v15_2.ops:Ops.emit": ("c4f8bb6288e024fa08b9761b79ab712e22192d46ba4943ebaa75b844ae26de56", "274eeb19e35d0ed2bbc9867590521b8f571a530f30955bd0f2678bd99923aa94"),
    "v15_2.physical:metadata": ("c32e69a4db42a71d6a46586aaf10958bb0aac222ffcd03f08e655874500a0af7", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    "v15_2.physical:Physical.helpers": ("1b8091fe40898a3779d4f7d2b3f611b05164177863370bc1bec17a61b950f4be", "64969bb0107e6c78a51532cbb2699fe4acfcf1a6af76ec498c30ab8b72286428"),
    "v15_2.physical:Physical.prepare": ("de8d2b3a4c938d1a4152b9666239c0cd96a309c39c937abc78e08da7bbac9c0d", "0fa4e7a6388d877b3b675d0f01ba49f35feed6c2ce38fa2651797192d93c1c9d"),
    "v15_2.physical:Physical.merge": ("dec5e727c30f8d4584a71ea9ff997c6b98acfac2f6efd5c2b48f5afa5d9d4ee2", "7bb6c4226332861dc65e28d5657cdb528eb86e5ba43c67962d5a28c8f878eba9"),
    "v15_2.physical:Physical.guard_fully_clipped": ("533eef87c7d7eae649fc1afd3b1c2a6aee84b50a0836e45adaf7e700fb3a6baf", "26f46d4ae504b8e0a47765ed08c9e737673a7a25f0a3ffbedb3afaddfed618b0"),
    "v15_2.physical:Physical.emit": ("d19848cfb6ade57c620719743f0f37e3db57f8f10f49e123f352a147193b2b7c", "45676b101fd4f01f20fba9e0c8b3df38c1e89537c1b4cb223aaf9dd82b34985a"),
    "v15_2.pipeline:load_input": ("0d6251ec76b3a34769b9f1a7a1b649afc0d152ed378ed90c8f6feab18b3275e4", "9039d56e6f646f61f52bb592d5fae74da43210dc0d0894c51c965a054b8bd41d"),
    "v15_2.pipeline:frame_meta": ("918e8ac33d45164b82daa5bf4fb5e3199da82d00ff75afdb3a92819cbb2ffe71", "bf37069559f3eb97b40ed68c5614a61b5b71b0ef95e6d1e38fee84606c996687"),
    "v15_2.registration:guides": ("7c5af6ef250ba9a576e8d16fad201e53f0d4a0c96c35ffff21de40813b08cfa0", "0051945a1cbbe36d553df7d006c8b9a1c785fd899c241e5a336d16d2a399f042"),
    "v15_2.alignment:Proxy.components": ("96cbd31b8775ba4982e38ed0b05095c856e93744bce45ba82af22a27a1787d84", "4c054af160bc8f6e20295e83f677348639f06219d65511bfa8cdba458598621f"),
    "v15_2.scene_evidence:Footprint.stats": ("753e7a9aecc6cc88737aff859886d713759f32fdcf7f2d8bf991e76e1a83322d", "64c592e66cc8ba14d8050184fc8f3cffe0cdfeffbe3d835254bbcfce6f298b9b"),
    "v15_2.scene_evidence:SceneEvidence.preview": ("eb083b7bb78db4e111e7eda32c614f6b96b29cfc5b6dff13f5336f0916930527", "db02135812209f3019f0372220937bbf47f86f27f7b654aacae742a663a426a4"),
    "v15_2.scene_consistency.evidence:PairEvidence.stats": ("529a6434c70d4c6ff4c01210f48544212432106d8ef6f4adf5640e6fc045ac46", "258201038a7c53edeb6ff5d2476542fa5ebe8dba8d49c66e197515a3f3310218"),
    "v15_2.scene_consistency.physical:ConsistentPhysical.emit": ("3ab50a0a27982eecd80b2c69b30df09a3752276be0e9b8538f4b86697274e3a8", "b71e5ad732019c808433ff65d02448c9bca3428da2d601a56813bb97ce813762"),
}

_OWNERS = {
    ("v15_2.physical", "Physical"),
    ("v15_2.alignment", "Proxy"),
    ("v15_2.scene_evidence", "Footprint"),
    ("v15_2.scene_evidence", "SceneEvidence"),
    ("v15_2.scene_consistency.evidence", "PairEvidence"),
    ("v15_2.scene_consistency.physical", "ConsistentPhysical"),
}
_STRIDE = re.compile(r"\b(k|j|other)\*17u\b")
_READS_OLD_NOISE = re.compile(r"a2\[(?:k\*29u\+|j\+)1[3-6]u\]|a2\[1[3-6]\]")
_NOISE_DIRECT = "a2[k*17u+13u]*vv+a2[k*17u+14u]+a2[k*17u+15u]+a2[k*17u+16u]*vv*vv"
_NOISE_PAIR = "a2[j+13u]*vv+a2[j+14u]+a2[j+15u]+a2[j+16u]*vv*vv"
_NOISE_GUARD = "a2[k*17u+13u]*white+a2[k*17u+14u]+a2[k*17u+15u]+a2[k*17u+16u]*white*white"
_NOISE_PROXY = "a2[13]*vp+a2[14]+a2[15]+a2[16]*vp*vp"
_NOISE_TAGS = {
    "evidence130": (_NOISE_DIRECT, "k*29u+13u+c*4u", "vv"),
    "physical_bandwidth_probe": (_NOISE_PAIR, "j+13u+c*4u", "vv"),
    "pair_common_ceiling_taylor_explicit_v6": (_NOISE_PAIR, "j+13u+c*4u", "vv"),
    "censoring_bound_guard": (_NOISE_GUARD, "k*29u+13u+plane(k,phase)*4u", "white"),
    "proxy_cfa": (_NOISE_PROXY, "13u+c*4u", "vp"),
}
_LAYOUT_ONLY_TAGS = {"seed", "base", "white", "coarse_preview"}


def _coefficients(value, shape=(4, 4)):
    a = np.asarray(value, dtype=np.float64)
    if a.shape != shape or not np.isfinite(a).all() or (a < 0).any():
        raise ValueError(f"calibration_cfa must be finite nonnegative {shape} S/O/Q/PRNU")
    with np.errstate(over="ignore"):
        a = a.astype(np.float32)
    if not np.isfinite(a).all():
        raise ValueError("calibration_cfa cannot be represented as float32")
    return np.ascontiguousarray(a)


def noise_variance(raw, calibration, exposure=1.0, amplitude=1.0):
    """Independent CPU oracle in the engine's normalized sensor units.

    The last calibration axis is S,O,Q,PRNU.  Negative RAW values are retained
    as signal, but only max(raw,0) enters the heteroscedastic variance law.
    """
    cal = np.asarray(calibration, dtype=np.float64)
    if cal.shape[-1:] != (4,) or not np.isfinite(cal).all() or (cal < 0).any():
        raise ValueError("invalid noise coefficients")
    den = np.asarray(exposure, dtype=np.float64) * np.asarray(amplitude, dtype=np.float64)
    if not np.isfinite(den).all() or (den <= 0).any():
        raise ValueError("positive finite exposure and amplitude required")
    v = np.maximum(np.asarray(raw, dtype=np.float64), 0.0)
    return (cal[..., 0] * v + cal[..., 1] + cal[..., 2] + cal[..., 3] * v * v) / (den * den)


def metadata(exposure, index, white, under, calibration, calibration_cfa=None):
    e = np.asarray(exposure, np.float32).reshape(-1)
    k = len(e)
    idx = np.asarray(index).reshape(k, 4)
    if not np.isfinite(e).all() or (e <= 0).any():
        raise ValueError("invalid per-frame exposure")
    if not all(sorted(row.tolist()) == [0, 1, 2, 3] for row in idx):
        raise ValueError("CFA index must map R/G1/G2/B to the four sensor phases")
    if calibration_cfa is None:
        scalar = np.broadcast_to(calibration, (k, 4))
        coeff = np.repeat(scalar[:, None, :], 4, axis=1)
    else:
        coeff = np.asarray(calibration_cfa)
        if coeff.shape == (4, 4) and k == 1:
            coeff = coeff[None, :, :]
    coeff = _coefficients(coeff, (k, 4, 4))
    out = np.c_[e, idx, np.broadcast_to(white, (k, 4)),
                np.broadcast_to(under, (k, 4)), coeff.reshape(k, 16)]
    if not np.isfinite(out).all():
        raise ValueError("non-finite physical metadata")
    return np.ascontiguousarray(out, dtype=np.float32).reshape(-1)


def frame_meta(entry):
    return metadata([entry["exposure"]], np.asarray(entry["index"])[None],
                    np.asarray(entry["white"])[None], np.asarray(entry["under"])[None],
                    entry["calibration"], entry.get("calibration_cfa"))


def attach_cfa(data):
    """Attach manifest noise rows after the original loader has reordered frames."""
    frames = data["manifest"]["frames"]
    order = data["order"]
    entries = data["entries"]
    if len(order) != len(entries) or len(frames) != len(entries):
        raise ValueError("inconsistent physical input frame order")
    contract = data["manifest"].get("cfa_calibration")
    if contract is not None:
        if contract.get("schema") != "JSR_CFA_CALIBRATION_V1" or contract.get("enabled") is not True:
            raise ValueError("unknown or disabled CFA calibration contract")
        if any("calibration_cfa" not in frame for frame in frames):
            raise ValueError("CFA calibration contract requires coefficients for every frame")
    for entry, source_index in zip(entries, order):
        frame = frames[int(source_index)]
        if "calibration_cfa" in frame:
            if frame.get("calibration_cfa_order", "R,G1,G2,B") != "R,G1,G2,B":
                raise ValueError("unsupported CFA calibration row order")
            entry["calibration_cfa"] = _coefficients(frame["calibration_cfa"])
    return data


def _tag_strings(tag):
    if isinstance(tag, str):
        return [tag]
    if isinstance(tag, tuple):
        return [s for member in tag for s in _tag_strings(member)]
    return []


def _variance_wgsl(base, value):
    return (f"a2[{base}]*{value}+a2[{base}+1u]+a2[{base}+2u]"
            f"+a2[{base}+3u]*{value}*{value}")


def rewrite_shader(owner, tag, body, helpers=""):
    """Rewrite only the pinned metadata consumers, after deghost injection.

    CensoredShape's independent 19-word counters also contain a +17u term;
    they intentionally never match this metadata-stride rule.
    """
    combined = body + helpers
    candidate = bool(_STRIDE.search(combined))
    if not candidate:
        return body, helpers, False
    if owner not in _OWNERS:
        raise RuntimeError(f"unrecognized JSR metadata shader owner: {owner}")
    tags = set(_tag_strings(tag))
    recognized = tags & (_LAYOUT_ONLY_TAGS | set(_NOISE_TAGS))
    if len(recognized) != 1:
        raise RuntimeError(f"unrecognized JSR metadata shader tag: {tag!r}")
    kind = recognized.pop()
    if kind in _NOISE_TAGS:
        old, base, value = _NOISE_TAGS[kind]
        if combined.count(old) != 1:
            raise RuntimeError(f"JSR {kind} noise expression changed; refusing partial patch")
        new = _variance_wgsl(base, value)
        body, helpers = body.replace(old, new), helpers.replace(old, new)
    body = _STRIDE.sub(lambda m: m[1] + "*29u", body)
    helpers = _STRIDE.sub(lambda m: m[1] + "*29u", helpers)
    if _STRIDE.search(body + helpers) or _READS_OLD_NOISE.search(body + helpers):
        raise RuntimeError(f"unpatched scalar metadata access in {kind}")
    return body, helpers, True


def correct_guide_variance(data, result, cv2):
    """Update both green phases and the read-noise floor; keep original guides."""
    guides, lattice = result
    h, w = data["h"], data["w"]
    kernel = cv2.getGaussianKernel(5, 0.55)
    for raw, entry, guide in zip(data["raws"], data["entries"], guides):
        if "calibration_cfa" not in entry:
            continue
        cal = _coefficients(entry["calibration_cfa"]).astype(np.float64)
        rh, rw = guide["linear"].shape
        area = rw * rh / ((w // 2) * (h // 2))
        exposure = entry["exposure"]
        variance = np.zeros((rh, rw), np.float32)
        for c in (1, 2):
            phase = int(entry["index"][c])
            positive = np.maximum(np.asarray(raw[phase // 2::2, phase % 2::2]), 0.0)
            pixel_var = (cal[c, 0] * positive + cal[c, 1] + cal[c, 2]
                         + cal[c, 3] * positive ** 2)
            variance += cv2.resize(pixel_var, (rw, rh), interpolation=cv2.INTER_AREA) * (0.25 * area / exposure ** 2)
        guide["variance"] = cv2.sepFilter2D(np.sqrt(variance), -1, kernel, kernel) ** 2
        floor_variance = (cal[1, 1] + cal[1, 2] + cal[2, 1] + cal[2, 2]) * 0.25 * area
        guide["floor"] = max(5 * np.sqrt(floor_variance) / exposure, guide["white"] * 1e-8)
    return guides, lattice


def _digest(fn):
    code = fn.__code__
    h = hashlib.sha256()
    for value in code.co_consts:
        if isinstance(value, str):
            encoded = value.encode("utf8")
            h.update(len(encoded).to_bytes(8, "little"))
            h.update(encoded)
    return hashlib.sha256(code.co_code).hexdigest(), h.hexdigest()


def _resolve(key):
    module, path = key.split(":", 1)
    obj = importlib.import_module(module)
    for name in path.split("."):
        obj = getattr(obj, name)
    return obj


def install():
    """Install once, before backend.product_main imports its local callables."""
    global _INSTALLED
    if _INSTALLED:
        return
    # Validate every relevant producer before making any mutation.
    for key, expected in _GUARDS.items():
        if _digest(_resolve(key)) != expected:
            raise RuntimeError(f"unsupported original JSR engine function: {key}")
    physical = importlib.import_module("v15_2.physical")
    pipeline = importlib.import_module("v15_2.pipeline")
    ops = importlib.import_module("v15_2.ops")
    registration = importlib.import_module("v15_2.registration")
    original_emit = ops.Ops.emit
    original_load = pipeline.load_input
    original_guides = registration.guides
    original_frame_meta = pipeline.frame_meta
    _ORIGINALS.update(emit=original_emit, metadata=physical.metadata,
                      guides=original_guides)

    @functools.wraps(original_load)
    def load_input(path):
        return attach_cfa(original_load(path))

    @functools.wraps(original_guides)
    def guides(data, *args, **kwargs):
        result = original_guides(data, *args, **kwargs)
        return correct_guide_variance(data, result, registration.cv2)

    @functools.wraps(original_emit)
    def emit(self, tag, inputs, shape, body, helpers="", count=None, out=None):
        owner = (type(self).__module__, type(self).__name__)
        body, helpers, changed = rewrite_shader(owner, tag, body, helpers)
        if changed:
            # White is the sole consumer binding metadata at a0; all others a2.
            meta_index = 0 if "white" in _tag_strings(tag) else 2
            size = math.prod(inputs[meta_index].spec.shape)
            if not size or size % METADATA_STRIDE:
                raise RuntimeError("per-CFA shader received a legacy or malformed metadata buffer")
            tag = ("jsr_per_cfa_noise_v1", tag)
        return original_emit(self, tag, inputs, shape, body, helpers, count=count, out=out)

    physical.metadata = metadata
    pipeline.metadata = metadata
    pipeline.frame_meta = frame_meta
    pipeline.load_input = load_input
    registration.guides = guides
    ops.Ops.emit = emit
    # The detector imports frame_meta by value.  Patch any already-loaded alias;
    # future imports will obtain pipeline.frame_meta directly.
    for name, module in tuple(sys.modules.items()):
        if name.startswith("v15_2.") and module is not None:
            if getattr(module, "frame_meta", None) is original_frame_meta:
                module.frame_meta = frame_meta
    physical._JSR_CFA_CALIBRATION_V1 = True
    _INSTALLED = True


def gpu_selftest():
    """Exercise actual shipped Physical and deghost WGSL against a CPU oracle.

    Intended for the isolated bundled-interpreter probe, not a live GUI process.
    It first runs the original scalar ABI, then the identical repeated-CFA model,
    then independently varied CFA coefficients.  No model assets are required.
    """
    install()
    from v15_2.runtime import Runtime
    from v15_2.physical import Physical
    from v15_2.ops import Ops
    from v15_2.scene_evidence import Footprint
    from v15_2.scene_consistency.evidence import PairEvidence
    from v15_2.scene_consistency.physical import ConsistentPhysical

    def check_close(actual, expected, rtol=1e-7, atol=0):
        actual, expected = np.broadcast_arrays(actual, expected)
        difference = np.abs(actual.astype(np.float64) - expected.astype(np.float64))
        if (not np.isfinite(actual).all() or not np.isfinite(expected).all()
                or not (difference <= atol + rtol * np.abs(expected)).all()):
            raise AssertionError(f"GPU oracle mismatch: shape={actual.shape}, max_abs={float(difference.max())}")

    k, native, source = 7, 8, 32
    output = 2 * native
    raw_value = np.float32(0.2)
    scalar = np.array([0.002, 0.00003, 0.00001, 0.0002], np.float32)
    factors = np.array([1, 2, 4, 8], np.float32)
    exposures = np.ones(k, np.float32)
    # Vary the spatial CFA mapping between frames: the noise rows must still
    # be indexed by canonical colour, not mistaken for raw-grid phase order.
    indices = np.array([[0, 1, 2, 3], [3, 2, 1, 0], [1, 0, 3, 2],
                        [2, 3, 0, 1], [0, 1, 2, 3], [3, 2, 1, 0],
                        [1, 0, 3, 2]], np.float32)
    white = np.ones((k, 4), np.float32)
    under = np.full((k, 4), -0.1, np.float32)
    bank_array = np.zeros((4, k, source, source), np.float32)
    bank_array[0] = raw_value
    bank_array[1] = 1
    y, x = np.mgrid[:output, :output]
    coordinates = np.stack((8 + x * 0.5, 8 + y * 0.5), axis=-1).astype(np.float32)
    geometry = np.broadcast_to(coordinates, (k, 3, output, output, 2)).copy()
    repeated = np.broadcast_to(scalar, (k, 4, 4)).copy()
    varied = repeated * factors[None, :, None]
    runtime = Runtime(budget_bytes=512 * 1024 * 1024)
    installed_emit = Ops.emit
    try:
        bank = runtime.upload(bank_array)
        geom = runtime.upload(geometry)
        old_meta = runtime.upload(_ORIGINALS["metadata"](exposures, indices, white, under, scalar))
        same_meta = runtime.upload(metadata(exposures, indices, white, under, scalar, repeated))
        varied_meta = runtime.upload(metadata(exposures, indices, white, under, scalar, varied))
        engine = Physical(runtime, native, k, source_hw=(source, source), colours=3)
        Ops.emit = _ORIGINALS["emit"]
        old_values = engine.prepare(bank, geom, old_meta)
        old_arrays = [runtime.read(value).copy() for value in old_values]
        Ops.emit = installed_emit
        same_values = engine.prepare(bank, geom, same_meta)
        same_arrays = [runtime.read(value).copy() for value in same_values]
        for before, after in zip(old_arrays, same_arrays):
            check_close(after, before, rtol=2e-6, atol=2e-7)
        varied_values = engine.prepare(bank, geom, varied_meta)
        evidence, base, amplitude, support, tag_white = varied_values
        ev = runtime.read(evidence)[0]
        amp = runtime.read(amplitude).reshape(output, output)
        for f in range(k):
            for c in range(4):
                expected = np.log1p(noise_variance(raw_value, varied[f, c]) /
                                   amp[1::4, 1::4].astype(np.float64) ** 2)
                for tap in range(4):
                    actual = ev[f, (c * 4 + tap) * 8 + 1]
                    check_close(actual, expected, rtol=3e-4, atol=3e-7)

        # Compile and run the clipping guard, where colour c is RGB but the
        # coefficient row must be plane(k,phase), including the two greens.
        shoulder = Physical(runtime, native, k, source_hw=(source, source),
                            colours=3, shortest_rolloff=0)
        prediction = runtime.upload(np.full((1, 3, output, output), raw_value, np.float32))
        guarded = shoulder.guard_fully_clipped(bank, geom, varied_meta, prediction, tag_white)
        check_close(runtime.read(guarded), raw_value, atol=1e-7)
        gates = runtime.upload(np.zeros((k, 4, 8, 8), np.float32))
        retained = ConsistentPhysical(runtime, native, frames=k, source_hw=(source, source),
                                      colours=3, shortest_rolloff=0, rejection=gates,
                                      query_origin=(8, 8))
        retained_values = retained.prepare(bank, geom, varied_meta)
        for expected, actual in zip(varied_values, retained_values):
            check_close(runtime.read(actual), runtime.read(expected), rtol=3e-6, atol=3e-7)

        # Both deghost evidence families must scale their *actual GPU* variance
        # by the chosen CFA factor, regardless of phase mapping or interpolation.
        side, stride = 6, 2
        yy, xx = np.mgrid[:side, :side]
        sample_coords = np.stack((8 + xx * stride, 8 + yy * stride), axis=-1).astype(np.float32)
        grid = runtime.upload(np.broadcast_to(sample_coords, (k, 4, side, side, 2)).copy())
        gains = runtime.upload(np.ones((k, side, side), np.float32))
        evidence_checks = {}
        for label, obj in (("footprint", Footprint(runtime)),
                           ("pair_deghost", PairEvidence(runtime, gains))):
            same = runtime.read(obj.stats(bank, grid, same_meta, 1.0, stride)).copy()
            different = runtime.read(obj.stats(bank, grid, varied_meta, 1.0, stride)).copy()
            expected = same[:, :, 1] * factors[None, :, None, None]
            check_close(different[:, :, 1], expected, rtol=5e-5, atol=1e-9)
            evidence_checks[label] = True
        return {"scalar_equivalence": True, "per_cfa_evidence_cpu_oracle": True,
                "clipping_guard": True, "deghost_retained_reconstruction": True, **evidence_checks,
                "frames": k, "metadata_stride": METADATA_STRIDE,
                "cfa_factors": factors.tolist()}
    finally:
        Ops.emit = installed_emit
        runtime.close()
