"""Explicit developer probe in the bundled runtime; never runs during normal jobs."""
import json
from pathlib import Path
import sys
import traceback


def run(report_path, args):
    from engine_patch import __version__
    result = {"patch_version": __version__, "python": sys.version, "status": "running"}
    code = 0
    try:
        from engine_patch import rendering, physical
        import v124_dng
        result["baseline"] = {str(g): v124_dng.baseline_exposure(g) for g in (0.25, 1.0, 4.0)}
        if result["baseline"] != {"0.25": 2.0, "1.0": 0.0, "4.0": -2.0}:
            raise AssertionError("Neutral baseline function did not take effect")
        # A local validation script is an explicit CLI developer feature, not an
        # untrusted DNG instruction. Only the supplied script is executed.
        if args:
            if len(args) != 2 or args[0] != "--probe-script":
                raise ValueError("Expected --probe-script PATH")
            import runpy
            scope = runpy.run_path(str(Path(args[1]).resolve()), init_globals={"report": result})
        result["status"] = "passed"
    except Exception:
        result.update(status="failed", error=traceback.format_exc())
        code = 1
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return code
