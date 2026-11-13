"""Check repackaging boundaries without shipping or executing upstream code."""
import hashlib
import marshal
import types
import unittest

from engine_patch.archive import Archive, Member, COOKIE, MAGIC
from engine_patch.build import patch_code


class ArchiveTests(unittest.TestCase):
    def fixture(self):
        archive = Archive.__new__(Archive)
        archive.prefix = b"MZ synthetic PE prefix"
        archive.python = 310
        archive.library = b"python310.dll"
        archive.members = [Member.create("backend", b"s", b"script"),
                           Member.create("weights.npz", b"x", bytes(range(256)) * 4),
                           Member.create("native.dll", b"b", b"unchanged-library")]
        return archive

    def test_roundtrip_preserves_prefix_and_every_compressed_member(self):
        original = self.fixture()
        restored = Archive(original.pack())
        self.assertEqual(original.prefix, restored.prefix)
        self.assertEqual(original.members, restored.members)
        self.assertEqual(restored.members[1].extract(), bytes(range(256)) * 4)
        self.assertEqual(original.pack(), restored.pack())

    def test_replacing_entry_preserves_model_and_library_byte_for_byte(self):
        archive = self.fixture()
        before = archive.inventory()
        archive.members[0] = Member.create("backend", b"s", b"authored-wrapper")
        archive.members.append(Member.create("engine_patch\\bootstrap.py", b"x", b"pass\n"))
        after = Archive(archive.pack()).inventory()
        self.assertEqual([k for k in before if before[k] != after[k]], ["backend"])

    def test_rejects_truncated_bad_size_and_trailing_data(self):
        blob = self.fixture().pack()
        for broken in (blob[:-1], blob + b"unexpected-signature", b"short"):
            with self.subTest(size=len(broken)), self.assertRaises(ValueError):
                Archive(broken)

    def test_declared_member_size_must_match(self):
        member = Member.create("bad", b"x", b"data")
        with self.assertRaises(ValueError):
            Member(member.name, member.kind, member.compressed, 99, member.data).extract()


class EntryWrapperTests(unittest.TestCase):
    def test_embedded_entry_is_wrapped_once_without_executing(self):
        original = compile("def product_main(argv=None):\n    raise AssertionError('must not run during build')\n", "backend.py", "exec")
        root = compile("payload = b'placeholder'\n", "wrapper.py", "exec")
        root = root.replace(co_consts=tuple(marshal.dumps(original) if x == b"placeholder" else x for x in root.co_consts))
        patched, hits = patch_code(root)
        self.assertEqual(len(hits), 1)
        inner = marshal.loads(next(x for x in patched.co_consts if isinstance(x, bytes)))
        entry = next(x for x in inner.co_consts if isinstance(x, types.CodeType))
        saved = next(x for x in entry.co_consts if isinstance(x, bytes))
        original_entry = next(x for x in original.co_consts if isinstance(x, types.CodeType))
        self.assertEqual(marshal.loads(saved).co_code, original_entry.co_code)
        self.assertIn("engine_patch.bootstrap", entry.co_names)
        self.assertEqual(entry.co_argcount, original_entry.co_argcount)

    def test_missing_or_ambiguous_entry_is_rejected(self):
        for source in ("pass", "def product_main(argv=None): pass\ndef product_main(argv=None): pass"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                patch_code(compile(source, "backend.py", "exec"))

    def test_unexpected_signature_is_rejected(self):
        with self.assertRaises(ValueError):
            patch_code(compile("def product_main(a, b): pass", "backend.py", "exec"))


if __name__ == "__main__":
    unittest.main()
