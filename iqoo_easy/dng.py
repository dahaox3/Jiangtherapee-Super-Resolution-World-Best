"""Read original iQOO DNG metadata for the engine's per-CFA noise interface.

Only the Python standard library is used. Original files are opened read-only.
NoiseProfile describes a normalized variance ``S*x + O``. For a linear,
unprocessed RAW with span W-B, this gives an *effective*, model-derived
full well of 1/S electrons and a read-noise estimate of (W-B)*sqrt(O) DN.
These are metadata estimates, not a new physical sensor measurement.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Iterable


class ValidationError(ValueError):
    """An input cannot safely use the automatic main-camera workflow."""


PROVENANCE = "DNG NoiseProfile 元数据估算，非实测标定"

@dataclass(frozen=True)
class FrameMetadata:
    path: Path
    make: str
    model: str
    width: int
    height: int
    bits_per_sample: int
    cfa_pattern: tuple[int, ...]
    iso: int
    shutter_seconds: float
    f_number: float
    focal_length_mm: float
    black_dn: float
    white_dn: float
    noise_s: float
    noise_o: float
    read_noise_dn: float
    fullwell_e: float
    file_size: int
    modified_ns: int
    black_levels_dn: tuple[float, ...] = ()
    noise_slopes: tuple[float, ...] = ()
    noise_offsets: tuple[float, ...] = ()
    provenance: str = PROVENANCE

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def shutter(self) -> float:
        return self.shutter_seconds

    @property
    def black_level_max_error_dn(self) -> float:
        return max((abs(value - self.black_dn) for value in self.black_levels_dn), default=0.0)

    @property
    def cfa_black(self) -> tuple[float, ...]:
        return self.black_levels_dn * 4 if len(self.black_levels_dn) == 1 else self.black_levels_dn

    @property
    def cfa_read_noise_dn(self) -> tuple[float, ...]:
        return tuple((self.white_dn - b) * math.sqrt(o) for b, o in zip(self.cfa_black, self.noise_offsets))

    def engine_payload(self, digest: str) -> dict:
        return {"source": str(self.path), "sha256": digest,
                "width": self.width, "height": self.height,
                "cfa_pattern": list(self.cfa_pattern),
                "black_levels_dn": list(self.cfa_black),
                "white_levels_dn": [self.white_dn] * 4,
                "noise_s": list(self.noise_slopes), "noise_o": list(self.noise_offsets),
                "iso": self.iso, "shutter_seconds": self.shutter_seconds}

    def to_dict(self) -> dict:
        result = asdict(self)
        result["path"] = str(self.path)
        result["cfa_pattern"] = list(self.cfa_pattern)
        result["black_levels_dn"] = list(self.black_levels_dn)
        result["noise_slopes"] = list(self.noise_slopes)
        result["noise_offsets"] = list(self.noise_offsets)
        result["black_level_max_error_dn"] = self.black_level_max_error_dn
        return result


@dataclass(frozen=True)
class BurstCalibration:
    frames: tuple[FrameMetadata, ...]
    reference: FrameMetadata
    iso: int
    shutter_seconds: float
    black_dn: float
    white_dn: float
    read_noise_dn: float
    fullwell_e: float
    source_sha256: tuple[str, ...]
    provenance: str = PROVENANCE

    @property
    def shutter(self) -> float:
        return self.shutter_seconds

    @property
    def black_level_note(self) -> str:
        values = ", ".join(f"{v:g}" for v in self.reference.cfa_black)
        return f"参考帧黑电平 [{values}] DN；每张照片按各自四个相位标定。"

    @property
    def parameter_summary(self) -> str:
        def span(values):
            low, high = min(values), max(values)
            return f"{low:.4g}" if math.isclose(low, high, rel_tol=1e-6) else f"{low:.4g}–{high:.4g}"
        noises = [v for frame in self.frames for v in frame.cfa_read_noise_dn]
        wells = [1 / s for frame in self.frames for s in frame.noise_slopes]
        return (f"照片噪声模型估算：读噪 {span(noises)} DN  ·  有效满阱 {span(wells)} e⁻\n"
                + self.black_level_note)

    def engine_payload(self) -> dict:
        return {"schema": "JSR_CFA_CALIBRATION_V1",
                "frames": [f.engine_payload(digest) for f, digest in zip(self.frames, self.source_sha256)]}

    def to_dict(self) -> dict:
        return {
            "frames": [frame.to_dict() for frame in self.frames],
            "reference": str(self.reference.path),
            "iso": self.iso,
            "shutter_seconds": self.shutter_seconds,
            "black_dn": self.black_dn,
            "white_dn": self.white_dn,
            "read_noise_dn": self.read_noise_dn,
            "fullwell_e": self.fullwell_e,
            "source_sha256": list(self.source_sha256),
            "black_level_note": self.black_level_note,
            "provenance": self.provenance,
        }


# Only selected metadata values are decoded; raw pixel data is never loaded.
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1,
          8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_FORMATS = {1: "B", 3: "H", 4: "I", 6: "b", 7: "B", 8: "h",
            9: "i", 11: "f", 12: "d", 13: "I"}
_TAGS = {254, 256, 257, 258, 262, 271, 272, 277, 330, 34665,
         33421, 33422, 33434, 33437, 34855, 34867, 37386,
         50706, 50712, 50713, 50714, 50715, 50716, 50717, 50727, 50935, 51041}
_MAX_IFDS = 64
_MAX_ENTRIES = 4096
_MAX_METADATA_VALUE = 1024 * 1024


@dataclass
class _IFD:
    offset: int
    kind: str
    tags: dict
    parent: int | None


class _TIFF:
    def __init__(self, stream: BinaryIO, size: int):
        self.stream, self.size = stream, size
        self.ifds: list[_IFD] = []
        self.by_offset: dict[int, _IFD] = {}
        header = self._read(0, 8)
        if header[:2] not in (b"II", b"MM"):
            raise ValidationError("不是有效的 TIFF/DNG 文件")
        self.order = "<" if header[:2] == b"II" else ">"
        magic, first = struct.unpack(self.order + "HI", header[2:])
        if magic != 42:
            raise ValidationError("暂不支持 BigTIFF；请选择手机原始 DNG")
        if first < 8:
            raise ValidationError("DNG 缺少有效的图像目录")
        self._walk(first, "root", None, frozenset())

    def _read(self, offset: int, count: int) -> bytes:
        if offset < 0 or count < 0 or offset > self.size or count > self.size - offset:
            raise ValidationError("DNG 元数据偏移超出文件范围，文件可能不完整")
        self.stream.seek(offset)
        value = self.stream.read(count)
        if len(value) != count:
            raise ValidationError("读取 DNG 时文件发生变化或内容不完整")
        return value

    def _decode(self, kind: int, count: int, value: bytes):
        if kind == 2:
            return value.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
        if kind in (5, 10):
            fmt = self.order + ("II" if kind == 5 else "ii")
            values = []
            for numerator, denominator in struct.iter_unpack(fmt, value):
                if denominator == 0:
                    raise ValidationError("DNG 元数据含零分母")
                values.append(numerator / denominator)
            return tuple(values)
        return struct.unpack(self.order + str(count) + _FORMATS[kind], value)

    def _walk(self, offset: int, kind: str, parent: int | None, ancestors: frozenset):
        if not offset:
            return
        if offset in ancestors:
            raise ValidationError("DNG 图像目录包含循环引用")
        if offset in self.by_offset:
            return
        if len(self.ifds) >= _MAX_IFDS:
            raise ValidationError("DNG 图像目录数量过多")
        count = struct.unpack(self.order + "H", self._read(offset, 2))[0]
        if count > _MAX_ENTRIES:
            raise ValidationError("DNG 单个目录的元数据数量过多")
        table = self._read(offset + 2, count * 12 + 4)
        tags = {}
        for index in range(count):
            entry = table[index * 12:index * 12 + 12]
            tag, field_type, length = struct.unpack(self.order + "HHI", entry[:8])
            if tag not in _TAGS:
                continue
            if tag in tags:
                raise ValidationError(f"DNG 元数据标签重复：{tag}")
            if field_type not in _SIZES or not length:
                raise ValidationError(f"DNG 元数据标签类型无效：{tag}")
            size = _SIZES[field_type] * length
            if size > _MAX_METADATA_VALUE:
                raise ValidationError(f"DNG 元数据标签过大：{tag}")
            if size > 4:
                position = struct.unpack(self.order + "I", entry[8:])[0]
                value = self._read(position, size)
            else:
                value = entry[8:8 + size]
            tags[tag] = self._decode(field_type, length, value)
            if tag in (330, 34665) and field_type not in (4, 13):
                raise ValidationError("DNG 子目录指针类型无效")
        directory = _IFD(offset, kind, tags, parent)
        self.ifds.append(directory)
        self.by_offset[offset] = directory
        ancestors = ancestors | {offset}
        for pointer_tag, child_kind in ((330, "raw-subifd"), (34665, "exif")):
            for child in tags.get(pointer_tag, ()):
                self._walk(child, child_kind, offset, ancestors)
        following = struct.unpack(self.order + "I", table[count * 12:])[0]
        if following:
            self._walk(following, "next", parent, ancestors)


def _numbers(value, label: str) -> tuple[float, ...]:
    if not isinstance(value, tuple) or not value:
        raise ValidationError(f"缺少或无法识别 {label}")
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise ValidationError(f"{label} 不是有限数值")
    return value


def _scalar(value, label: str) -> float:
    values = _numbers(value, label)
    if len(values) != 1:
        raise ValidationError(f"{label} 应只有一个值")
    return values[0]


def _integer(value, label: str) -> int:
    number = _scalar(value, label)
    if number != int(number):
        raise ValidationError(f"{label} 必须是整数")
    return int(number)


def _uniform(value, label: str, *, tolerance: float = 1e-7) -> float:
    values = _numbers(value, label)
    first = values[0]
    if any(not math.isclose(v, first, rel_tol=tolerance, abs_tol=1e-12) for v in values):
        raise ValidationError(f"各颜色通道的{label}不一致，不能使用单组手动参数")
    return float(first)


def read_metadata(path: str | Path) -> FrameMetadata:
    """Read and validate the supported iQOO 12 Pro main-camera RAW mode."""
    path = Path(path)
    try:
        path = path.resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".dng":
            raise ValidationError("请选择原始 DNG 文件")
        with path.open("rb") as stream:
            stamp = os.fstat(stream.fileno())
            parsed = _TIFF(stream, stamp.st_size)
            after = os.fstat(stream.fileno())
            if (stamp.st_size, stamp.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValidationError("读取时原片发生变化，请等文件复制完成")
        root = parsed.ifds[0]
        if 50706 not in root.tags:
            raise ValidationError("文件没有 DNGVersion 标签")
        candidates = []
        for directory in parsed.ifds:
            t = directory.tags
            if t.get(262) == (32803,) and t.get(254, (0,)) == (0,):
                candidates.append(directory)
        if len(candidates) != 1:
            raise ValidationError("需要一幅原始 Bayer RAW；不能选择已合成的线性 RGB DNG")
        raw = candidates[0]
        # Prefer tags of the full-resolution RAW, then its parent/root; EXIF
        # supplies capture settings but never overrides the RAW calibration.
        inherited = dict(root.tags)
        lineage = []
        current = raw
        while current.parent is not None:
            current = parsed.by_offset[current.parent]
            lineage.append(current)
        for directory in reversed(lineage):
            inherited.update(directory.tags)
        inherited.update(raw.tags)
        capture = dict(inherited)
        for directory in parsed.ifds:
            if directory.kind == "exif" and directory.parent in {root.offset, raw.offset}:
                capture.update(directory.tags)
        model = capture.get(272, "")
        make = capture.get(271, "")
        if not isinstance(model, str) or model.upper() != "V2329A":
            raise ValidationError(f"自动模式适用于 iQOO 12 Pro（V2329A），当前机型为 {model!r}")
        if not isinstance(make, str) or make.lower() not in ("vivo", "iqoo"):
            raise ValidationError("无法确认手机厂商为 vivo/iQOO")
        f_number = float(_scalar(capture.get(33437), "光圈"))
        focal = float(_scalar(capture.get(37386), "焦距"))
        if not math.isclose(f_number, 1.68, abs_tol=0.02) or not math.isclose(focal, 6.45, abs_tol=0.05):
            raise ValidationError(f"请选择 1× 主摄原片；当前 f/{f_number:g}、焦距 {focal:g} mm")
        width = _integer(raw.tags.get(256), "RAW 宽度")
        height = _integer(raw.tags.get(257), "RAW 高度")
        bits = _integer(raw.tags.get(258), "RAW 位深")
        if width < 2 or height < 2 or bits not in (10, 12, 14, 16):
            raise ValidationError("RAW 尺寸或位深不受支持")
        if (width, height) != (4096, 3072):
            raise ValidationError("当前自动模式支持 1× 主摄 4096×3072 DNG；请选择同一拍摄模式")
        if raw.tags.get(277, (1,)) != (1,):
            raise ValidationError("原片必须是单平面 Bayer RAW")
        if inherited.get(33421) != (2, 2):
            raise ValidationError("原片需要 2×2 Bayer 色彩排列")
        cfa = tuple(_numbers(inherited.get(33422), "CFA 排列"))
        if len(cfa) != 4 or sorted(cfa) != [0, 1, 1, 2] or cfa.index(0) ^ cfa.index(2) != 3:
            raise ValidationError("原片 CFA 排列不是受支持的 Bayer 模式")
        iso = _integer(capture.get(34855, capture.get(34867)), "ISO")
        shutter = float(_scalar(capture.get(33434), "快门时间"))
        if not 0 < iso <= 1_000_000 or not 0 < shutter <= 3600:
            raise ValidationError("ISO 或快门时间超出有效范围")
        if 50712 in inherited:
            raise ValidationError("DNG 包含线性化查找表，暂不能可靠换算三参数")
        for delta in (50715, 50716):
            if delta in inherited and any(v != 0 for v in _numbers(inherited[delta], "黑电平修正")):
                raise ValidationError("DNG 包含逐行或逐列变化的黑电平，当前四相位接口不支持")
        if 50935 in inherited and _scalar(inherited[50935], "已应用降噪") > 0:
            raise ValidationError("DNG 声明已应用降噪，无法从噪声模型可靠估算电子数")
        if 50727 in inherited and any(v != 1 for v in _numbers(inherited[50727], "模拟平衡")):
            raise ValidationError("DNG 包含分通道增益，暂不能使用单组噪声参数")
        black_levels = tuple(float(v) for v in _numbers(inherited.get(50714), "黑电平"))
        repeat = inherited.get(50713, (1, 1))
        if (len(black_levels), repeat) not in ((1, (1, 1)), (4, (2, 2))):
            raise ValidationError("黑电平排列不受支持；需要单个值或 2×2 Bayer 排列")
        black = math.fsum(black_levels) / len(black_levels)
        white = _uniform(inherited.get(50717), "白电平")
        if not 0 < white <= (1 << bits) - 1 or any(not 0 <= v < white - 5 for v in black_levels):
            raise ValidationError("黑白电平超出 RAW 编码范围")
        noise = _numbers(inherited.get(51041), "NoiseProfile 噪声模型（缺失时需提供匹配标定）")
        if len(noise) not in (2, 6):
            raise ValidationError("NoiseProfile 的通道数量不受支持")
        slopes = tuple(float(noise[0 if len(noise) == 2 else int(color) * 2]) for color in cfa)
        offsets = tuple(float(noise[1 if len(noise) == 2 else int(color) * 2 + 1]) for color in cfa)
        if any(not 0 < s <= 1 for s in slopes) or any(not 0 <= o < 1 for o in offsets):
            raise ValidationError("NoiseProfile 系数超出有效范围")
        # These summaries feed the original parser's scalar placeholders only.
        # The reconstruction receives every original per-phase coefficient.
        slope, offset = math.fsum(slopes) / 4, math.fsum(offsets) / 4
        cfa_black = black_levels * 4 if len(black_levels) == 1 else black_levels
        fullwell = math.fsum(1 / s for s in slopes) / 4
        read_noise = math.fsum((white - b) * math.sqrt(o) for b, o in zip(cfa_black, offsets)) / 4
        if not math.isfinite(fullwell) or not math.isfinite(read_noise):
            raise ValidationError("噪声模型换算结果不是有限数值")
        return FrameMetadata(path, make, model, width, height, bits, cfa, iso, shutter,
                             f_number, focal, black, white, slope, offset, read_noise, fullwell,
                             stamp.st_size, stamp.st_mtime_ns, black_levels_dn=black_levels,
                             noise_slopes=slopes, noise_offsets=offsets)
    except ValidationError as exc:
        raise ValidationError(f"{path.name}：{exc}") from exc
    except (OSError, struct.error, OverflowError) as exc:
        raise ValidationError(f"{path.name}：无法读取 DNG 元数据：{exc}") from exc


def validate_burst(paths: Iterable[str | Path], *, expected_count: int = 7) -> BurstCalibration:
    """Preflight a burst; never silently reuse another ISO's calibration."""
    paths = tuple(Path(path) for path in paths)
    if expected_count != 7:
        raise ValidationError("简化模式目前使用七张合成引擎")
    if len(paths) != expected_count:
        raise ValidationError(f"请选择同一场景的 7 张原始 DNG，当前为 {len(paths)} 张")
    resolved = [str(path.resolve()).casefold() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ValidationError("选中的文件路径重复，请选择 7 张不同的原片")
    frames = tuple(read_metadata(path) for path in paths)
    first = frames[0]
    exact = ("make", "model", "width", "height", "bits_per_sample", "cfa_pattern", "iso")
    approximate = ("shutter_seconds", "f_number", "focal_length_mm")
    labels = {"iso": "ISO", "shutter_seconds": "快门", "f_number": "光圈", "focal_length_mm": "焦距",
              "black_dn": "黑电平", "black_levels_dn": "黑电平分布", "white_dn": "白电平", "noise_s": "噪声模型 S", "noise_o": "噪声模型 O"}
    for frame in frames[1:]:
        for key in exact:
            if getattr(frame, key) != getattr(first, key):
                raise ValidationError(f"{frame.name} 的{labels.get(key, '相机/RAW 模式')}与 {first.name} 不同；请锁定 1×、ISO 和快门后重新选择")
        for key in approximate:
            if not math.isclose(getattr(frame, key), getattr(first, key), rel_tol=1e-6, abs_tol=1e-12):
                raise ValidationError(f"{frame.name} 的{labels[key]}与 {first.name} 不同；请选择同一曝光设置的七张原片")
    digests = []
    for frame in frames:
        try:
            with frame.path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                after = os.fstat(stream.fileno())
                expected = (frame.file_size, frame.modified_ns)
                if any((stat.st_size, stat.st_mtime_ns) != expected for stat in (before, after)):
                    raise ValidationError(f"{frame.name}：校验时原片发生变化，请等文件复制完成")
        except OSError as exc:
            raise ValidationError(f"{frame.name}：无法校验原片：{exc}") from exc
        if digest in digests:
            raise ValidationError(f"{frame.name} 与 {frames[digests.index(digest)].name} 内容完全相同；复制同一张不能代替连拍")
        digests.append(digest)
    return BurstCalibration(frames, frames[len(frames) // 2], first.iso, first.shutter_seconds,
                            first.black_dn, first.white_dn, first.read_noise_dn,
                            first.fullwell_e, tuple(digests))
