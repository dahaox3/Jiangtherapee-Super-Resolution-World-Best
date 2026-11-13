"""UI state regressions; windows stay withdrawn and no engine is launched."""

import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from iqoo_easy import gui
from iqoo_easy.runner import Cancelled, RunResult


class GuiStateTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.withdraw()
        self.patches = [
            patch.object(gui, "load_settings", return_value={}),
            patch.object(gui, "save_settings"),
            patch.object(gui, "discover_engine", return_value=Path("/fake/jsr_backend.exe")),
        ]
        for item in self.patches:
            item.start()
        self.app = gui.Application(self.root)
        self.calibration = SimpleNamespace(iso=400, shutter_seconds=0.02,
            parameter_summary="读噪 1.204 DN · 有效满阱 2368 e⁻；四个相位分别标定。",
            black_level_note="四个相位分别标定。")

    def tearDown(self):
        if not hasattr(self, "app"):
            return
        self.app.cancel_event.set()
        if self.app.worker is not None:
            self.app.worker.join(timeout=3)
        try:
            if self.app._poll_id:
                self.root.after_cancel(self.app._poll_id)
            self.root.destroy()
        except tk.TclError:
            pass
        for item in reversed(self.patches):
            item.stop()

    def ready(self):
        self.app.validation_generation = 2
        self.app._handle_event(("validated", 2, self.calibration))

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(predicate(), "GUI state did not settle")

    def test_late_validation_does_not_replace_new_selection(self):
        self.app.validation_generation = 3
        self.app._handle_event(("validated", 2, self.calibration))
        self.assertIsNone(self.app.calibration)
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_validation_error_clears_previous_calibration(self):
        self.ready()
        self.app._handle_event(("validation_error", 2, "第七张 ISO 不一致"))
        self.assertIsNone(self.app.calibration)
        self.assertIn("ISO", self.app.status_var.get())
        self.assertEqual(str(self.app.start_button["state"]), "disabled")

    def test_valid_selection_shows_automatic_parameters(self):
        self.ready()
        self.assertIn("ISO 400", self.app.camera_var.get())
        self.assertIn("1/50", self.app.camera_var.get())
        self.assertIn("2368", self.app.parameters_var.get())
        self.assertEqual(str(self.app.start_button["state"]), "normal")

    def test_fractional_black_is_displayed_per_phase(self):
        self.calibration.parameter_summary = "黑电平 [64.06, 64, 64, 64.06] DN；四个相位分别标定。"
        self.ready()
        self.assertIn("64.06", self.app.parameters_var.get())
        self.assertIn("四个相位", self.app.parameters_var.get())
        self.assertNotIn("均值近似", self.app.parameters_var.get())

    def test_cancel_waits_for_worker_and_restores_controls(self):
        self.ready()
        self.app.output_var.set(str(Path.cwd() / "unused-output"))

        def run(*args, cancel, **kwargs):
            if not cancel.wait(2):
                raise AssertionError("cancel did not reach the worker")
            raise Cancelled()

        with patch.object(gui, "validate_engine", side_effect=lambda value: value), patch.object(gui, "run_burst", side_effect=run):
            self.app.start()
            self.assertEqual(str(self.app.output_entry["state"]), "disabled")
            self.assertEqual(str(self.app.choose_button["state"]), "disabled")
            self.app.cancel()
            self.wait_for(lambda: self.app.state == "ready")
        self.app.worker.join(timeout=1)
        self.assertFalse(self.app.worker.is_alive())
        self.assertEqual(str(self.app.choose_button["state"]), "normal")
        self.assertIn("取消", self.app.status_var.get())

    def test_completed_job_exposes_output_actions_without_opening_apps(self):
        self.ready()
        self.app.output_var.set(str(Path.cwd() / "unused-output"))
        result = RunResult(Path("/test/result.dng"), Path("/test"), Path("/test/run.log"))
        with patch.object(gui, "validate_engine", side_effect=lambda value: value), patch.object(gui, "run_burst", return_value=result), patch.object(gui, "_open_path") as open_path:
            self.app.start()
            self.wait_for(lambda: self.app.state == "complete")
            self.assertEqual(str(self.app.open_file_button["state"]), "normal")
            open_path.assert_not_called()
            self.app.open_result()
            open_path.assert_called_once_with(result.output_file)


if __name__ == "__main__":
    unittest.main()
