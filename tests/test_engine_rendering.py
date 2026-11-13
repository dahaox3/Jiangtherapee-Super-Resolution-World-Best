"""Numeric rendering regressions; all fixtures are synthetic camera RGB."""

import inspect
import math
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np
import cv2  # Load extension before temporary sys.modules patches in install tests.

from engine_patch import rendering


def _legacy_writer(*args, **kwargs):
    # The actual original writer is tested by the packaged-engine probe.  This
    # stub verifies that installation changes one literal and no instructions.
    return "2 EV - log2(encoding_gain); reference-camera units"


def _modules():
    dng = types.ModuleType("v124_dng")
    dng.baseline_exposure = lambda gain: round(2 - math.log2(gain), 2)
    dng.thumbnail_matrix = lambda profile: profile.matrix
    dng.dng_thumbnail = lambda *args, **kwargs: None
    output = types.ModuleType("output_chain")
    output._display_rgb = lambda *args, **kwargs: None
    output.write_fullwell_dng16 = types.FunctionType(
        _legacy_writer.__code__, globals(), "write_fullwell_dng16")
    return {"v124_dng": dng, "output_chain": output}


class QuantizationTests(unittest.TestCase):
    def test_neutral_baseline_removes_only_extra_two_stops(self):
        for gain, expected in [(1, 0), (2, -1), (4, -2), (0.25, 2)]:
            self.assertEqual(rendering.neutral_baseline_exposure(gain), expected)
        for gain in np.geomspace(0.001, 1000, 31):
            baseline = rendering.neutral_baseline_exposure(gain)
            self.assertLessEqual(abs(math.log2(gain) + baseline), 0.005 + 1e-12)
            legacy = round(2 - math.log2(gain), 2)
            self.assertAlmostEqual(legacy - baseline, 2.0)

    def test_bad_gains_fail(self):
        for gain in (0, -1, math.nan, math.inf, -math.inf):
            with self.subTest(gain=gain), self.assertRaises(ValueError):
                rendering.neutral_baseline_exposure(gain)

    def test_quantizer_matches_original_formula_without_mutation(self):
        image = np.random.default_rng(129).uniform(-0.2, 1.9, (259, 5, 3)).astype(np.float32)
        before = image.copy()
        gain = 1.731
        expected = np.rint(np.clip(image * np.float32(65535 * gain), 0, 65535)).astype(np.uint16)
        for rows in (1, 128, 500):
            actual = rendering.encode_camera_u16(image, gain, block_rows=rows)
            np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(image, before)

    def test_bad_camera_arrays_fail(self):
        for image in (np.zeros((0, 3, 3)), np.zeros((2, 2)),
                      np.zeros((2, 2, 4)), np.full((1, 1, 3), math.nan),
                      np.full((1, 1, 3), math.inf)):
            with self.subTest(shape=image.shape), self.assertRaises(ValueError):
                rendering.encode_camera_u16(image, 1)


class RendererTests(unittest.TestCase):
    def render(self, image, **kwargs):
        return rendering.render_camera_rgb(np.asarray(image, dtype=np.float32),
                                           np.eye(3), **kwargs)

    def test_black_middle_gray_white_and_color_have_known_srgb_values(self):
        image = [[[0, 0, 0], [.18, .18, .18], [1, 1, 1], [.25, .5, .75]]]
        expected = [[[0, 0, 0], [118, 118, 118], [255, 255, 255], [137, 188, 225]]]
        np.testing.assert_array_equal(self.render(image), np.array(expected, dtype=np.uint8))

    def test_container_gain_does_not_brighten_unclipped_pixels(self):
        image = [[[.18, .10, .03], [.01, .08, .14]]]
        reference = self.render(image).astype(int)
        for gain in (.25, .5, 2, 4):
            rendered = self.render(image, encoding_gain=gain).astype(int)
            self.assertLessEqual(np.max(np.abs(rendered - reference)), 1)

    def test_compensation_does_not_invent_clipped_highlights(self):
        # .9 is clipped to 1 in a 2x encoded container, then compensated to .5.
        rendered = self.render([[[.9, .9, .9]]], encoding_gain=2)
        np.testing.assert_array_equal(rendered, [[[188, 188, 188]]])

    def test_as_shot_matrix_neutralizes_colored_camera_gray(self):
        image = np.array([[[.09, .18, .36]]], dtype=np.float32)
        rendered = rendering.render_camera_rgb(image, np.diag([2, 1, .5]))
        np.testing.assert_array_equal(rendered, [[[118, 118, 118]]])

    def test_negative_matrix_components_and_primaries_clip_cleanly(self):
        matrix = np.array([[1.2, -.1, -.1], [-.1, 1.2, -.1], [-.1, -.1, 1.2]])
        image = np.eye(3, dtype=np.float32).reshape(1, 3, 3)
        rendered = rendering.render_camera_rgb(image, matrix)
        np.testing.assert_array_equal(rendered, (image * 255).astype(np.uint8))

    def test_saturated_color_corners_clip_before_resizing(self):
        image = [[[4, .1, .1], [.1, 4, .1]],
                 [[.1, .1, 4], [.1, .1, .1]]]
        # Each output camera component is mean(65535,6554,6554,6554),
        # rounded to 21299 DN.  Resizing source floats first gives white.
        rendered = self.render(image, max_side=1)
        np.testing.assert_array_equal(rendered, [[[154, 154, 154]]])
        self.assertTrue(np.all(rendered < 255))

    def test_linear_resampling_precedes_gamma(self):
        rendered = self.render([[[1, 1, 1], [0, 0, 0]],
                                [[0, 0, 0], [0, 0, 0]]], max_side=1)
        # Average linear quarter-white encodes to 137, not average sRGB 64.
        np.testing.assert_array_equal(rendered, [[[137, 137, 137]]])

    def test_eight_and_sixteen_bit_exports_share_transfer(self):
        image = np.random.default_rng(14).uniform(0, 1, (9, 13, 3)).astype(np.float32)
        eight = self.render(image, bits=8)
        sixteen = self.render(image, bits=16)
        self.assertEqual(eight.dtype, np.uint8)
        self.assertEqual(sixteen.dtype, np.uint16)
        reduced = np.rint(sixteen.astype(float) / 257).astype(int)
        self.assertLessEqual(np.max(np.abs(reduced - eight.astype(int))), 1)

    def test_no_upscaling_and_aspect_ratio_preserved(self):
        image = np.zeros((7, 11, 3), dtype=np.float32)
        self.assertEqual(self.render(image, max_side=99).shape, (7, 11, 3))
        self.assertEqual(self.render(image, max_side=5).shape, (3, 5, 3))
        self.assertEqual(self.render(image, max_side=.1).shape, (1, 1, 3))

    def test_invalid_rendering_contracts_fail(self):
        image = np.ones((1, 1, 3), dtype=np.float32)
        invalid = ({"bits": 12}, {"max_side": 0}, {"max_side": math.nan},
                   {"white_level": 0}, {"white_level": 65536},
                   {"baseline": math.nan}, {"baseline": 2000})
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                rendering.render_camera_rgb(image, np.eye(3), **options)
        with self.assertRaises(ValueError):
            rendering.render_camera_rgb(image, np.full((3, 3), math.nan))


class InstallationTests(unittest.TestCase):
    def test_thumbnail_and_output_are_identical_at_same_size(self):
        modules = _modules()
        image = np.random.default_rng(41).uniform(-.05, 1.8, (17, 23, 3)).astype(np.float32)
        # Include color-specific hard clipping at opposite image corners.
        image[0, 0] = [9, .07, .2]
        image[-1, -1] = [.09, 9, .2]
        profile = types.SimpleNamespace(matrix=np.array([[1.5, -.2, -.1],
                                                         [-.1, 1.3, -.1],
                                                         [-.1, -.2, 1.1]]))
        with patch.dict(sys.modules, modules):
            rendering.install()
            for gain in (.5, 1, 1.73, 4):
                for side in (1, 7, 256):
                    dng = modules["v124_dng"]
                    thumbnail = dng.dng_thumbnail(image, profile, encoding_gain=gain,
                                                  baseline=dng.baseline_exposure(gain),
                                                  max_side=side)
                    preview, audit = modules["output_chain"]._display_rgb(
                        image, profile, max_side=side, encoding_gain=gain)
                    np.testing.assert_array_equal(thumbnail, preview)
                    self.assertTrue(audit["camera_white_balance_baked"])
                    self.assertEqual(audit["extra_exposure_ev"], 0)

    def test_install_is_idempotent_and_changes_no_writer_instructions(self):
        modules = _modules()
        writer = modules["output_chain"].write_fullwell_dng16
        original_code = writer.__code__
        original_matrix = modules["v124_dng"].thumbnail_matrix
        with patch.dict(sys.modules, modules):
            first = rendering.install()
            second = rendering.install()
            self.assertEqual(first, second)
            self.assertIs(modules["v124_dng"].thumbnail_matrix, original_matrix)
            self.assertEqual(writer.__code__.co_code, original_code.co_code)
            self.assertEqual(writer.__code__.co_consts[0], original_code.co_consts[0])
            self.assertEqual(writer(), rendering.BASELINE_POLICY)
            self.assertIs(modules["v124_dng"].baseline_exposure,
                          rendering.neutral_baseline_exposure)

    def test_unexpected_writer_fails_before_partial_installation(self):
        modules = _modules()
        modules["output_chain"].write_fullwell_dng16 = lambda: "unknown policy"
        baseline_before = modules["v124_dng"].baseline_exposure
        thumbnail_before = modules["v124_dng"].dng_thumbnail
        with patch.dict(sys.modules, modules), self.assertRaises(RuntimeError):
            rendering.install()
        self.assertIs(modules["v124_dng"].baseline_exposure, baseline_before)
        self.assertIs(modules["v124_dng"].dng_thumbnail, thumbnail_before)

    def test_original_callable_signatures_are_preserved(self):
        self.assertEqual(str(inspect.signature(rendering.dng_thumbnail)),
                         "(rgb, profile, *, encoding_gain, baseline, max_side=256, white_level=65535)")
        self.assertEqual(str(inspect.signature(rendering._display_rgb)),
                         "(rgb, profile, max_side=2560, bits=8, encoding_gain=1.0)")


if __name__ == "__main__":
    unittest.main()
