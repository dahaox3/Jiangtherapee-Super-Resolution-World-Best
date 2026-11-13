"""Adapter for the existing, unmodified JSR V18.3.1 seven-frame backend.

The public repository does not include this backend's complete source. The
arguments below were verified using its --help and a real reconstruction.
No photos, calibration values, or engine binaries are downloaded at runtime.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
from typing import Callable
import uuid

from .dng import BurstCalibration, ValidationError, validate_burst
from .settings import application_root, data_root, write_json
from . import __version__

BACKEND_SHA256 = "0b61d2d5af301fd1389952e0f8234e773ac27b468de648ba34709ff09923aed1"
EventCallback = Callable[[str, object], None]


class Cancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    output_file: Path
    job_dir: Path
    log_path: Path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def discover_engine(root: Path | None = None) -> Path | None:
    """Only inspect the app directory and its parent; never scan system drives."""
    base = (root or application_root()).resolve()
    for parent in (base, base.parent):
        candidates = [parent / "resources" / "jsr_backend.exe"]
        candidates += sorted(parent.glob("JSR_V*/resources/jsr_backend.exe"), reverse=True)
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    return None


def validate_engine(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path = path / "resources" / "jsr_backend.exe"
    if path.name.lower() != "jsr_backend.exe" or not path.is_file():
        raise ValidationError("请选择已安装逐通道补丁的 resources/jsr_backend.exe。")
    if sha256(path) != BACKEND_SHA256:
        raise ValidationError("引擎与助手版本不匹配。请使用 v0.2.0 对应的逐通道修正版引擎，不能直接使用原版或其他版本。")
    return path


def build_command(calibration: BurstCalibration, engine: Path, job_dir: Path,
                  prefix: str) -> list[str]:
    argv = [str(engine), "--v12-bracket-frames", "7"]
    for frame in calibration.frames:
        argv += ["--raw-frame", str(frame.path)]
    argv += [
        "--iqoo-calibration", str(job_dir / "calibration-cfa.json"),
        "--developer-mode", "--read-noise-dn", format(calibration.read_noise_dn, ".12g"),
        "--black-level-dn", format(calibration.black_dn, ".12g"),
        "--full-well-electrons", format(calibration.fullwell_e, ".12g"),
        "--confirm-equal-exposure", "--reference-index", "3",
        "--camera-profile-source", "embedded-dng",
        "--output-dir", str(job_dir), "--out-prefix", prefix,
        "--out-dng", "on", "--out-png", "off", "--out-tiff", "off",
        "--dng-compression", "native", "--backend", "Vulkan", "--deghost", "off",
    ]
    return argv


@contextmanager
def execution_lock(root: Path):
    """An OS lock releases even after an unclean launcher exit."""
    root.mkdir(parents=True, exist_ok=True)
    stream = (root / "execution.lock").open("a+b")
    if stream.seek(0, os.SEEK_END) == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError("另一个主摄助手正在合成，请等它结束后再试。") from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # Only this launcher's child and its descendants. Never kill by image name.
        try:
            subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            # Still reap/kill our owned child if taskkill itself is unavailable.
            proc.terminate()
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


@contextmanager
def external_dll_search():
    """Do not leak the frozen launcher's DLL directory to another frozen app."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        yield
        return
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
    kernel.GetDllDirectoryW.restype = ctypes.c_uint32
    kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel.SetDllDirectoryW.restype = ctypes.c_int
    length = kernel.GetDllDirectoryW(0, None)
    old = ctypes.create_unicode_buffer(length + 1)
    kernel.GetDllDirectoryW(len(old), old)
    if not kernel.SetDllDirectoryW(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.SetDllDirectoryW(old.value or None)


def _progress(line: str, emit: EventCallback) -> None:
    match = re.match(r"SRHQ_V12_PROGRESS\|([0-9.]+)\|(.*)", line)
    if match:
        # 100 from the engine precedes final file checks; reserve completion for us.
        emit("progress", min(99.0, float(match[1])))
        message = match[2]
        if float(match[1]) == 2.0 and "PTC" in message:
            message = "解码七张 RAW，应用逐通道标定"
        emit("status", message)


def run_burst(calibration: BurstCalibration, engine: Path, output_root: Path, *,
              on_event: EventCallback | None = None,
              cancel: threading.Event | None = None) -> RunResult:
    emit = on_event or (lambda *_: None)
    cancel = cancel or threading.Event()
    emit("status", "核对照片和合成引擎…")
    engine = validate_engine(engine)
    root = data_root()
    with execution_lock(root):
        # Reread immediately before launch, including hashes, instead of trusting
        # metadata cached by a GUI that could have been open for hours.
        fresh = validate_burst([f.path for f in calibration.frames])
        if fresh != calibration:
            raise ValidationError("所选照片已发生变化，请重新选择七张照片。")
        if cancel.is_set():
            raise Cancelled("已取消。")
        output_root = Path(output_root).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        job_dir = output_root / ("JSR_" + job_id)
        job_dir.mkdir(exist_ok=False)
        temp_dir = root / "temp" / job_id
        temp_dir.mkdir(parents=True, exist_ok=False)
        log_path = job_dir / "engine.log"
        prefix = "JSR_1x_" + job_id
        command = build_command(fresh, engine, job_dir, prefix)
        write_json(job_dir / "calibration-cfa.json", fresh.engine_payload())
        receipt = {
            "app_version": __version__, "status": "running",
            "backend_sha256": BACKEND_SHA256,
            "calibration": fresh.to_dict(), "command": command,
            "note": "NoiseProfile metadata estimates, not an independent PTC measurement",
            "engine_policy": "exact per-CFA calibration; no extra +2 EV; native lossless export",
        }
        write_json(job_dir / "job.json", receipt)
        env = os.environ.copy()
        env.update(TEMP=str(temp_dir), TMP=str(temp_dir), TMPDIR=str(temp_dir),
                   PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        # A frozen launcher sets these for itself. The upstream executable is an
        # independent PyInstaller app and must unpack its own runtime.
        for key in tuple(env):
            if key.startswith("_PYI_") or key == "_MEIPASS2":
                env.pop(key)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        proc = None
        reader = None
        tail: deque[str] = deque(maxlen=12)
        try:
            emit("status", "正在启动 GPU 合成…")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with external_dll_search():
                proc = subprocess.Popen(command, cwd=engine.parent, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, creationflags=flags,
                                        start_new_session=os.name != "nt")
            lines: queue.Queue[bytes | None] = queue.Queue()

            def read_stdout():
                try:
                    assert proc.stdout is not None
                    for line in iter(proc.stdout.readline, b""):
                        lines.put(line)
                finally:
                    lines.put(None)

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            with log_path.open("w", encoding="utf-8", buffering=1) as log:
                eof = False
                while not eof or proc.poll() is None:
                    if cancel.is_set():
                        _stop_process(proc)
                        raise Cancelled("已取消合成；任务记录已保留。")
                    try:
                        raw = lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if raw is None:
                        eof = True
                        continue
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    log.write(line + "\n")
                    tail.append(line)
                    emit("log", line)
                    _progress(line, emit)
            if proc.returncode != 0:
                detail = "\n".join(tail)[-1800:]
                raise RuntimeError(f"合成引擎退出（代码 {proc.returncode}）。\n{detail}\n日志：{log_path}")
            if cancel.is_set():
                raise Cancelled("已取消。")
            files = [p for p in job_dir.rglob("*.dng") if p.is_file()]
            if len(files) != 1 or files[0].stat().st_size < 1024:
                raise RuntimeError(f"引擎未产生完整的 DNG 输出，请查看日志：{log_path}")
            output = files[0]
            with output.open("rb") as stream:
                if stream.read(4) not in (b"II*\0", b"MM\0*"):
                    raise RuntimeError("输出文件不是有效的 TIFF/DNG 文件头。")
            # Show the final DNG directly in its task directory, even when this
            # engine creates an extra prefix-named folder beneath output-dir.
            destination = job_dir / output.name
            if output != destination:
                if destination.exists():
                    raise RuntimeError("输出文件名冲突，已保留引擎的原始输出。")
                output.rename(destination)
                output = destination
            receipt.update(status="complete", output_file=str(output),
                           output_sha256=sha256(output))
            write_json(job_dir / "job.json", receipt)
            emit("progress", 100.0)
            emit("status", "合成完成")
            emit("complete", str(output))
            return RunResult(output.resolve(), job_dir, log_path)
        except Exception as exc:
            if proc is not None:
                _stop_process(proc)
            receipt.update(status="cancelled" if isinstance(exc, Cancelled) else "failed", error=str(exc))
            write_json(job_dir / "job.json", receipt)
            raise
        finally:
            if reader is not None:
                reader.join(timeout=3)
            if proc is not None and proc.stdout is not None:
                proc.stdout.close()
            # Only the unique directory we just created, inside portable UserData.
            if temp_dir.resolve().parent == (root / "temp").resolve():
                shutil.rmtree(temp_dir, ignore_errors=True)
