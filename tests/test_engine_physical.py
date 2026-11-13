import copy
import unittest

import cv2
import numpy as np

from engine_patch import physical


class PerCfaNoiseTests(unittest.TestCase):
    def test_noise_model_hand_calculated_and_negative_signal(self):
        # S=.02, O=.003, Q=.001, PRNU=.04.  At signal .5 variance
        # is .01+.003+.001+.01=.024. Exposure 2 and amplitude 3 give /36.
        actual = physical.noise_variance(np.array([-.5, 0, .5]), [.02, .003, .001, .04], 2, 3)
        np.testing.assert_allclose(actual, np.array([.004, .004, .024]) / 36)

    def test_equal_cfa_coefficients_reproduce_scalar_noise(self):
        rng = np.random.default_rng(18)
        signals = rng.uniform(-.1, 1.3, (4, 50))
        coefficients = np.array([.003, .0002, .00001, .0004])
        original = (coefficients[0] * np.maximum(signals, 0) + coefficients[1]
                    + coefficients[2] + coefficients[3] * np.maximum(signals, 0) ** 2) / 2.5 ** 2
        actual = physical.noise_variance(signals, np.tile(coefficients, (4, 1))[:, None], 2.5)
        np.testing.assert_allclose(actual, original, rtol=5e-16, atol=0)

    def test_each_cfa_can_have_independent_shot_and_read_noise(self):
        cfa = [[.01, .001, 0, 0], [.02, .003, 0, 0],
               [.03, .005, 0, 0], [.04, .007, 0, 0]]
        np.testing.assert_allclose(physical.noise_variance(.2, cfa), [.003, .007, .011, .015])

    def test_variance_obeys_squared_exposure_and_amplitude(self):
        cfa = np.array([[.01, .001, 0, 0]] * 4)
        base = physical.noise_variance(.2, cfa)
        np.testing.assert_allclose(physical.noise_variance(.2, cfa, 2, 4), base / 64)

    def test_metadata_does_not_confuse_canonical_colour_with_grid_phase(self):
        cal = np.array([[1, 10, 100, 1000], [2, 20, 200, 2000],
                        [3, 30, 300, 3000], [4, 40, 400, 4000]], np.float32)
        packed = physical.metadata([2], [[3, 1, 2, 0]], [[.9, 1, 1.1, 1.2]],
                                   [[-.1] * 4], [0] * 4, cal)
        self.assertEqual(packed.shape, (29,))
        np.testing.assert_array_equal(packed[:5], [2, 3, 1, 2, 0])
        # canonical B lives at spatial phase 0, but its row is still row 3.
        np.testing.assert_array_equal(packed[25:29], cal[3])

    def test_legacy_scalar_metadata_repeats_coefficients_without_averaging(self):
        packed = physical.metadata([1, 2], [[0, 1, 2, 3]] * 2,
                                   [1] * 4, [-.1] * 4,
                                   [[.1, .2, .3, .4], [.5, .6, .7, .8]]).reshape(2, 29)
        np.testing.assert_array_equal(packed[0, 13:].reshape(4, 4),
                                      np.tile(np.float32([.1, .2, .3, .4]), (4, 1)))
        np.testing.assert_array_equal(packed[1, 13:].reshape(4, 4),
                                      np.tile(np.float32([.5, .6, .7, .8]), (4, 1)))

    def test_frame_reordering_keeps_coefficient_identity(self):
        frames = [{"calibration_cfa": np.full((4, 4), i).tolist()} for i in (1, 2, 3)]
        data = {"manifest": {"frames": frames}, "order": [2, 0, 1], "entries": [{}, {}, {}]}
        physical.attach_cfa(data)
        self.assertEqual([e["calibration_cfa"][0, 0] for e in data["entries"]], [3, 1, 2])

    def test_calibrated_manifest_cannot_silently_lose_a_frame(self):
        data = {"manifest": {"frames": [{}, {}], "cfa_calibration":
                {"schema": "JSR_CFA_CALIBRATION_V1", "enabled": True}},
                "order": [0, 1], "entries": [{}, {}]}
        with self.assertRaisesRegex(ValueError, "every frame"):
            physical.attach_cfa(data)

    def test_bad_coefficients_fail_closed(self):
        for value in (np.zeros((4,)), np.full((4, 4), np.nan),
                      np.full((4, 4), -1), np.full((4, 4), 1e300)):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    physical.metadata([1], [[0, 1, 2, 3]], [1] * 4, [0] * 4, [0] * 4, value)

    def test_registration_green_variance_uses_both_green_noise_models(self):
        # R/B noise must not leak into an alignment guide formed from G1/G2.
        cal = np.array([[100, 100, 0, 0], [0, .004, 0, 0],
                        [0, .012, 0, 0], [100, 100, 0, 0]])
        raw = np.full((32, 32), .2, np.float32)
        data = {"h": 32, "w": 32, "raws": [raw], "entries": [
            {"calibration_cfa": cal, "exposure": 2, "index": [3, 2, 1, 0]}]}
        original = {"linear": np.full((16, 16), .1, np.float32),
                    "variance": np.zeros((16, 16), np.float32), "white": .5,
                    "valid": np.ones((16, 16), np.float32), "floor": 0}
        output, lattice = physical.correct_guide_variance(data, ([copy.deepcopy(original)], np.eye(3)), cv2)
        np.testing.assert_allclose(output[0]["variance"], .001, rtol=2e-6)
        self.assertAlmostEqual(output[0]["floor"], 5 * np.sqrt(.004) / 2, places=8)
        np.testing.assert_array_equal(output[0]["linear"], original["linear"])
        np.testing.assert_array_equal(output[0]["valid"], original["valid"])

    def test_unmarked_registration_input_preserves_original_arrays(self):
        guide = {"linear": np.zeros((4, 4)), "variance": np.full((4, 4), 123), "floor": 99}
        result = ([guide], np.eye(3))
        output = physical.correct_guide_variance(
            {"h": 8, "w": 8, "raws": [np.zeros((8, 8))], "entries": [{}]}, result, cv2)
        self.assertIs(output[0][0]["variance"], guide["variance"])
        self.assertEqual(output[0][0]["floor"], 99)


class ShaderContractTests(unittest.TestCase):
    def test_deghost_pair_variance_selects_canonical_cfa_row(self):
        body = "let j=k*17u;let vv=max(raw,0.);let noise=" + physical._NOISE_PAIR + ";"
        rewritten, _, changed = physical.rewrite_shader(
            ("v15_2.scene_consistency.evidence", "PairEvidence"),
            ("pair_common_ceiling_taylor_explicit_v6", 1.0, 2), body)
        self.assertTrue(changed)
        self.assertIn("let j=k*29u", rewritten)
        self.assertIn("a2[j+13u+c*4u]", rewritten)

    def test_clipping_guard_uses_four_phase_plane_not_three_rgb_colour(self):
        helpers = "fn x(){let variance=" + physical._NOISE_GUARD + ";}"
        body, rewritten, _ = physical.rewrite_shader(
            ("v15_2.physical", "Physical"),
            ("shoulder0999", 0, "censoring_bound_guard"), "", helpers)
        self.assertIn("plane(k,phase)*4u", rewritten)
        self.assertNotIn("+13u+c*4u", rewritten)

    def test_unknown_metadata_shader_and_changed_noise_expression_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "owner"):
            physical.rewrite_shader(("unknown", "NewShader"), "white", "a2[k*17u]")
        with self.assertRaisesRegex(RuntimeError, "tag"):
            physical.rewrite_shader(("v15_2.physical", "Physical"), "new", "a2[k*17u]")
        with self.assertRaisesRegex(RuntimeError, "noise expression changed"):
            physical.rewrite_shader(("v15_2.physical", "Physical"), "evidence130",
                                    "let foo=a2[k*17u+13u];")

    def test_independent_censored_region_counter_layout_is_untouched(self):
        text = "let j=p*19u;let y=stats[j+17u];"
        self.assertEqual(physical.rewrite_shader(("v15_2.scene_consistency.censored", "CensoredShape"),
                                                "censored_moments", text), (text, "", False))


if __name__ == "__main__":
    unittest.main()
