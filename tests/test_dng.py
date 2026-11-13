"""Bounded metadata fixtures: no personal photos are included in this suite."""

import math
import struct
import tempfile
import unittest
from pathlib import Path

from iqoo_easy.dng import ValidationError, read_metadata, validate_burst


SLOPE = 0.000422235575
OFFSET = 1.57600836e-6


def make_fixture(path, *, order="<", changes=None, nested=False, exif=False, marker=b"pixels"):
    """Write TIFF metadata and a unique opaque tail; this is not an image fixture."""
    capture = {
        33434: (5, ((1, 50),)), 33437: (5, ((168, 100),)),
        34855: (3, (400,)), 37386: (5, ((645, 100),)),
    }
    raw = {
        254: (4, (0,)), 256: (4, (4096,)), 257: (4, (3072,)),
        258: (3, (16,)), 262: (3, (32803,)), 277: (3, (1,)),
        33421: (3, (2, 2)), 33422: (1, (0, 1, 1, 2)),
        50713: (3, (2, 2)), 50714: (5, ((64, 1),) * 4), 50717: (4, (1023,)),
        51041: (12, (SLOPE, OFFSET) * 3),
    }
    raw.update(capture)
    for key, value in (changes or {}).items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    root = {271: (2, "vivo"), 272: (2, "V2329A"), 50706: (1, (1, 4, 0, 0))}
    directories = [root]
    links = []
    if nested:
        root.update({254: (4, (1,)), 256: (4, (64,)), 257: (4, (48,)),
                     262: (3, (2,)), 50714: (4, (0,)), 50717: (4, (255,))})
        root[330] = (4, (0,))
        links.append((root, 330, 1))
        directories.append(raw)
    else:
        root.update(raw)
        raw = root
    if exif:
        extracted = {key: raw.pop(key) for key in capture if key in raw}
        root[34665] = (4, (0,))
        links.append((root, 34665, len(directories)))
        directories.append(extracted)
    offsets = []
    cursor = 8
    for directory in directories:
        offsets.append(cursor)
        cursor += 2 + 12 * len(directory) + 4
    for directory, tag, index in links:
        directory[tag] = (4, (offsets[index],))
    data = bytearray(cursor)
    struct.pack_into(order + "2sHI", data, 0, b"II" if order == "<" else b"MM", 42, 8)
    formats = {1: "B", 3: "H", 4: "I", 9: "i", 12: "d"}
    for directory, offset in zip(directories, offsets):
        struct.pack_into(order + "H", data, offset, len(directory))
        for index, (tag, (field_type, values)) in enumerate(sorted(directory.items())):
            if field_type == 2:
                blob = values.encode("ascii") + b"\0"
                count = len(blob)
            elif field_type in (5, 10):
                blob = b"".join(struct.pack(order + ("II" if field_type == 5 else "ii"), *v) for v in values)
                count = len(values)
            else:
                blob = struct.pack(order + str(len(values)) + formats[field_type], *values)
                count = len(values)
            position = offset + 2 + index * 12
            struct.pack_into(order + "HHI", data, position, tag, field_type, count)
            if len(blob) > 4:
                struct.pack_into(order + "I", data, position + 8, len(data))
                data.extend(blob)
            else:
                data[position + 8:position + 12] = blob.ljust(4, b"\0")
    path.write_bytes(bytes(data) + marker)
    return path


def mutate_entry(path, tag, *, pointer=None, count=None):
    data = bytearray(path.read_bytes())
    entries = struct.unpack_from("<H", data, 8)[0]
    for index in range(entries):
        pos = 10 + index * 12
        if struct.unpack_from("<H", data, pos)[0] == tag:
            if pointer is not None:
                struct.pack_into("<I", data, pos + 8, pointer)
            if count is not None:
                struct.pack_into("<I", data, pos + 4, count)
            path.write_bytes(data)
            return
    raise AssertionError(f"tag {tag} missing")


class DNGTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def fixture(self, name="one.dng", **kwargs):
        return make_fixture(self.folder / name, **kwargs)

    def batch(self, changes=None):
        return [self.fixture(f"frame_{i}.dng", marker=bytes([i]), changes=changes) for i in range(7)]

    def test_both_endian_and_exif_layouts(self):
        for order in ("<", ">"):
            for nested in (False, True):
                for exif in (False, True):
                    with self.subTest(order=order, nested=nested, exif=exif):
                        m = read_metadata(self.fixture(order=order, nested=nested, exif=exif))
                        self.assertEqual((m.width, m.height), (4096, 3072))
                        self.assertEqual(m.iso, 400)
                        self.assertEqual(m.shutter_seconds, 0.02)
                        self.assertEqual(m.black_dn, 64)
                        self.assertEqual(m.white_dn, 1023)
                        self.assertAlmostEqual(m.fullwell_e, 2368.3461536844684)
                        self.assertAlmostEqual(m.read_noise_dn, 1.2039206554142843)

    def test_single_noise_pair_accepted(self):
        m = read_metadata(self.fixture(changes={51041: (12, (SLOPE, OFFSET))}))
        self.assertAlmostEqual(m.fullwell_e, 1 / SLOPE)

    def test_calibration_and_order_preserved(self):
        paths = self.batch()
        calibration = validate_burst(paths)
        self.assertEqual(calibration.reference.path, paths[3].resolve())
        self.assertEqual(calibration.frames[0].path, paths[0].resolve())
        self.assertEqual(len(set(calibration.source_sha256)), 7)
        self.assertIsInstance(calibration.to_dict()["frames"][0]["path"], str)
        self.assertIn("估算", calibration.provenance)

    def test_input_files_unchanged(self):
        paths = self.batch()
        before = [p.read_bytes() for p in paths]
        validate_burst(paths)
        self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_different_batches_may_use_different_iso_and_shutter(self):
        first = validate_burst(self.batch())
        second = validate_burst(self.batch(changes={
            34855: (3, (800,)), 33434: (5, ((1, 100),)),
            51041: (12, (SLOPE * 2, OFFSET * 4) * 3),
        }))
        self.assertEqual((first.iso, first.shutter_seconds), (400, 0.02))
        self.assertEqual((second.iso, second.shutter_seconds), (800, 0.01))
        self.assertAlmostEqual(second.fullwell_e, first.fullwell_e / 2)
        self.assertAlmostEqual(second.read_noise_dn, first.read_noise_dn * 2)


    def test_missing_and_invalid_noise(self):
        for value in (None, (12, (0.0, OFFSET)), (12, (math.nan, OFFSET)),
                      (12, (SLOPE, math.inf)), (12, (SLOPE, -1.0)),
                      (12, (2.0, OFFSET)), (12, (SLOPE, OFFSET, SLOPE, OFFSET))):
            with self.subTest(value=value), self.assertRaisesRegex(ValidationError, "NoiseProfile|噪声模型"):
                read_metadata(self.fixture(changes={51041: value}))

    def test_heterogeneous_channel_model_rejected(self):
        noise = (SLOPE, OFFSET, SLOPE * 2, OFFSET, SLOPE, OFFSET)
        with self.assertRaisesRegex(ValidationError, "各颜色通道"):
            read_metadata(self.fixture(changes={51041: (12, noise)}))

    def test_black_levels_must_agree_and_fit_range(self):
        for levels in (((64, 1), (65, 1)), ((1023, 1),), ((1024, 1),)):
            with self.subTest(levels=levels), self.assertRaises(ValidationError):
                read_metadata(self.fixture(changes={50714: (5, levels)}))

    def test_fractional_black_levels_use_bounded_mean_without_changing_input(self):
        levels = ((6406, 100), (64, 1), (64, 1), (6406, 100))
        paths = self.batch(changes={50714: (5, levels)})
        before = [p.read_bytes() for p in paths]
        c = validate_burst(paths)
        self.assertAlmostEqual(c.black_dn, 64.03)
        self.assertEqual(c.frames[0].black_levels_dn, (64.06, 64, 64, 64.06))
        self.assertAlmostEqual(c.frames[0].black_level_max_error_dn, 0.03)
        self.assertAlmostEqual(c.read_noise_dn, (1023 - 64.03) * math.sqrt(OFFSET))
        self.assertIn("均值近似", c.black_level_note)
        self.assertIn("0.03", c.black_level_note)
        self.assertEqual(c.to_dict()["frames"][0]["black_levels_dn"], [64.06, 64, 64, 64.06])
        self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_black_approximation_requires_both_dn_and_noise_bounds(self):
        cases = (
            {50714: (5, ((6411, 100), (64, 1), (64, 1), (6411, 100)))},
            {50714: (5, ((6406, 100), (64, 1), (64, 1), (6406, 100))),
             51041: (12, (SLOPE, 1e-9) * 3)},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValidationError, "微差适配"):
                read_metadata(self.fixture(changes=changes))

    def test_equal_black_scalar_and_invalid_repeat_geometry(self):
        m = read_metadata(self.fixture(changes={50713: None, 50714: (5, ((64, 1),))}))
        self.assertEqual(m.black_dn, 64)
        self.assertEqual(m.black_level_max_error_dn, 0)
        for dimensions in ((1, 1), (1, 4), (4, 1)):
            with self.subTest(dimensions=dimensions), self.assertRaisesRegex(ValidationError, "排列"):
                read_metadata(self.fixture(changes={50713: (3, dimensions)}))

    def test_same_mean_but_different_black_pattern_is_rejected(self):
        paths = self.batch(changes={50714: (5, ((6406, 100), (64, 1), (64, 1), (6406, 100)))})
        make_fixture(paths[4], changes={50714: (5, ((64, 1), (6406, 100), (6406, 100), (64, 1)))})
        with self.assertRaisesRegex(ValidationError, "黑电平分布"):
            validate_burst(paths)

    def test_long_lens_wrong_model_and_linear_output_rejected(self):
        for changes in ({33437: (5, ((257, 100),)), 37386: (5, ((133, 10),))},
                        {272: (2, "different")}, {262: (3, (34892,))}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                read_metadata(self.fixture(changes=changes))

    def test_mixed_exposure_rejected_with_filename(self):
        for changes in ({34855: (3, (800,))}, {33434: (5, ((1, 60),))}):
            with self.subTest(changes=changes):
                paths = self.batch()
                make_fixture(paths[4], changes=changes)
                with self.assertRaisesRegex(ValidationError, "frame_4.dng"):
                    validate_burst(paths)

    def test_mixed_noise_profile_rejected(self):
        paths = self.batch()
        make_fixture(paths[2], changes={51041: (12, (SLOPE * 1.01, OFFSET) * 3)})
        with self.assertRaisesRegex(ValidationError, "噪声模型"):
            validate_burst(paths)

    def test_duplicate_paths_and_content_rejected(self):
        paths = self.batch()
        with self.assertRaisesRegex(ValidationError, "路径重复"):
            validate_burst(paths[:6] + [paths[0]])
        paths[6].write_bytes(paths[0].read_bytes())
        with self.assertRaisesRegex(ValidationError, "内容完全相同"):
            validate_burst(paths)

    def test_frame_count_rejected(self):
        with self.assertRaisesRegex(ValidationError, "7 张"):
            validate_burst(self.batch()[:5])

    def test_bad_offsets_and_oversized_count(self):
        p = self.fixture()
        mutate_entry(p, 51041, pointer=0xFFFFFFF0)
        with self.assertRaisesRegex(ValidationError, "范围"):
            read_metadata(p)
        p = self.fixture()
        mutate_entry(p, 51041, count=0xFFFFFFFF)
        with self.assertRaisesRegex(ValidationError, "过大"):
            read_metadata(p)

    def test_cycle_rejected(self):
        p = self.fixture(nested=True)
        mutate_entry(p, 330, pointer=8)
        with self.assertRaisesRegex(ValidationError, "循环"):
            read_metadata(p)

    def test_zero_denominator_rejected(self):
        with self.assertRaisesRegex(ValidationError, "零分母"):
            read_metadata(self.fixture(changes={33434: (5, ((1, 0),))}))

    def test_truncated_file(self):
        p = self.fixture()
        p.write_bytes(p.read_bytes()[:15])
        with self.assertRaisesRegex(ValidationError, "范围"):
            read_metadata(p)

    def test_big_tiff_unsupported(self):
        p = self.folder / "big.dng"
        p.write_bytes(b"II" + struct.pack("<HI", 43, 8))
        with self.assertRaisesRegex(ValidationError, "BigTIFF"):
            read_metadata(p)

    def test_non_linear_or_processed_raw_rejected(self):
        for change in ({50712: (3, (0, 1, 3, 5))}, {50935: (5, ((1, 2),))},
                       {50715: (10, ((1, 2),))}, {50727: (5, ((2, 1), (1, 1), (1, 1)))}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                read_metadata(self.fixture(changes=change))


if __name__ == "__main__":
    unittest.main()
