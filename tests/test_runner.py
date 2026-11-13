"""Exercise the launcher against tiny fake engines, never the GPU backend."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from iqoo_easy import runner, settings


@dataclass(frozen=True)
class Frame:
    path: Path


@dataclass(frozen=True)
class Calibration:
    frames: tuple[Frame, ...]
    read_noise_dn: float = 1.203928123
    black_dn: float = 64.0
    fullwell_e: float = 2368.3461

    def engine_payload(self):
        return {"schema": "JSR_CFA_CALIBRATION_V1", "frames": [
            {"source": str(f.path), "black_levels_dn": [64.06, 64, 64, 64.06]}
            for f in self.frames]}

    def to_dict(self):
        return {
            "paths": [str(frame.path) for frame in self.frames],
            "read_noise_dn": self.read_noise_dn,
            "black_dn": self.black_dn,
            "fullwell_e": self.fullwell_e,
        }


# The application protocol is exercised using a real subprocess. Its "DNG" is
# deliberately only a container-header stub: decoding/quality is tested by the
# separate integration run using the real engine and local photos.
FAKE_ENGINE = r'''
import json, os, pathlib, sys, time
mode = sys.argv[1]
args = sys.argv[2:]
root = pathlib.Path(args[args.index('--output-dir') + 1])
prefix = args[args.index('--out-prefix') + 1]
(root / 'child-env.json').write_text(json.dumps({k: v for k, v in os.environ.items()
    if k.startswith('_PYI_') or k in ('_MEIPASS2', 'PYINSTALLER_RESET_ENVIRONMENT',
        'TEMP', 'TMP', 'TMPDIR', 'PYTHONUTF8', 'PYTHONIOENCODING')}), encoding='utf-8')
if mode == 'cancel':
    print('READY', flush=True)
    time.sleep(30)
    sys.exit(99)
print('SRHQ_V12_PROGRESS|40|正在合成', flush=True)
if mode in ('success', 'failure-with-output', 'bad-header', 'truncated', 'multiple'):
    nested = root / prefix
    nested.mkdir()
    data = b'II*\0' + bytes(2048)
    if mode == 'bad-header': data = b'JPEG' + bytes(2048)
    if mode == 'truncated': data = b'II*\0'
    (nested / (prefix + '.dng')).write_bytes(data)
    if mode == 'multiple': (nested / 'unexpected.dng').write_bytes(data)
print('SRHQ_V12_PROGRESS|100|导出完成', flush=True)
if mode in ('failure', 'failure-with-output'):
    print('simulated export error', flush=True)
    sys.exit(7)
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        # Keep test files on the checkout drive instead of the system TEMP disk.
        local = Path(__file__).resolve().parents[1] / ".local-test"
        local.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="runner-", dir=local)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "portable data"
        self.output = self.root / "output with spaces"
        self.engine = self.root / "engine" / "jsr_backend.exe"
        self.engine.parent.mkdir()
        self.engine.write_bytes(b"fake engine, never executed")
        inputs = self.root / "原始 DNG"
        inputs.mkdir()
        frames = []
        for index in range(7):
            path = inputs / f"frame {index}.dng"
            path.write_bytes(f"unchanged input {index}".encode())
            frames.append(Frame(path.resolve()))
        self.calibration = Calibration(tuple(frames))
        self.originals = {f.path: f.path.read_bytes() for f in frames}
        self.events = []
        self.children = []
        self.launches = []
        self.real_popen = subprocess.Popen
        for patcher in (
            mock.patch.object(runner, "validate_engine", return_value=self.engine),
            mock.patch.object(runner, "validate_burst", return_value=self.calibration),
            mock.patch.object(runner, "data_root", return_value=self.data),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    def spawn(self, mode):
        def launch(command, **kwargs):
            if str(command[0]) != str(self.engine):
                # subprocess.run(taskkill) also uses Popen; let it target the
                # fake engine's real PID instead of replacing taskkill itself.
                return self.real_popen(command, **kwargs)
            self.launches.append((list(command), dict(kwargs)))
            process = self.real_popen([sys.executable, "-c", FAKE_ENGINE, mode, *command[1:]], **kwargs)
            self.children.append(process)
            return process
        return mock.patch.object(runner.subprocess, "Popen", side_effect=launch)

    def run_fake(self, mode, **kwargs):
        with self.spawn(mode):
            return runner.run_burst(self.calibration, self.engine, self.output,
                                    on_event=lambda kind, value: self.events.append((kind, value)),
                                    **kwargs)

    def receipts(self):
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in self.output.glob("*/job.json")]

    def test_command_passes_original_paths_and_precise_calibration(self):
        command = runner.build_command(self.calibration, self.engine, self.output, "test-prefix")
        paths = [command[i + 1] for i, token in enumerate(command) if token == "--raw-frame"]
        self.assertEqual(paths, [str(f.path) for f in self.calibration.frames])
        for flag, value in (("--read-noise-dn", self.calibration.read_noise_dn),
                            ("--black-level-dn", self.calibration.black_dn),
                            ("--full-well-electrons", self.calibration.fullwell_e)):
            self.assertAlmostEqual(float(command[command.index(flag) + 1]), value, places=7)
        self.assertIn("--developer-mode", command)
        self.assertIn("--confirm-equal-exposure", command)
        self.assertEqual(command[command.index("--iqoo-calibration") + 1], str(self.output / "calibration-cfa.json"))
        self.assertEqual(command[command.index("--camera-profile-source") + 1], "embedded-dng")
        self.assertNotIn("--ptc-proxy", command)
        self.assertNotIn("--dark-frame", command)

    def test_success_is_unique_and_preserves_inputs(self):
        first = self.run_fake("success")
        second = self.run_fake("success")
        self.assertNotEqual(first.job_dir, second.job_dir)
        self.assertTrue(first.output_file.is_file())
        self.assertEqual(first.output_file.parent, first.job_dir)
        self.assertTrue(first.log_path.is_file())
        self.assertIn("正在合成", first.log_path.read_text(encoding="utf-8"))
        self.assertEqual({p: p.read_bytes() for p in self.originals}, self.originals)
        self.assertEqual([r["status"] for r in self.receipts()], ["complete", "complete"])
        receipt = json.loads((first.job_dir / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["output_sha256"], runner.sha256(first.output_file))
        self.assertEqual(receipt["calibration"], self.calibration.to_dict())
        self.assertEqual(json.loads((first.job_dir / "calibration-cfa.json").read_text(encoding="utf-8")),
                         self.calibration.engine_payload())
        self.assertEqual(self.events[-1], ("complete", str(second.output_file)))
        self.assertEqual(list((self.data / "temp").iterdir()), [])

    def test_child_runtime_is_independent_of_frozen_launcher(self):
        with mock.patch.dict(os.environ, {
            "_PYI_APPLICATION_HOME_DIR": "C:/parent/_internal",
            "_PYI_ARCHIVE_FILE": "C:/parent/parent.exe",
            "_PYI_PARENT_PROCESS_LEVEL": "1",
            "_MEIPASS2": "C:/parent/_MEI123",
        }):
            result = self.run_fake("success")
        env = json.loads((result.job_dir / "child-env.json").read_text(encoding="utf-8"))
        self.assertFalse(any(k.startswith("_PYI_") for k in env))
        self.assertNotIn("_MEIPASS2", env)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(env["TEMP"], env["TMP"])
        self.assertEqual(env["TEMP"], env["TMPDIR"])
        self.assertTrue(Path(env["TEMP"]).is_relative_to(self.data))
        _, options = self.launches[0]
        self.assertEqual(options["cwd"], self.engine.parent)
        self.assertFalse(options.get("shell", False))

    def test_failed_engine_does_not_report_success_even_with_output(self):
        for mode in ("failure", "failure-with-output"):
            with self.subTest(mode=mode):
                self.events.clear()
                with self.assertRaisesRegex(RuntimeError, "代码 7"):
                    self.run_fake(mode)
                self.assertFalse(any(event[0] == "complete" for event in self.events))
                self.assertNotIn(("progress", 100.0), self.events)
        self.assertTrue(all(r["status"] == "failed" for r in self.receipts()))

    def test_exit_zero_requires_one_nontruncated_dng(self):
        for mode in ("missing", "truncated", "multiple", "bad-header"):
            with self.subTest(mode=mode):
                self.events.clear()
                with self.assertRaises(RuntimeError):
                    self.run_fake(mode)
                self.assertFalse(any(kind == "complete" for kind, _ in self.events))
                self.assertNotIn(("progress", 100.0), self.events)
        self.assertTrue(all(r["status"] == "failed" for r in self.receipts()))

    def test_changed_calibration_is_rejected_before_launch(self):
        with mock.patch.object(runner, "validate_burst", return_value=replace(self.calibration, read_noise_dn=3)), \
                mock.patch.object(runner.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(runner.ValidationError, "变化"):
                runner.run_burst(self.calibration, self.engine, self.output)
        launch.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_pre_cancelled_job_does_not_launch(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(runner.subprocess, "Popen") as launch:
            with self.assertRaises(runner.Cancelled):
                runner.run_burst(self.calibration, self.engine, self.output, cancel=cancel)
        launch.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_cancel_stops_owned_child_and_keeps_cancelled_receipt(self):
        cancel = threading.Event()

        def receive(kind, value):
            self.events.append((kind, value))
            if kind == "log" and value == "READY":
                cancel.set()

        with self.spawn("cancel"):
            with self.assertRaises(runner.Cancelled):
                runner.run_burst(self.calibration, self.engine, self.output,
                                 on_event=receive, cancel=cancel)
        self.assertIsNotNone(self.children[0].poll())
        self.assertEqual(self.receipts()[0]["status"], "cancelled")
        self.assertFalse(any(kind == "complete" for kind, _ in self.events))
        self.assertEqual(list((self.data / "temp").iterdir()), [])
        self.assertEqual({p: p.read_bytes() for p in self.originals}, self.originals)

    def test_execution_lock_is_exclusive_and_released(self):
        with runner.execution_lock(self.data):
            with self.assertRaisesRegex(RuntimeError, "另一个"):
                with runner.execution_lock(self.data):
                    self.fail("second job acquired the lock")
        with runner.execution_lock(self.data):
            pass

    def test_stop_targets_only_owned_pid(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = None
        with mock.patch.object(runner.os, "name", "nt"), \
                mock.patch.object(runner.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                mock.patch.object(runner.subprocess, "run") as kill:
            runner._stop_process(proc)
        self.assertEqual(kill.call_args.args[0], ["taskkill.exe", "/PID", "12345", "/T", "/F"])
        proc.wait.assert_called()

    def test_taskkill_timeout_still_falls_back_to_owned_process(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("fake", 10), 0]
        with mock.patch.object(runner.os, "name", "nt"), \
                mock.patch.object(runner.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                mock.patch.object(runner.subprocess, "run", side_effect=subprocess.TimeoutExpired("taskkill", 20)):
            runner._stop_process(proc)
        proc.kill.assert_called_once()

    def test_frozen_windows_dll_search_is_restored_after_spawn_failure(self):
        kernel = mock.Mock()
        old_directory = "D:\\launcher\\_internal"

        def get_directory(size, buffer):
            if size:
                buffer.value = old_directory
            return len(old_directory)

        kernel.GetDllDirectoryW.side_effect = get_directory
        kernel.SetDllDirectoryW.return_value = 1
        with mock.patch.object(runner.os, "name", "nt"), \
                mock.patch.object(runner.sys, "frozen", True, create=True), \
                mock.patch("ctypes.WinDLL", return_value=kernel, create=True):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                with runner.external_dll_search():
                    kernel.SetDllDirectoryW.assert_called_once_with(None)
                    raise OSError("spawn failed")
        self.assertEqual(kernel.SetDllDirectoryW.call_args_list,
                         [mock.call(None), mock.call(old_directory)])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        local = Path(__file__).resolve().parents[1] / ".local-test"
        local.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="settings-", dir=local)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = mock.patch.dict(os.environ, {"JSR_EASY_DATA_DIR": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_portable_settings_round_trip(self):
        saved = {"input_dir": "F:/中文照片", "engine_path": "D:/JSR/resources/jsr_backend.exe"}
        settings.save_settings(saved)
        self.assertEqual(settings.data_root(), self.root.resolve())
        self.assertEqual(settings.load_settings(), saved)
        self.assertEqual([p.name for p in self.root.iterdir()], ["settings.json"])

    def test_failed_serialization_preserves_previous_settings(self):
        settings.save_settings({"input_dir": "F:/original"})
        with self.assertRaises(ValueError):
            settings.save_settings({"value": float("nan")})
        self.assertEqual(settings.load_settings(), {"input_dir": "F:/original"})
        self.assertEqual([p.name for p in self.root.iterdir()], ["settings.json"])

    def test_corrupt_settings_fall_back_without_deletion(self):
        path = self.root / "settings.json"
        path.write_text("not valid json", encoding="utf-8")
        self.assertEqual(settings.load_settings(), {})
        self.assertEqual(path.read_text(encoding="utf-8"), "not valid json")


if __name__ == "__main__":
    unittest.main()
