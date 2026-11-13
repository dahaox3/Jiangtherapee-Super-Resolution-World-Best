"""Build the engine patch using CPython 3.10, without extracting model weights.

Usage: python310/python.exe engine_patch/build.py --original ORIGINAL.exe
       --output STAGING/jsr_backend.exe
"""
import argparse
import hashlib
import json
import marshal
from pathlib import Path
import sys
import types

# Embeddable Python runs with an isolated sys.path. Add only this source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine_patch import ORIGINAL_ENGINE_SHA256, __version__
from engine_patch.archive import Archive, Member


def patch_code(root):
    hits = []

    def visit(code):
        if code.co_name == "product_main" and code.co_filename == "backend.py":
            if code.co_argcount != 1 or code.co_freevars or code.co_kwonlyargcount:
                raise ValueError("Unexpected product_main signature")
            original = marshal.dumps(code)
            hits.append({"name": code.co_name, "line": code.co_firstlineno,
                         "marshal_sha256": hashlib.sha256(original).hexdigest()})
            wrapper = compile(
                "def product_main(argv=None):\n"
                "    from engine_patch.bootstrap import run_original\n"
                "    return run_original(b'IQOO_ORIGINAL_PRODUCT_MAIN', globals(), argv)\n",
                "engine_patch/entry.py", "exec")
            function = next(x for x in wrapper.co_consts if isinstance(x, types.CodeType))
            return function.replace(co_consts=tuple(original if x == b"IQOO_ORIGINAL_PRODUCT_MAIN" else x
                                                     for x in function.co_consts))
        constants = []
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                value = visit(value)
            elif isinstance(value, bytes) and len(value) > 128:
                # Upstream has three marshal-in-bytes entry wrappers. Parse only
                # full code objects; random model/data constants remain untouched.
                import io
                stream = io.BytesIO(value)
                try:
                    nested = marshal.load(stream)
                except (ValueError, TypeError, EOFError):
                    nested = None
                if isinstance(nested, types.CodeType) and stream.tell() == len(value):
                    count = len(hits)
                    updated = visit(nested)
                    if len(hits) != count:
                        value = marshal.dumps(updated)
            constants.append(value)
        return code.replace(co_consts=tuple(constants))

    updated = visit(root)
    if len(hits) != 1:
        raise ValueError(f"Expected exactly one original entry, found {len(hits)}")
    return updated, hits


def build(original, output):
    if sys.version_info[:2] != (3, 10):
        raise RuntimeError("Use CPython 3.10: the original engine uses CPython 3.10 bytecode")
    original, output = Path(original).resolve(), Path(output).resolve()
    if original == output or output.exists():
        raise ValueError("Choose a new output path; this builder never overwrites an executable")
    blob = original.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != ORIGINAL_ENGINE_SHA256:
        raise ValueError("Original engine checksum mismatch; refusing to patch an unknown or already modified version")
    archive = Archive(blob)
    before = archive.inventory()
    if archive.python != 310:
        raise ValueError("Unexpected embedded Python version")
    found = [m for m in archive.members if m.name == "backend" and m.kind == b"s"]
    if len(found) != 1:
        raise ValueError("Missing original backend entry")
    updated, entry = patch_code(marshal.loads(found[0].extract()))
    replacement = Member.create("backend", b"s", marshal.dumps(updated))
    archive.members = [replacement if m is found[0] else m for m in archive.members]
    source_hashes = {}
    for name in ("__init__.py", "bootstrap.py", "calibration.py", "physical.py", "rendering.py", "probe.py"):
        # Git's Windows line-ending preference must not change release hashes.
        source = Path(__file__).with_name(name).read_bytes().replace(b"\r\n", b"\n")
        compile(source, "engine_patch/" + name, "exec")
        source_hashes[name] = hashlib.sha256(source).hexdigest()
        archive.members.append(Member.create("engine_patch\\" + name, b"x", source))
    manifest = {"patch_version": __version__, "original_sha256": digest,
                "entry": entry, "adapter_sources": source_hashes,
                "policy": "neutral display; exact CFA calibration before reconstruction; native lossless DNG"}
    archive.members.append(Member.create("engine_patch\\manifest.json", b"x",
                                        json.dumps(manifest, indent=2).encode("utf-8")))
    packed = archive.pack()
    verified = Archive(packed)
    after = verified.inventory()
    changed = [name for name, value in before.items() if after.get(name) != value]
    if changed != ["backend"] or verified.prefix != archive.prefix:
        raise ValueError("Unexpected change to upstream runtime/model content")
    manifest.update(patched_sha256=hashlib.sha256(packed).hexdigest(),
                    modified_original_members=changed,
                    preserved_original_members=len(before) - len(changed),
                    added_members=[name for name in after if name not in before],
                    archive_members=after)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(packed)
    output.with_suffix(".patch.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {k: v for k, v in manifest.items() if k != "archive_members"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.original, args.output), indent=2))
