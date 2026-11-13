"""Run ``run()`` inside the patched frozen seven-frame engine, without files.

Unlike the normal unit tests, this probe uses the shipped DNG profile helper,
not a stub.  It requires the original v124_dng/output_chain modules and their
bundled numpy/cv2 runtime.  It does not read photos or write output images.
"""


def run():
    import math
    import numpy as np
    import v124_dng
    import output_chain
    from engine_patch import rendering

    class Profile:
        # XYZ -> linear sRGB is a convenient synthetic camera ColorMatrix.
        CM1 = np.array([[3.2404542, -1.5371385, -0.4985314],
                        [-0.9692660, 1.8760108, 0.0415560],
                        [0.0556434, -0.2040259, 1.0572252]])

        def __init__(self, neutral, calibrated=False):
            self.ASN = np.asarray(neutral, dtype=np.float64)
            self.calibrated = calibrated

        def dng_ifd0_tags(self):
            if not self.calibrated:
                return []
            # Rational CameraCalibration1 and AnalogBalance as native DNG tags.
            return [(50723, 10, 9, (8, 10, 0, 1, 0, 1,
                                    0, 1, 11, 10, 0, 1,
                                    0, 1, 0, 1, 9, 10), False),
                    (50727, 5, 3, (12, 10, 9, 10, 11, 10), False)]

    writer_code = output_chain.write_fullwell_dng16.__code__
    receipt = rendering.install()
    assert output_chain.write_fullwell_dng16.__code__.co_code == writer_code.co_code
    assert rendering.BASELINE_POLICY in output_chain.write_fullwell_dng16.__code__.co_consts
    assert v124_dng.baseline_exposure(1) == 0
    assert v124_dng.baseline_exposure(2) == -1
    assert v124_dng.baseline_exposure(.25) == 2

    neutral = Profile([1, 1, 1])
    shot = Profile([.5, 1, .8])
    calibrated = Profile([.5, 1, .8], calibrated=True)
    matrices = [v124_dng.thumbnail_matrix(profile)
                for profile in (neutral, shot, calibrated)]
    for matrix in matrices:
        assert matrix.shape == (3, 3) and np.isfinite(matrix).all()
    assert not np.allclose(matrices[0], matrices[1])
    assert not np.allclose(matrices[1], matrices[2])

    checks = 0
    for profile in (neutral, shot, calibrated):
        camera_gray = (profile.ASN * .18).astype(np.float32).reshape(1, 1, 3)
        gray, _ = output_chain._display_rgb(camera_gray, profile)
        assert int(np.ptp(gray[0, 0])) <= 1, (profile.ASN, gray)
        for gain in (.5, 1, 2, 4):
            compensated, _ = output_chain._display_rgb(camera_gray, profile, encoding_gain=gain)
            assert np.max(np.abs(compensated.astype(int) - gray.astype(int))) <= 1
            checks += 1

    image = np.tile(np.array([[[.02, .1, .4], [.2, .1, .03],
                               [.5, .4, .7], [0, 0, 0]]], dtype=np.float32), (11, 5, 1))
    image[0, 0] = [9, .07, .2]
    image[-1, -1] = [.09, 9, .2]
    for profile in (neutral, shot, calibrated):
        for gain in (.5, 1, 1.73, 4):
            baseline = v124_dng.baseline_exposure(gain)
            assert abs(math.log2(gain) + baseline) <= .005 + 1e-12
            for side in (1, 7, 256):
                thumbnail = v124_dng.dng_thumbnail(image, profile, encoding_gain=gain,
                                                  baseline=baseline, max_side=side)
                preview, audit = output_chain._display_rgb(image, profile, max_side=side,
                                                           encoding_gain=gain)
                assert np.array_equal(thumbnail, preview)
                assert audit["camera_white_balance_baked"] is True
                assert audit["extra_exposure_ev"] == 0
                checks += 1

    return {"passed": True, "comparisons": checks,
            "actual_as_shot_neutral_used": True,
            "actual_camera_calibration_and_analog_balance_used": True,
            "original_writer_instructions_unchanged_during_install": True,
            "rendering_policy": receipt["rendering_policy"]}


# The patched-engine probe provides this dictionary to its explicit script.
if "report" in globals():
    report["rendering_runtime"] = run()
