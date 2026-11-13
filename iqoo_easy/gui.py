"""Small desktop interface for seven-frame iQOO main-camera reconstruction.

All image validation and engine work runs outside the Tk thread.  This module
never changes an input DNG, starts a reconstruction on launch, or opens another
application without an explicit button click.
"""

from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .dng import BurstCalibration, ValidationError, validate_burst
from .runner import Cancelled, RunResult, discover_engine, run_burst, validate_engine
from .settings import load_settings, save_settings


def _existing_directory(value: object, fallback: Path) -> Path:
    if isinstance(value, str) and value.strip():
        candidate = Path(value).expanduser()
        if candidate.is_dir():
            return candidate
    return fallback


def _default_input_directory() -> Path:
    drive = Path("F:/")
    if sys.platform == "win32" and drive.is_dir():
        return drive
    pictures = Path.home() / "Pictures"
    return pictures if pictures.is_dir() else Path.home()


def _format_shutter(seconds: float) -> str:
    if seconds <= 0:
        return "未知"
    reciprocal = 1 / seconds
    if reciprocal >= 2 and abs(reciprocal - round(reciprocal)) < 0.02:
        return f"1/{round(reciprocal)} 秒"
    return f"{seconds * 1000:.3g} ms"


def _open_path(path: Path) -> None:
    """Use the OS association only in response to the user's explicit click."""
    if not path.exists():
        raise OSError(f"文件或目录已不存在：{path}")
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Application(ttk.Frame):
    """The public GUI controller; ``root.withdraw()`` can be used for testing."""

    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root, padding=(24, 20, 24, 18), style="Page.TFrame")
        self.root = root
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.calibration: BurstCalibration | None = None
        self.result: RunResult | None = None
        self.paths: tuple[Path, ...] = ()
        self.state = "empty"
        self.validation_generation = 0
        self.run_generation = 0
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.close_when_idle = False
        self._settings_visible = False
        self._log_visible = False
        self._poll_id: str | None = None
        self._settings_warning = ""

        try:
            self.settings = load_settings()
        except (OSError, ValueError) as exc:
            self.settings = {}
            self._settings_warning = f"上次的设置暂时无法读取：{exc}"
        self.input_dir = _existing_directory(self.settings.get("input_dir"), _default_input_directory())
        saved_output = self.settings.get("output_dir")
        self.output_custom = bool(saved_output)
        self.output_var = tk.StringVar(value=str(saved_output or self.input_dir / "JSR_Easy_Output"))
        saved_engine = self.settings.get("engine_path")
        engine = Path(saved_engine) if isinstance(saved_engine, str) and saved_engine else None
        if engine is None or not engine.is_file():
            engine = discover_engine()
        self.engine_var = tk.StringVar(value=str(engine) if engine else "")

        self.count_var = tk.StringVar(value="尚未选择照片")
        self.camera_var = tk.StringVar(value="只需选择同一场景的 7 张原始 DNG")
        self.parameters_var = tk.StringVar(value="读噪、黑电平和有效满阱会从照片自动读取或估算。")
        self.status_var = tk.StringVar(value="请先选择照片。")
        self.engine_status_var = tk.StringVar(value="合成引擎：已找到" if engine else "合成引擎：需要选择")
        self.result_var = tk.StringVar(value="")

        self._configure_styles()
        self._build()
        self.pack(fill="both", expand=True)
        self.root.title("JSR 主摄助手")
        self.root.configure(background="#f5f4ef")
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.root.bind("<Control-o>", lambda event: self.choose_files())
        self.root.bind("<Configure>", self._resize_labels, add=True)
        if not engine:
            self.toggle_settings()
            self.status_var.set("先选择照片，再在设置中指定官方 JSR 的 jsr_backend.exe。")
        if self._settings_warning:
            self.append_log(self._settings_warning)
        self._fit_window()
        self._refresh_actions()
        self._poll_id = self.root.after(80, self._poll_events)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        font = ("Microsoft YaHei UI", 10)
        self.root.option_add("*Font", font)
        style.configure(".", font=font, background="#f5f4ef", foreground="#24352f")
        style.configure("Page.TFrame", background="#f5f4ef")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Title.TLabel", background="#f5f4ef", font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Muted.TLabel", background="#f5f4ef", foreground="#69756e")
        style.configure("Card.TLabel", background="#ffffff")
        style.configure("CardTitle.TLabel", background="#ffffff", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("CardMuted.TLabel", background="#ffffff", foreground="#69756e")
        style.configure("Status.TLabel", background="#f5f4ef", foreground="#315e4a")
        style.configure("Error.TLabel", background="#f5f4ef", foreground="#a12f22")
        style.configure("TButton", padding=(12, 8), borderwidth=1, relief="flat")
        style.configure("Primary.TButton", background="#315e4a", foreground="#ffffff", padding=(24, 10))
        style.map("Primary.TButton", background=[("disabled", "#c4cec8"), ("active", "#244b3a")], foreground=[("disabled", "#f3f5f3")])
        style.configure("TEntry", fieldbackground="#ffffff", padding=7)
        style.configure("Horizontal.TProgressbar", background="#3e7058", troughcolor="#e1e6df", borderwidth=0)

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        ttk.Label(self, text="JSR 主摄助手", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="选择七张 1× 主摄连拍，自动设置参数，直接合成。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 18))

        card = ttk.Frame(self, style="Card.TFrame", padding=18)
        card.grid(row=2, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        header = ttk.Frame(card, style="Card.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, textvariable=self.count_var, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.choose_button = ttk.Button(header, text="选择七张 DNG…", command=self.choose_files)
        self.choose_button.grid(row=0, column=1)
        files_frame = ttk.Frame(card, style="Card.TFrame")
        files_frame.grid(row=1, column=0, sticky="nsew")
        files_frame.columnconfigure(0, weight=1)
        files_frame.rowconfigure(0, weight=1)
        self.files = tk.Listbox(files_frame, height=7, background="#f7f8f5", foreground="#34443b", relief="flat", borderwidth=0, highlightthickness=0, activestyle="none", selectbackground="#dce8df", selectforeground="#24352f", exportselection=False, font=("Microsoft YaHei UI", 10))
        self.files.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(files_frame, orient="vertical", command=self.files.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.files.configure(yscrollcommand=scroll.set)
        self.camera_label = ttk.Label(card, textvariable=self.camera_var, style="Card.TLabel", wraplength=720)
        self.camera_label.grid(row=2, column=0, sticky="w", pady=(13, 3))
        self.parameters_label = ttk.Label(card, textvariable=self.parameters_var, style="CardMuted.TLabel", wraplength=720)
        self.parameters_label.grid(row=3, column=0, sticky="w")

        output = ttk.Frame(self, style="Page.TFrame")
        output.grid(row=3, column=0, sticky="ew", pady=(16, 8))
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="保存位置").grid(row=0, column=0, padx=(0, 10))
        self.output_entry = ttk.Entry(output, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=1, sticky="ew")
        self.output_button = ttk.Button(output, text="更改…", command=self.choose_output)
        self.output_button.grid(row=0, column=2, padx=(8, 0))

        options = ttk.Frame(self, style="Page.TFrame")
        options.grid(row=4, column=0, sticky="ew")
        options.columnconfigure(0, weight=1)
        ttk.Label(options, textvariable=self.engine_status_var, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.settings_button = ttk.Button(options, text="设置", command=self.toggle_settings)
        self.settings_button.grid(row=0, column=1)
        self.log_button = ttk.Button(options, text="运行详情", command=self.toggle_log)
        self.log_button.grid(row=0, column=2, padx=(8, 0))

        self.engine_frame = ttk.Frame(self, style="Page.TFrame")
        self.engine_frame.columnconfigure(0, weight=1)
        self.engine_hint = ttk.Label(self.engine_frame, text="官方合成引擎（JSR 安装目录 / resources / jsr_backend.exe）", style="Muted.TLabel", wraplength=780)
        self.engine_hint.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        self.engine_entry = ttk.Entry(self.engine_frame, textvariable=self.engine_var, state="readonly")
        self.engine_entry.grid(row=1, column=0, sticky="ew")
        self.engine_button = ttk.Button(self.engine_frame, text="选择…", command=self.choose_engine)
        self.engine_button.grid(row=1, column=1, padx=(8, 0))

        self.status_label = ttk.Label(self, textvariable=self.status_var, style="Status.TLabel", wraplength=780)
        self.status_label.grid(row=6, column=0, sticky="ew", pady=(12, 7))
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.grid(row=7, column=0, sticky="ew")

        actions = ttk.Frame(self, style="Page.TFrame")
        actions.grid(row=8, column=0, sticky="ew", pady=(13, 0))
        actions.columnconfigure(2, weight=1)
        self.start_button = ttk.Button(actions, text="开始合成", style="Primary.TButton", command=self.start)
        self.start_button.grid(row=0, column=0)
        self.cancel_button = ttk.Button(actions, text="取消", command=self.cancel)
        self.cancel_button.grid(row=0, column=1, padx=(8, 0))
        self.open_folder_button = ttk.Button(actions, text="打开输出目录", command=self.open_output_folder)
        self.open_folder_button.grid(row=0, column=3, padx=(8, 0))
        self.open_file_button = ttk.Button(actions, text="打开结果", command=self.open_result)
        self.open_file_button.grid(row=0, column=4, padx=(8, 0))
        self.result_label = ttk.Label(self, textvariable=self.result_var, style="Muted.TLabel", wraplength=780)
        self.result_label.grid(row=9, column=0, sticky="ew", pady=(8, 0))

        self.log_frame = ttk.Frame(self, style="Page.TFrame")
        self.log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(self.log_frame, height=8, wrap="word", state="disabled", background="#17251f", foreground="#d6e3da", relief="flat", borderwidth=0, padx=10, pady=8, font=("Consolas", 9))
        self.log_text.grid(row=0, column=0, sticky="ew")
        log_scroll = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    @property
    def running(self) -> bool:
        return self.state in {"running", "cancelling"}

    def _resize_labels(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        # Tk sizes fonts in points but geometry in pixels; use real dimensions
        # so the same layout remains readable on a high-DPI Windows display.
        width = max(200, event.width - 48)
        for label in (self.status_label, self.result_label, self.engine_hint):
            label.configure(wraplength=width)
        self.camera_label.configure(wraplength=max(200, width - 36))
        self.parameters_label.configure(wraplength=max(200, width - 36))

    def _fit_window(self) -> None:
        self.root.update_idletasks()
        requested_width = self.root.winfo_reqwidth()
        requested_height = self.root.winfo_reqheight()
        width_limit = max(640, self.root.winfo_screenwidth() - 100)
        height_limit = max(480, self.root.winfo_screenheight() - 120)
        width = min(width_limit, max(880, requested_width, self.root.winfo_width()))
        height = min(height_limit, max(680, requested_height, self.root.winfo_height()))
        self.root.minsize(min(requested_width, width_limit), min(max(570, requested_height - 140), height_limit))
        self.root.geometry(f"{width}x{height}")

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_var.set(message)
        self.status_label.configure(style="Error.TLabel" if error else "Status.TLabel")

    def _refresh_actions(self) -> None:
        busy = self.running
        for widget in (self.choose_button, self.output_entry, self.output_button, self.engine_button):
            widget.configure(state="disabled" if busy else "normal")
        can_start = self.calibration is not None and not busy and bool(self.engine_var.get())
        self.start_button.configure(state="normal" if can_start else "disabled")
        self.cancel_button.configure(state="normal" if self.state == "running" else "disabled")
        have_result = self.result is not None and not busy
        self.open_folder_button.configure(state="normal" if have_result else "disabled")
        self.open_file_button.configure(state="normal" if have_result else "disabled")

    def _remember(self) -> None:
        self.settings.update(engine_path=self.engine_var.get(), input_dir=str(self.input_dir), output_dir=self.output_var.get().strip())
        try:
            save_settings(self.settings)
        except (OSError, ValueError) as exc:
            self.append_log(f"设置未保存，下次可能需要重新选择路径：{exc}")

    def choose_files(self) -> None:
        if self.running:
            return
        selected = filedialog.askopenfilenames(parent=self.root, title="选择同一场景的七张主摄 DNG", initialdir=str(self.input_dir), filetypes=[("原始 DNG 照片", "*.dng"), ("所有文件", "*.*")])
        if not selected:
            return
        paths = tuple(sorted((Path(path) for path in selected), key=lambda path: str(path).casefold()))
        self.input_dir = paths[0].parent
        if not self.output_custom:
            self.output_var.set(str(self.input_dir / "JSR_Easy_Output"))
        self._remember()
        self.begin_validation(paths)

    def begin_validation(self, paths: tuple[Path, ...]) -> None:
        """Invalidate a previous selection before the asynchronous read begins."""
        if self.running:
            return
        self.validation_generation += 1
        generation = self.validation_generation
        self.paths = paths
        self.calibration = None
        self.result = None
        self.result_var.set("")
        self.state = "validating"
        self.progress["value"] = 0
        self.count_var.set(f"已选择 {len(paths)} / 7 张照片")
        self.files.delete(0, "end")
        for index, path in enumerate(paths, 1):
            self.files.insert("end", f"  {index:02d}   {path.name}")
        self.camera_var.set("正在核对镜头、ISO、快门和照片尺寸…")
        self.parameters_var.set("正在读取 DNG 噪声模型…")
        self._set_status("检查照片中，请稍候。")
        self._refresh_actions()

        def work() -> None:
            try:
                calibration = validate_burst(paths)
                self.events.put(("validated", generation, calibration))
            except Exception as exc:
                self.events.put(("validation_error", generation, str(exc)))

        threading.Thread(target=work, name="DNG metadata validation", daemon=True).start()

    def choose_output(self) -> None:
        if self.running:
            return
        initial = _existing_directory(self.output_var.get(), self.input_dir)
        selected = filedialog.askdirectory(parent=self.root, title="选择合成结果保存目录", initialdir=str(initial), mustexist=False)
        if selected:
            self.output_var.set(selected)
            self.output_custom = True
            self._remember()

    def choose_engine(self) -> None:
        if self.running:
            return
        current = Path(self.engine_var.get()) if self.engine_var.get() else None
        selected = filedialog.askopenfilename(parent=self.root, title="选择官方 JSR 的 jsr_backend.exe", initialdir=str(current.parent if current and current.parent.is_dir() else self.input_dir), filetypes=[("JSR 合成引擎", "jsr_backend.exe"), ("可执行文件", "*.exe")])
        if not selected:
            return
        # The executable is validated by the worker before any reconstruction.
        self.engine_var.set(selected)
        self.engine_status_var.set("合成引擎：已选择")
        self._remember()
        self._refresh_actions()

    def toggle_settings(self) -> None:
        self._settings_visible = not self._settings_visible
        if self._settings_visible:
            self.engine_frame.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        else:
            self.engine_frame.grid_remove()
        self._fit_window()

    def toggle_log(self) -> None:
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.log_frame.grid(row=10, column=0, sticky="ew", pady=(10, 0))
        else:
            self.log_frame.grid_remove()
        self.log_button.configure(text="收起详情" if self._log_visible else "运行详情")
        self._fit_window()

    def append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 1200:
            self.log_text.delete("1.0", "201.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def start(self) -> None:
        if self.running or self.calibration is None:
            return
        engine_text = self.engine_var.get().strip()
        output_text = self.output_var.get().strip()
        if not engine_text or not output_text:
            self._set_status("请选择合成引擎和保存位置。", error=True)
            return
        output = Path(output_text).expanduser()
        if not output.is_absolute():
            self._set_status("保存位置请使用完整路径，例如 F:\\JSR_Easy_Output。", error=True)
            return
        self._remember()
        self.state = "running"
        self.result = None
        self.result_var.set("")
        self.progress["value"] = 0
        self.run_generation += 1
        generation = self.run_generation
        self.cancel_event = threading.Event()
        calibration = self.calibration
        self._set_status("准备合成…")
        self.append_log("开始新任务。")
        self._refresh_actions()

        def event(kind: str, value: object) -> None:
            self.events.put(("runner_event", generation, kind, value))

        def work() -> None:
            try:
                engine = validate_engine(Path(engine_text))
                result = run_burst(calibration, engine, output, on_event=event, cancel=self.cancel_event)
                self.events.put(("run_result", generation, result))
            except Cancelled:
                self.events.put(("run_cancelled", generation))
            except Exception as exc:
                self.events.put(("run_error", generation, str(exc)))

        self.worker = threading.Thread(target=work, name="JSR reconstruction", daemon=False)
        self.worker.start()

    def cancel(self) -> None:
        if self.state != "running":
            return
        self.cancel_event.set()
        self.state = "cancelling"
        self._set_status("正在取消，请等待合成引擎退出…")
        self._refresh_actions()

    def _handle_event(self, item: tuple[Any, ...]) -> None:
        kind, generation, *payload = item
        if kind in {"validated", "validation_error"}:
            if generation != self.validation_generation or self.running:
                return
            if kind == "validation_error":
                self.state = "failed"
                self.calibration = None
                self.camera_var.set("照片检查未通过")
                self.parameters_var.set("请按下方提示调整本次选择。")
                self._set_status(payload[0], error=True)
                self.append_log(payload[0])
            else:
                self.calibration = payload[0]
                calibration = self.calibration
                self.state = "ready"
                self.camera_var.set(f"1× 主摄  ·  ISO {calibration.iso}  ·  {_format_shutter(calibration.shutter_seconds)}  ·  七张设置一致")
                self.parameters_var.set(f"照片噪声模型估算：读噪 {calibration.read_noise_dn:.3g} DN  ·  黑电平 {calibration.black_dn:g} DN  ·  有效满阱 {calibration.fullwell_e:.0f} e⁻")
                self._set_status("检查通过，可以开始合成。" if self.engine_var.get() else "照片检查通过，请在设置中选择合成引擎。")
            self._refresh_actions()
            return
        if generation != self.run_generation:
            return
        if kind == "runner_event":
            event_kind, value = payload
            if event_kind == "log":
                self.append_log(str(value))
            elif event_kind == "status" and self.state != "cancelling":
                self._set_status(str(value))
            elif event_kind == "progress":
                try:
                    self.progress["value"] = max(0.0, min(100.0, float(value)))
                except (TypeError, ValueError):
                    pass
        elif kind == "run_result":
            self.result = payload[0]
            self.state = "complete"
            self.progress["value"] = 100
            self._set_status("合成完成。")
            self.result_var.set(str(self.result.output_file))
            self.append_log(f"输出：{self.result.output_file}")
            self._refresh_actions()
        elif kind == "run_cancelled":
            self.state = "ready"
            self._set_status("任务已取消，可以重新开始。")
            self.append_log("任务已取消。")
            self._refresh_actions()
        elif kind == "run_error":
            self.state = "failed"
            self._set_status(f"合成未完成：{payload[0]}", error=True)
            self.append_log(str(payload[0]))
            self._refresh_actions()

    def _poll_events(self) -> None:
        self._poll_id = None
        # Bound each drain so a noisy backend cannot starve the UI event loop.
        for _ in range(200):
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(item)
        if self.close_when_idle and not self.running and (self.worker is None or not self.worker.is_alive()):
            self.root.destroy()
            return
        self._poll_id = self.root.after(80, self._poll_events)

    def open_output_folder(self) -> None:
        if self.result is not None:
            self._open(self.result.output_file.parent)

    def open_result(self) -> None:
        if self.result is not None:
            self._open(self.result.output_file)

    def _open(self, path: Path) -> None:
        try:
            _open_path(path)
        except OSError as exc:
            messagebox.showerror("无法打开", str(exc), parent=self.root)

    def request_close(self) -> None:
        if self.running or (self.worker is not None and self.worker.is_alive()):
            if not self.close_when_idle:
                if not messagebox.askokcancel("合成仍在运行", "取消当前任务并关闭？程序会等待合成引擎退出。", parent=self.root):
                    return
                self.close_when_idle = True
                self.cancel_event.set()
                if self.running:
                    self.state = "cancelling"
                self._set_status("正在取消，合成引擎退出后将自动关闭…")
                self._refresh_actions()
            return
        if self._poll_id is not None:
            self.root.after_cancel(self._poll_id)
            self._poll_id = None
        self.root.destroy()


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.withdraw()
    Application(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
