"""Small, bounded PyInstaller CArchive adapter; preserves untouched compressed data.

No upstream Python code is distributed here. The caller must supply its own
matching original executable. This module does not execute archive contents.
"""
from dataclasses import dataclass
import hashlib
import struct
import zlib

MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
COOKIE = struct.Struct("!8sIIII64s")
ENTRY = struct.Struct("!IIIIBc")


@dataclass(frozen=True)
class Member:
    name: str
    kind: bytes
    compressed: int
    size: int
    data: bytes

    def extract(self):
        value = zlib.decompress(self.data) if self.compressed else self.data
        if len(value) != self.size:
            raise ValueError("Archive member size mismatch: " + self.name)
        return value

    @classmethod
    def create(cls, name, kind, data):
        return cls(name, kind, 1, len(data), zlib.compress(data, 9))


class Archive:
    def __init__(self, blob):
        if len(blob) < COOKIE.size:
            raise ValueError("Truncated archive")
        magic, size, offset, length, self.python, self.library = COOKIE.unpack(blob[-COOKIE.size:])
        if magic != MAGIC or size > len(blob) or size < COOKIE.size:
            raise ValueError("Expected an unsigned, complete PyInstaller CArchive")
        start = len(blob) - size
        if offset + length != size - COOKIE.size:
            raise ValueError("Invalid CArchive table bounds")
        self.prefix = blob[:start]
        self.members = []
        cursor, end = start + offset, start + offset + length
        seen = set()
        while cursor < end:
            record, position, packed, unpacked, compressed, kind = ENTRY.unpack_from(blob, cursor)
            if record < ENTRY.size + 1 or cursor + record > end or position + packed > offset:
                raise ValueError("Invalid CArchive member bounds")
            name = blob[cursor + ENTRY.size:cursor + record].rstrip(b"\0").decode("utf-8")
            if compressed not in (0, 1) or (name in seen and kind != b"o"):
                raise ValueError("Invalid or duplicate CArchive member: " + name)
            seen.add(name)
            self.members.append(Member(name, kind, compressed, unpacked, blob[start + position:start + position + packed]))
            cursor += record

    def pack(self):
        body, toc = bytearray(), bytearray()
        for member in self.members:
            name = member.name.encode("utf-8") + b"\0"
            record = (ENTRY.size + len(name) + 15) // 16 * 16
            toc.extend(ENTRY.pack(record, len(body), len(member.data), member.size, member.compressed, member.kind))
            toc.extend(name.ljust(record - ENTRY.size, b"\0"))
            body.extend(member.data)
        cookie = COOKIE.pack(MAGIC, len(body) + len(toc) + COOKIE.size,
                             len(body), len(toc), self.python, self.library)
        return self.prefix + body + toc + cookie

    def inventory(self):
        return {m.name: hashlib.sha256(m.data).hexdigest() for m in self.members if m.kind != b"o"}
