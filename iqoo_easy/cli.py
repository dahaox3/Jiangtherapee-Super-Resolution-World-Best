"""Optional headless entry for inspection, reproducible runs and package checks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .dng import validate_burst
from .runner import discover_engine, run_burst
from .settings import write_json


def main() -> int:
    if len(sys.argv) == 1:
        from .gui import main as gui_main
        gui_main()
        return 0
    parser = argparse.ArgumentParser(description="JSR 主摄助手：每组七张同曝光的 iQOO 12 Pro 主摄 DNG")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "run"):
        child = commands.add_parser(name)
        child.add_argument("files", nargs=7, type=Path)
        child.add_argument("--report", type=Path, help="将结果或错误写入 JSON；窗口版 EXE 建议指定")
        if name == "run":
            child.add_argument("--engine", type=Path)
            child.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        calibration = validate_burst(args.files)
        result = {"status": "validated", "calibration": calibration.to_dict()}
        if args.command == "run":
            engine = args.engine or discover_engine()
            if engine is None:
                raise ValueError("未找到 JSR 引擎，请指定 --engine resources/jsr_backend.exe")
            completed = run_burst(calibration, engine, args.output_dir)
            result.update(status="complete", output_file=str(completed.output_file),
                          job_dir=str(completed.job_dir), log_path=str(completed.log_path))
        code = 0
    except Exception as exc:
        result = {"status": "failed", "error": str(exc)}
        code = 1
    if args.report:
        write_json(args.report.resolve(), result)
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return code
