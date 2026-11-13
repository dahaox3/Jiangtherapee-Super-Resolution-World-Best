"""Runs inside the original interpreter after upstream startup wrappers install."""
import json
import marshal
from pathlib import Path
import sys
import types


def take_option(argv, name):
    count = argv.count(name)
    if not count:
        return None
    if count != 1:
        raise ValueError("Duplicate option: " + name)
    index = argv.index(name)
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise ValueError("Missing option value: " + name)
    value = argv[index + 1]
    del argv[index:index + 2]
    return value


def run_original(original_code, namespace, argv):
    # Batch wrappers may supply argv explicitly; do not modify their input list.
    args = list(sys.argv[1:] if argv is None else argv)
    calibration_path = take_option(args, "--iqoo-calibration")
    probe_path = take_option(args, "--iqoo-engine-probe")
    from engine_patch import __version__
    from engine_patch import physical, rendering
    rendering.install()
    physical.install()
    if calibration_path:
        from engine_patch import calibration
        calibration.install(Path(calibration_path))
    print("JSR_ENGINE_PATCH|" + __version__ + "|neutral-display|cfa-physical", flush=True)
    if probe_path:
        from engine_patch.probe import run
        return run(Path(probe_path), args)
    function = types.FunctionType(marshal.loads(original_code), namespace, "product_main")
    return function(args)
