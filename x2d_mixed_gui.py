"""Window for x2d_mixed_slice.py: open a Bambu Studio project, choose a nozzle side per filament, slice.

Start it with "X2D Mixed Nozzle.command" (macOS) or "X2D Mixed Nozzle.bat" (Windows), or run
    python3 x2d_mixed_gui.py [project.3mf]
Needs Python 3.9 or newer with Tkinter, and x2d_mixed_slice.py, flatten.py and fix_plate_diameters.py in
the same folder. The window only collects the options and runs x2d_mixed_slice.py; see README.md.
"""
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import x2d_mixed_slice as slicer  # noqa: E402  (DEFAULT_CLI, fmt)

SCRIPT = HERE / "x2d_mixed_slice.py"
TITLE = "X2D Mixed Nozzle"
SIZES = ("0.2", "0.4", "0.6", "0.8")
CREATE_NO_WINDOW = 0x08000000
GOOD, BAD, WARN, DIM = "#1a7f37", "#cf222e", "#b35900", "#6e7781"


def short_name(preset):
    """'Bambu PLA Basic @BBL X2D 0.4 nozzle' -> 'Bambu PLA Basic'."""
    return re.sub(r" @.*$", "", preset)


def swatch_colour(value):
    m = re.fullmatch(r"(#[0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?", value or "")
    return m.group(1) if m else "#FFFFFF"


def read_project(path):
    """The project's settings and plate count; ValueError with a plain message if it can't be used."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "Metadata/project_settings.config" not in names:
                raise ValueError("This file has no Bambu Studio project settings.\n"
                                 "Open it in Bambu Studio, save the project and try again.")
            settings = json.loads(z.read("Metadata/project_settings.config"))
            model = (z.read("Metadata/model_settings.config").decode(errors="replace")
                     if "Metadata/model_settings.config" in names else "")
    except (zipfile.BadZipFile, OSError):
        raise ValueError("This is not a 3MF project file.")
    printer = settings.get("printer_model")
    if printer != "Bambu Lab X2D":
        raise ValueError(f"The project is set up for {printer or 'an unknown printer'}, not the Bambu Lab X2D.\n"
                         "Select the X2D printer in Bambu Studio, save the project and try again.")
    if not settings.get("filament_settings_id"):
        raise ValueError("The project has no filaments.")
    return settings, max(1, model.count("<plate>"))


def as_height(text):
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None
    return value if 0.02 <= value <= 1.0 else None


def num(value):
    return f"{value:.3f}".rstrip("0").rstrip(".")


class App(tk.Tk):
    def __init__(self, project=None):
        super().__init__()
        self.title(TITLE)
        self.minsize(660, 520)
        self.proc = None
        self.cancelled = False
        self.lines = queue.Queue()
        self.settings = None
        self.plates = 1
        self.slot_vars = []
        self.slot_radios = []
        self.out_auto = True
        self.last_error = ""
        self.saw_problems = False

        self.project = tk.StringVar()
        self.main_d = tk.StringVar(value="0.2")
        self.aux_d = tk.StringVar(value="0.4")
        self.layer = tk.StringVar()
        self.first = tk.StringVar()
        self.tower = tk.StringVar()
        self.plate = tk.StringVar()
        self.out = tk.StringVar()
        self.cli = tk.StringVar(value=slicer.DEFAULT_CLI or "")
        self.profiles = tk.StringVar()
        self.show_advanced = tk.BooleanVar(value=not slicer.DEFAULT_CLI)

        bold = font.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self.bold = bold
        self.build()
        self.layer.trace_add("write", lambda *_: self.update_hint())
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll_id = self.after(100, self.poll)
        if not slicer.DEFAULT_CLI:
            self.set_status("Bambu Studio was not found. Set its location under Advanced.", BAD)
        if project:
            self.load(project)

    # ---------- layout ----------
    def build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        row = 0

        intro = ("Slices a Bambu Studio X2D project with a different nozzle size on each side.\n"
                 "1. Open the project (saved in Bambu Studio on the X2D 0.4 nozzle printer).  "
                 "2. Pick the nozzle for each filament.  3. Press Slice, then open the result in Bambu Studio and print.")
        ttk.Label(root, text=intro, wraplength=620, justify="left").grid(row=row, column=0, sticky="we", pady=(0, 8))
        row += 1

        box = ttk.LabelFrame(root, text="1. Project", padding=8)
        box.grid(row=row, column=0, sticky="we", pady=4)
        box.columnconfigure(0, weight=1)
        ttk.Entry(box, textvariable=self.project, state="readonly").grid(row=0, column=0, sticky="we")
        ttk.Button(box, text="Open…", command=self.browse_project).grid(row=0, column=1, padx=(6, 0))
        self.info = ttk.Label(box, text="No project open.", foreground=DIM, wraplength=600, justify="left")
        self.info.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 1

        box = ttk.LabelFrame(root, text="2. Nozzles and filaments", padding=8)
        box.grid(row=row, column=0, sticky="we", pady=4)
        noz = ttk.Frame(box)
        noz.grid(row=0, column=0, sticky="w")
        ttk.Label(noz, text="Main (Direct Drive) nozzle:").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(noz, textvariable=self.main_d, values=SIZES, width=5, state="readonly")
        cb.grid(row=0, column=1, padx=(4, 18))
        cb.bind("<<ComboboxSelected>>", lambda e: self.nozzles_changed())
        ttk.Label(noz, text="Auxiliary (Bowden) nozzle:").grid(row=0, column=2, sticky="w")
        cb = ttk.Combobox(noz, textvariable=self.aux_d, values=SIZES, width=5, state="readonly")
        cb.grid(row=0, column=3, padx=(4, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.nozzles_changed())
        self.noz_warning = ttk.Label(box, text="", foreground=BAD)
        self.noz_warning.grid(row=1, column=0, sticky="w")
        self.slots = ttk.Frame(box)
        self.slots.grid(row=2, column=0, sticky="we", pady=(6, 0))
        ttk.Label(box, text="Each filament slot prints on one side only. To print the same spool with both nozzles, "
                            "give it two slots in Bambu Studio. Unused slots can stay on either side.",
                  foreground=DIM, wraplength=600, justify="left").grid(row=3, column=0, sticky="w", pady=(6, 0))
        row += 1

        box = ttk.LabelFrame(root, text="3. Settings", padding=8)
        box.grid(row=row, column=0, sticky="we", pady=4)
        ttk.Label(box, text="Layer height (mm):").grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.layer, width=7).grid(row=0, column=1, sticky="w", padx=(4, 24))
        ttk.Label(box, text="First layer (mm):").grid(row=0, column=2, sticky="w")
        ttk.Entry(box, textvariable=self.first, width=7).grid(row=0, column=3, sticky="w", padx=4)
        self.hint = ttk.Label(box, text="", foreground=DIM)
        self.hint.grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Label(box, text="Prime tower:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.tower_cb = ttk.Combobox(box, textvariable=self.tower, width=18, state="readonly")
        self.tower_cb.grid(row=2, column=1, sticky="w", padx=(4, 24), pady=(6, 0))
        ttk.Label(box, text="Plate:").grid(row=2, column=2, sticky="w", pady=(6, 0))
        self.plate_cb = ttk.Combobox(box, textvariable=self.plate, width=12, state="readonly")
        self.plate_cb.grid(row=2, column=3, sticky="w", padx=4, pady=(6, 0))
        row += 1

        box = ttk.LabelFrame(root, text="Output file", padding=8)
        box.grid(row=row, column=0, sticky="we", pady=4)
        box.columnconfigure(0, weight=1)
        ttk.Entry(box, textvariable=self.out, state="readonly").grid(row=0, column=0, sticky="we")
        ttk.Button(box, text="Save as…", command=self.browse_out).grid(row=0, column=1, padx=(6, 0))
        row += 1

        ttk.Checkbutton(root, text="Advanced", variable=self.show_advanced,
                        command=self.toggle_advanced).grid(row=row, column=0, sticky="w", pady=(4, 0))
        row += 1
        self.advanced = ttk.Frame(root, padding=(18, 0, 0, 0))
        self.advanced.columnconfigure(1, weight=1)
        ttk.Label(self.advanced, text="Bambu Studio:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.advanced, textvariable=self.cli).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(self.advanced, text="Browse…", command=self.browse_cli).grid(row=0, column=2)
        ttk.Label(self.advanced, text="Profiles folder:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(self.advanced, textvariable=self.profiles).grid(row=1, column=1, sticky="we", padx=4, pady=(4, 0))
        ttk.Button(self.advanced, text="Browse…", command=self.browse_profiles).grid(row=1, column=2, pady=(4, 0))
        ttk.Label(self.advanced, text="Leave the profiles folder empty to use the one inside Bambu Studio.",
                  foreground=DIM).grid(row=2, column=1, sticky="w", padx=4)
        self.advanced_row = row
        row += 1

        bar = ttk.Frame(root)
        bar.grid(row=row, column=0, sticky="we", pady=(10, 4))
        bar.columnconfigure(2, weight=1)
        self.slice_btn = ttk.Button(bar, text="Slice", command=self.start)
        self.slice_btn.grid(row=0, column=0)
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=6)
        self.progress = ttk.Progressbar(bar, mode="indeterminate")
        self.progress.grid(row=0, column=2, sticky="we", padx=6)
        self.open_btn = ttk.Button(bar, text="Open in Bambu Studio", command=self.open_result, state="disabled")
        self.open_btn.grid(row=0, column=3)
        self.show_btn = ttk.Button(bar, text="Show file", command=self.show_result, state="disabled")
        self.show_btn.grid(row=0, column=4, padx=(6, 0))
        row += 1

        self.status = ttk.Label(root, text="", font=self.bold, wraplength=620, justify="left")
        self.status.grid(row=row, column=0, sticky="we", pady=(2, 4))
        row += 1

        logbox = ttk.Frame(root)
        logbox.grid(row=row, column=0, sticky="nsew")
        root.rowconfigure(row, weight=1)
        logbox.columnconfigure(0, weight=1)
        logbox.rowconfigure(0, weight=1)
        self.log = tk.Text(logbox, height=6, wrap="word", font=font.nametofont("TkFixedFont"),
                           state="disabled", relief="solid", borderwidth=1)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(logbox, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        for tag, colour in (("bad", BAD), ("good", GOOD), ("warn", WARN), ("dim", DIM)):
            self.log.tag_configure(tag, foreground=colour)

        self.toggle_advanced()
        self.update_hint()

    def toggle_advanced(self):
        if self.show_advanced.get():
            self.advanced.grid(row=self.advanced_row, column=0, sticky="we")
        else:
            self.advanced.grid_remove()

    def build_slots(self):
        for child in self.slots.winfo_children():
            child.destroy()
        self.slot_vars, self.slot_radios = [], []
        s = self.settings
        names = s["filament_settings_id"]
        colours = s.get("filament_colour", [])
        types = s.get("filament_type", [])
        for col, text in enumerate(("Slot", "", "Filament", "Prints with")):
            ttk.Label(self.slots, text=text, foreground=DIM).grid(row=0, column=col, sticky="w", padx=(0, 8))
        for i, name in enumerate(names):
            r = i + 1
            ttk.Label(self.slots, text=str(r)).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=1)
            colour = swatch_colour(colours[i] if i < len(colours) else "")
            tk.Label(self.slots, width=3, background=colour, relief="solid", borderwidth=1).grid(
                row=r, column=1, sticky="w", padx=(0, 8), pady=1)
            kind = types[i] if i < len(types) else ""
            label = short_name(name) + (f"  ({kind})" if kind and kind not in name else "")
            ttk.Label(self.slots, text=label).grid(row=r, column=2, sticky="w", padx=(0, 12), pady=1)
            var = tk.StringVar(value="aux")
            main = ttk.Radiobutton(self.slots, variable=var, value="main")
            aux = ttk.Radiobutton(self.slots, variable=var, value="aux")
            main.grid(row=r, column=3, sticky="w", padx=(0, 10))
            aux.grid(row=r, column=4, sticky="w")
            self.slot_vars.append(var)
            self.slot_radios.append((main, aux))
        self.nozzles_changed()

    # ---------- state ----------
    def nozzles_changed(self):
        m, a = self.main_d.get(), self.aux_d.get()
        for main, aux in self.slot_radios:
            main.configure(text=f"Main {m} mm")
            aux.configure(text=f"Auxiliary {a} mm")
        self.noz_warning.configure(text="Main and Auxiliary must have different nozzle sizes." if m == a else "")
        self.update_hint()
        if self.out_auto and self.project.get():
            p = Path(self.project.get())
            stem = p.name[:-len(".3mf")] if p.name.lower().endswith(".3mf") else p.stem
            stem = stem[:-len(".gcode")] if stem.endswith(".gcode") else stem
            self.out.set(str(p.with_name(f"{stem}_X2D_Main{slicer.fmt(m)}_Aux{slicer.fmt(a)}.gcode.3mf")))

    def update_hint(self):
        h = as_height(self.layer.get())
        m, a = self.main_d.get(), self.aux_d.get()
        if not self.layer.get():
            text = ""
        elif h is None:
            text = "enter a number such as 0.16"
        elif {m, a} == {"0.2", "0.4"}:
            text = f"the 0.2 nozzle prints {num(h / 2)} mm layers, the 0.4 prints {num(h)} mm"
        else:
            text = "the smaller nozzle prints thinner layers"
        self.hint.configure(text=text)

    def set_status(self, text, colour=None):
        self.status.configure(text=text, foreground=colour or "")

    def append(self, line):
        tag = None
        if line.startswith(("error", "PROBLEMS", "Traceback")) or "do not print" in line:
            tag = "bad"
        elif line.startswith("warning"):
            tag = "warn"
        elif line.startswith("done:"):
            tag = "good"
        if line.startswith("error:"):
            self.last_error = line[len("error:"):].strip()
        if line.startswith("PROBLEMS"):
            self.saw_problems = True
        self.log.configure(state="normal")
        self.log.insert("end", line, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---------- files ----------
    def browse_project(self):
        start = Path(self.project.get()).parent if self.project.get() else Path.home()
        path = filedialog.askopenfilename(title="Open Bambu Studio project", initialdir=str(start),
                                          filetypes=[("Bambu Studio project", "*.3mf"), ("All files", "*")])
        if path:
            self.load(path)

    def load(self, path):
        try:
            settings, plates = read_project(path)
        except ValueError as e:
            messagebox.showerror(TITLE, str(e), parent=self)
            return
        self.settings, self.plates = settings, plates
        self.project.set(str(Path(path).resolve()))
        self.out_auto = True
        self.layer.set(settings.get("layer_height", ""))
        self.first.set(settings.get("initial_layer_print_height", ""))
        tower = "on" if settings.get("enable_prime_tower") == "1" else "off"
        self.tower_cb.configure(values=(f"As in project ({tower})", "On", "Off"))
        self.tower_cb.current(0)
        self.plate_cb.configure(values=["All plates"] + [f"Plate {n}" for n in range(1, plates + 1)])
        self.plate_cb.current(0)
        n = len(settings["filament_settings_id"])
        info = (f"Printer preset: {settings.get('printer_settings_id', '?')}  ·  "
                f"{n} filament slot{'s' if n != 1 else ''}  ·  {plates} plate{'s' if plates != 1 else ''}")
        variant = settings.get("printer_variant")
        if variant and slicer.fmt(variant) != "0.4":
            info += (f"\nMade on the X2D {variant} nozzle preset: it will be switched to the 0.4 nozzle preset, "
                     "and changes to the process settings are not kept.")
        self.info.configure(text=info, foreground="")
        self.build_slots()
        self.open_btn.configure(state="disabled")
        self.show_btn.configure(state="disabled")
        self.clear_log()
        self.set_status("Pick the nozzle for each filament, then press Slice.")

    def browse_out(self):
        current = Path(self.out.get()) if self.out.get() else None
        path = filedialog.asksaveasfilename(
            title="Save sliced file as", defaultextension=".3mf",
            initialdir=str(current.parent) if current else str(Path.home()),
            initialfile=current.name if current else "",
            filetypes=[("Sliced 3MF", "*.gcode.3mf"), ("3MF", "*.3mf")])
        if path:
            if not path.lower().endswith(".3mf"):
                path += ".gcode.3mf"
            self.out.set(path)
            self.out_auto = False

    def browse_cli(self):
        if sys.platform == "darwin":
            app = filedialog.askopenfilename(title="Select Bambu Studio", initialdir="/Applications",
                                             filetypes=[("Application", "*.app")])
            if app:
                exe = next((Path(app) / "Contents" / "MacOS").glob("BambuStudio*"), None)
                self.cli.set(str(exe or app))
        else:
            exe = filedialog.askopenfilename(title="Select bambu-studio.exe",
                                             filetypes=[("Program", "*.exe"), ("All files", "*")])
            if exe:
                self.cli.set(exe)

    def browse_profiles(self):
        folder = filedialog.askdirectory(title="Select the profiles/BBL folder")
        if folder:
            self.profiles.set(folder)

    # ---------- running ----------
    def start(self):
        if self.proc:
            return
        if not self.settings:
            messagebox.showinfo(TITLE, "Open a project first.", parent=self)
            return
        m, a = self.main_d.get(), self.aux_d.get()
        if m == a:
            messagebox.showerror(TITLE, "Main and Auxiliary must have different nozzle sizes.", parent=self)
            return
        layer, first = as_height(self.layer.get()), as_height(self.first.get())
        if layer is None or first is None:
            messagebox.showerror(TITLE, "Layer height and first layer must be numbers such as 0.16.", parent=self)
            return
        cli = self.cli.get().strip()
        if not cli or not Path(cli).is_file():
            self.show_advanced.set(True)
            self.toggle_advanced()
            messagebox.showerror(TITLE, "Bambu Studio was not found. Set its location under Advanced.", parent=self)
            return
        out = self.out.get()
        if Path(out).exists() and not messagebox.askyesno(
                TITLE, f"{Path(out).name} already exists. Replace it?", parent=self):
            return

        diameters = [m if v.get() == "main" else a for v in self.slot_vars]
        cmd = [sys.executable, "-I", "-X", "utf8", str(SCRIPT), self.project.get(),
               "--filaments", ",".join(diameters), "--main", m, "--aux", a, "--out", out, "--cli", cli]
        if abs(layer - float(self.settings.get("layer_height", 0) or 0)) > 1e-6:
            cmd += ["--layer-height", num(layer)]
        if abs(first - float(self.settings.get("initial_layer_print_height", 0) or 0)) > 1e-6:
            cmd += ["--first-layer", num(first)]
        tower = self.tower_cb.current()
        if tower > 0:
            cmd += ["--prime-tower", "on" if tower == 1 else "off"]
        plate = self.plate_cb.current()
        if plate > 0:
            cmd += ["--plate", str(plate)]
        if self.profiles.get().strip():
            cmd += ["--profiles", self.profiles.get().strip()]

        self.clear_log()
        self.append("$ " + " ".join(shlex.quote(c) for c in cmd[3:]) + "\n")
        self.log.tag_add("dim", "1.0", "1.end")
        self.last_error, self.saw_problems, self.cancelled = "", False, False
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW} if os.name == "nt" \
            else {"start_new_session": True}
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                         bufsize=1, **kwargs)
        except OSError as e:
            messagebox.showerror(TITLE, f"Could not start the slicer script:\n{e}", parent=self)
            return
        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()
        self.set_status("Slicing… this takes a minute or two.")
        for w in (self.slice_btn, self.open_btn, self.show_btn):
            w.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.start(12)

    def read_output(self, proc):
        for line in proc.stdout:
            self.lines.put(line)
        self.lines.put(proc.wait())

    def poll(self):
        try:
            while True:
                item = self.lines.get_nowait()
                if isinstance(item, int):
                    self.finished(item)
                else:
                    self.append(item)
        except queue.Empty:
            pass
        self.poll_id = self.after(100, self.poll)

    def finished(self, code):
        self.proc = None
        self.progress.stop()
        self.slice_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        name = Path(self.out.get()).name
        if self.cancelled:
            self.set_status("Cancelled.", WARN)
        elif code == 0:
            self.set_status(f"Done: {name}\nOpen it in Bambu Studio (File > Open Project), press Print, and load "
                            "each filament on the side of its nozzle.", GOOD)
            self.open_btn.configure(state="normal")
            self.show_btn.configure(state="normal")
        elif self.saw_problems:
            self.set_status("The check found problems. Do not print this file. Details below.", BAD)
        elif self.last_error:
            self.set_status("Could not slice: " + self.last_error, BAD)
        else:
            self.set_status(f"Could not slice (exit code {code}). Details below.", BAD)

    def cancel(self):
        proc = self.proc
        if not proc:
            return
        self.cancelled = True
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    def on_close(self):
        if self.proc:
            if not messagebox.askyesno(TITLE, "Slicing is still running. Stop it and close?", parent=self):
                return
            self.cancel()
        self.destroy()

    def destroy(self):
        self.after_cancel(self.poll_id)
        super().destroy()

    # ---------- result ----------
    def open_result(self):
        out, cli = self.out.get(), Path(self.cli.get())
        try:
            if sys.platform == "darwin":
                app = next((p for p in cli.parents if p.suffix == ".app"), None)
                subprocess.Popen(["open", "-a", str(app), out] if app else ["open", out])
            else:
                subprocess.Popen([str(cli), out])
        except OSError as e:
            messagebox.showerror(TITLE, f"Could not open Bambu Studio:\n{e}", parent=self)

    def show_result(self):
        out = self.out.get()
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", out])
            elif os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(out)])
            else:
                subprocess.Popen(["xdg-open", str(Path(out).parent)])
        except OSError as e:
            messagebox.showerror(TITLE, f"Could not show the file:\n{e}", parent=self)


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()


if __name__ == "__main__":
    main()
