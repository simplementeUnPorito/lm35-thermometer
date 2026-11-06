"""Interfaz Tkinter + matplotlib (español, pensada para 1366x768)."""
from __future__ import annotations

import json
import os
import shutil
import uuid
import statistics
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import serial.tools.list_ports

from . import protocol as P
from .calibration import CalibrationStore, CalibrationError, DEFAULT_VFS
from .cleanup import CleanupError, cleanup_data
from .demo import FakeSerial
from .history import History, HistoryError, load_history
from .recalc import recalc_csv, RecalcError
from .worker import SerialWorker, SessionSettings, PlotBuffer, open_serial

WINDOW_S = 10.0
MIN_SPAN_S = 0.05
LIVE_XLABEL = "Tiempo [s] (ventana deslizante de 10 s)"
STOP_XLABEL = "Tiempo de la experiencia [s] (arrastre con el ratón para desplazar, rueda para zoom)"
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]
DEMO_TEXT = "SIMULADO – DATOS DE DEMOSTRACIÓN (sin hardware)"


def default_data_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datos")


def parse_float(s: str) -> float:
    return float(str(s).strip().replace(",", "."))


class App(tk.Tk):
    def destroy(self):
        # Cancel callbacks before removing the Tcl widgets (also used by smoke tests).
        if getattr(self, "_tick_timer", None):
            self.after_cancel(self._tick_timer)
        canvas = getattr(self, "canvas", None)
        idle_draw = getattr(canvas, "_idle_draw_id", None)
        if idle_draw:
            # Cancel at Tcl level; the canvas still owns and deletes its callback command.
            self.tk.call("after", "cancel", idle_draw)
            canvas._idle_draw_id = None
        super().destroy()

    def __init__(self, data_dir=None, demo=False):
        super().__init__()
        self.title("LM35 Thermometer")
        self.geometry("1340x700+0+0")
        self.minsize(1100, 620)
        self.data_dir = os.path.abspath(data_dir or default_data_dir())
        os.makedirs(self.data_dir, exist_ok=True)
        self.settings_path = os.path.join(self.data_dir, "ajustes.json")
        self.worker: SerialWorker | None = None
        self.plot = PlotBuffer()
        self._last_state = ""
        self.store = None
        self._mode = "idle"          # idle | live | frozen (esperando CSV) | history
        self._history: History | None = None
        self._load_gen = 0
        self._loader = None
        self._load_result = None
        self._drag = None
        self._view = (0.0, WINDOW_S)
        self._build_vars(demo)
        self._build_ui()
        self._load_store()
        self._load_settings()
        self.v_pc_average_draft.set(str(self._pc_average()))
        self.v_pc_average.trace_add("write", self._on_pc_average_change)
        self._update_average_duration()
        self._update_plan_label()
        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._tick_timer = self.after(200, self._tick)

    # ------------------------------------------------------------------ vars
    def _build_vars(self, demo):
        self.v_port = tk.StringVar()
        self.v_baud = tk.StringVar(value="921600")
        self.v_n = tk.IntVar(value=1)
        self.v_rate = tk.StringVar(value="100")
        self.v_pc_average = tk.IntVar(value=20)
        self.v_pc_average_draft = tk.StringVar(value="20")
        self.v_pc_average_error = tk.StringVar()
        self.v_pc_average_duration = tk.StringVar()
        self.v_demo = tk.BooleanVar(value=demo)
        self.v_plan = tk.StringVar()
        self.v_status = tk.StringVar(value="Detenido")
        self.v_cal_sensor = tk.StringVar(value="S1")
        self.v_cal_ref = tk.StringVar()
        self.v_cal_adc = tk.StringVar()
        self.v_cal_name = tk.StringVar()
        self.v_cal_live = tk.StringVar(value="ADC actual: –")
        self.v_cal_status = tk.StringVar(value="")
        self.v_cal_info = tk.StringVar(value="")
        self.v_stats = tk.StringVar()
        self.v_experience = tk.StringVar(value="medicion")
        self.v_enabled = [tk.BooleanVar(value=(i == 0)) for i in range(6)]
        self.v_sensor_names = [tk.StringVar(value=f"S{i+1}") for i in range(6)]
        self.v_sensor_live = [tk.StringVar(value="ADC – · °C –") for _ in range(6)]
        self.sensor_photos = ["" for _ in range(6)]
        self.v_photo = [tk.StringVar(value="Sin foto") for _ in range(6)]
        self._restart_pending = False

    # -------------------------------------------------------------------- UI
    def _build_ui(self):
        s = ttk.Style(self)
        s.configure("Banner.TLabel", background="#b00020", foreground="white", font=("Segoe UI", 11, "bold"))
        s.configure("Big.TButton", font=("Segoe UI", 10, "bold"))
        self.banner = ttk.Label(self, text=DEMO_TEXT, style="Banner.TLabel", anchor="center")
        cleanup_bar = ttk.Frame(self)
        cleanup_bar.pack(side="top", fill="x", padx=6, pady=(4, 0))
        self.cleanup_buttons = [
            ttk.Button(cleanup_bar, text="Eliminar registros", command=lambda: self._cleanup("records")),
            ttk.Button(cleanup_bar, text="Eliminar calibraciones", command=lambda: self._cleanup("calibrations")),
            ttk.Button(cleanup_bar, text="Eliminar todo", command=lambda: self._cleanup("all")),
        ]
        for button in self.cleanup_buttons:
            button.pack(side="left", padx=(0, 4))
        top = ttk.LabelFrame(self, text="Conexión y muestreo")
        top.pack(side="top", fill="x", padx=6, pady=(4, 2))
        r1 = ttk.Frame(top); r1.pack(fill="x", padx=4, pady=2)
        ttk.Label(r1, text="Puerto:").pack(side="left")
        self.cb_port = ttk.Combobox(r1, textvariable=self.v_port, width=9)
        self.cb_port.pack(side="left", padx=2)
        ttk.Button(r1, text="↻", width=3, command=self.refresh_ports).pack(side="left")
        ttk.Label(r1, text="Baudios:").pack(side="left", padx=(8, 0))
        ttk.Entry(r1, textvariable=self.v_baud, width=8).pack(side="left", padx=2)
        ttk.Label(r1, text="Experiencia:").pack(side="left", padx=(8, 0))
        experience_entry = ttk.Entry(r1, textvariable=self.v_experience, width=18)
        experience_entry.pack(side="left", padx=2)
        experience_entry.bind("<FocusOut>", lambda _e: self._metadata_changed())
        ttk.Label(r1, text="Hz por sensor:").pack(side="left", padx=(8, 0))
        ttk.Entry(r1, textvariable=self.v_rate, width=7).pack(side="left", padx=2)
        for v in (self.v_baud, self.v_rate):
            v.trace_add("write", lambda *_: self._on_cfg_change())
        sf = ttk.LabelFrame(top, text="Canales (la selección controla físicamente qué GPIO lee el ESP32)")
        sf.pack(fill="x", padx=4, pady=2)
        for i in range(6):
            row = ttk.Frame(sf); row.grid(row=i // 2, column=i % 2, sticky="ew", padx=5, pady=2)
            ttk.Checkbutton(row, text=f"S{i+1}", variable=self.v_enabled[i],
                            command=self._channels_changed).pack(side="left")
            ttk.Label(row, text=f"GPIO {P.GPIOS[i]}").pack(side="left", padx=3)
            ent = ttk.Entry(row, textvariable=self.v_sensor_names[i], width=13)
            ent.pack(side="left", padx=2)
            ent.bind("<FocusOut>", lambda _e, sid=i: self._save_sensor_name(sid))
            ttk.Label(row, textvariable=self.v_sensor_live[i], width=22).pack(side="left", padx=2)
            ttk.Button(row, text="Foto…", command=lambda sid=i: self.attach_photo(sid)).pack(side="left")
            ttk.Label(row, textvariable=self.v_photo[i], width=12).pack(side="left", padx=2)
            sf.columnconfigure(i % 2, weight=1)
        r2 = ttk.Frame(top); r2.pack(fill="x", padx=4, pady=2)
        self.btn_start = ttk.Button(r2, text="▶ Iniciar", style="Big.TButton", command=self.start_acquisition)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(r2, text="■ Detener", style="Big.TButton", command=self.stop_acquisition, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.chk_demo = ttk.Checkbutton(r2, text="Modo DEMO (simulado, sin hardware)", variable=self.v_demo, command=self._on_demo)
        self.chk_demo.pack(side="left", padx=10)
        ttk.Label(r2, text="Promedio gráfica [muestras]:").pack(side="left", padx=(2, 0))
        self.ent_pc_average = ttk.Entry(r2, textvariable=self.v_pc_average_draft, width=5)
        self.ent_pc_average.pack(side="left", padx=2)
        self.ent_pc_average.bind("<Return>", self._apply_pc_average)
        self.btn_apply_average = ttk.Button(r2, text="Aplicar", command=self._apply_pc_average, width=7)
        self.btn_apply_average.pack(side="left", padx=(0, 2))
        ttk.Label(r2, textvariable=self.v_pc_average_duration, foreground="#555").pack(side="left", padx=(0, 4))
        ttk.Label(r2, textvariable=self.v_pc_average_error, foreground="#b00020",
                  wraplength=170, justify="left").pack(side="left", padx=(0, 4))
        ttk.Button(r2, text="Abrir carpeta de datos", command=self.open_data_dir).pack(side="left")
        ttk.Label(r2, textvariable=self.v_status, font=("Segoe UI", 10, "bold")).pack(side="right", padx=6)
        ttk.Label(top, textvariable=self.v_plan, foreground="#444").pack(fill="x", padx=6)

        self.fig = Figure(figsize=(8, 3.2), dpi=96)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlim(-WINDOW_S, 0)
        self.ax.set_xlabel(LIVE_XLABEL)
        self.ax.set_ylabel("Temperatura [°C]")
        self.ax.grid(True, alpha=0.3)
        self.raw_lines = [self.ax.plot([], [], color="#c8c8c8", alpha=0.5, lw=0.7,
                                       label="_nolegend_", zorder=1)[0] for _ in range(6)]
        self.lines = [self.ax.plot([], [], color=COLORS[i], lw=1.4, label=f"S{i+1}", zorder=2)[0]
                      for i in range(6)]
        self.fig.subplots_adjust(left=0.07, right=0.985, top=0.93, bottom=0.17)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.nb = ttk.Notebook(self)
        self.nb.pack(side="bottom", fill="x", padx=6, pady=(2, 4))
        self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True, padx=6)
        self._tab_readings()
        self._tab_calibration()
        self._tab_events()

    def _tab_readings(self):
        f = ttk.Frame(self.nb); self.nb.add(f, text="Lecturas")
        cols = ("sensor", "gpio", "adc", "temp", "modo", "rev", "calidad")
        self.tv_read = ttk.Treeview(f, columns=cols, show="headings", height=6)
        for c, t, w in zip(cols, ("Sensor", "GPIO", "ADC (vista)", "Temp. [°C]", "Conversión", "Rev.", "Calidad calibración"),
                           (150, 60, 90, 90, 100, 50, 260)):
            self.tv_read.heading(c, text=t); self.tv_read.column(c, width=w, anchor="center")
        self.tv_read.pack(fill="x", padx=4, pady=4)
        for i in range(6):
            self.tv_read.insert("", "end", iid=str(i), values=(f"S{i+1}", P.GPIOS[i], "–", "–", "", "", ""))
        ttk.Label(f, text="Conversión 'nominal' = aproximada (10 mV/°C con plena escala fija de 2,2 V, NO calibrada). "
                          "Use la pestaña Calibración para corregir cada sensor.", foreground="#8a4b00").pack(anchor="w", padx=6)

    def _tab_calibration(self):
        f = ttk.Frame(self.nb); self.nb.add(f, text="Calibración por sensor")
        self.cal_tab = f
        actions = ttk.Frame(f); actions.pack(fill="x", padx=6, pady=(4, 1))
        ttk.Label(actions, text="Sensor:").pack(side="left")
        self.cb_cal_sensor = ttk.Combobox(
            actions, textvariable=self.v_cal_sensor, values=[f"S{i}" for i in range(1, 7)],
            width=4, state="readonly"
        )
        self.cb_cal_sensor.pack(side="left", padx=(3, 6))
        self.cb_cal_sensor.bind("<<ComboboxSelected>>", lambda _e: self.refresh_cal())
        ttk.Label(actions, text="Nombre:").pack(side="left")
        ttk.Entry(actions, textvariable=self.v_cal_name, width=12).pack(side="left", padx=3)
        ttk.Button(actions, text="Renombrar", command=self.cal_rename).pack(side="left", padx=(0, 10))
        ttk.Label(actions, text="Medición real [°C]").pack(side="left")
        ttk.Entry(actions, textvariable=self.v_cal_ref, width=7).pack(side="left", padx=3)
        self.btn_cal_capture = ttk.Button(
            actions, text="Append medición de calibración", command=self.cal_capture
        )
        self.btn_cal_capture.pack(side="left")
        self.btn_cal_reset = ttk.Button(actions, text="Reset", command=self.cal_reset)
        self.btn_cal_reset.pack(side="left", padx=4)

        status = ttk.Frame(f); status.pack(fill="x", padx=6, pady=(0, 2))
        ttk.Label(status, textvariable=self.v_cal_live, foreground="#006400").pack(side="left")
        ttk.Label(status, textvariable=self.v_cal_status, foreground="#145a32").pack(side="left", padx=8)
        self.btn_recalc = ttk.Button(status, text="Recalcular CSV crudo…", command=self.recalc_dialog)
        self.btn_recalc.pack(side="right")

        body = ttk.Frame(f); body.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        mid = ttk.Frame(body); mid.pack(side="left", fill="both", expand=True)
        cols = ("ts", "adc", "ref", "src")
        self.tv_pts = ttk.Treeview(mid, columns=cols, show="headings", height=5, selectmode="extended")
        for c, t, w in zip(cols, ("Fecha/hora UTC", "ADC", "Ref. [°C]", "Origen"), (200, 70, 80, 80)):
            self.tv_pts.heading(c, text=t); self.tv_pts.column(c, width=w, anchor="center")
        sb = ttk.Scrollbar(mid, command=self.tv_pts.yview); self.tv_pts.configure(yscrollcommand=sb.set)
        self.tv_pts.pack(side="left", fill="both", expand=True); sb.pack(side="left", fill="y")
        right = ttk.Frame(body); right.pack(side="left", fill="y", padx=(6, 0))
        manual = ttk.Frame(right); manual.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        ttk.Label(manual, text="Manual ADC:").pack(side="left")
        ttk.Entry(manual, textvariable=self.v_cal_adc, width=7).pack(side="left", padx=3)
        ttk.Label(manual, text="(usa medición real)").pack(side="left")
        ttk.Button(manual, text="Agregar punto manual", command=self.cal_add_manual).pack(side="left", padx=3)
        for pos, (t, cmd) in enumerate((("Eliminar punto(s)", self.cal_remove),
                                        ("Importar puntos CSV…", self.cal_import),
                                        ("Exportar puntos CSV…", self.cal_export),
                                        ("Calcular y aplicar ajuste", self.cal_apply),
                                        ("Desactivar calibración", self.cal_revert))):
            ttk.Button(right, text=t, command=cmd, width=27).grid(
                row=1 + pos // 2, column=pos % 2, sticky="ew", padx=2, pady=1
            )
        ttk.Label(right, text="Desactivar conserva los puntos; Reset los borra.").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=2, pady=(2, 0)
        )
        ttk.Label(right, textvariable=self.v_cal_info, justify="left", wraplength=410).grid(
            row=5, column=0, columnspan=2, sticky="w", padx=2, pady=(2, 0)
        )

    def _tab_events(self):
        f = ttk.Frame(self.nb); self.nb.add(f, text="Eventos y estadísticas")
        ttk.Label(f, textvariable=self.v_stats, justify="left").pack(anchor="w", padx=6, pady=2)
        self.txt = tk.Text(f, height=7, wrap="none", state="disabled")
        self.txt.pack(fill="x", padx=6, pady=2)

    # --------------------------------------------------------------- settings
    def _load_store(self):
        name = "DEMO_SIMULADO_calibracion.json" if self.v_demo.get() else "calibracion.json"
        try:
            self.store = CalibrationStore(os.path.join(self.data_dir, name))
        except CalibrationError as e:
            messagebox.showerror("Calibración", f"{e}\nSe usará un archivo nuevo en memoria.")
            self.store = CalibrationStore(os.path.join(self.data_dir, "calibracion_recuperada.json"))
        if self.store.nominal_vfs != DEFAULT_VFS:       # plena escala nominal fija; se ignora lo persistido
            self.store.set_nominal_vfs(DEFAULT_VFS)
        for i in range(6):
            self.v_sensor_names[i].set(self.store.name(i + 1))
        self.banner.pack_forget()
        if self.v_demo.get():
            self.banner.pack(side="top", fill="x", before=self.winfo_children()[1])
            self.ax.set_title(DEMO_TEXT, color="#b00020", fontsize=10)
        else:
            self.ax.set_title("")
        self.refresh_cal()

    def _load_settings(self):
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                d = json.load(f)
            self.v_baud.set(str(d.get("baud", 921600)))
            self.v_n.set(int(d.get("n", 1)))
            self.v_rate.set(str(d.get("rate", 100)))
            self.v_pc_average.set(min(1000, max(1, int(d.get("pc_average_samples", 20)))))
            self.v_port.set(d.get("port", ""))
            enabled = d.get("enabled")
            if isinstance(enabled, list) and len(enabled) == 6:
                for i, value in enumerate(enabled): self.v_enabled[i].set(bool(value))
            self.v_experience.set(str(d.get("experience", "medicion")))
            for i, value in enumerate(d.get("names", [])[:6]): self.v_sensor_names[i].set(str(value))
            for i, value in enumerate(d.get("photos", [])[:6]):
                self.sensor_photos[i] = str(value)
                self.v_photo[i].set(os.path.basename(str(value)) or "Sin foto")
        except (OSError, ValueError, TypeError):
            pass

    def _save_settings(self):
        if getattr(self, "_suspend_settings", False):
            return
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump({"baud": self.v_baud.get(), "rate": self.v_rate.get(),
                           "pc_average_samples": self._pc_average(),
                           "port": self.v_port.get(), "experience": self.v_experience.get(),
                           "enabled": [v.get() for v in self.v_enabled],
                           "names": [v.get() for v in self.v_sensor_names], "photos": self.sensor_photos},
                          f, ensure_ascii=False, indent=1)
        except (OSError, tk.TclError):
            pass

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.cb_port["values"] = ports
        if ports and self.v_port.get() not in ports:
            self.v_port.set(ports[0])

    def open_data_dir(self):
        try:
            os.startfile(self.data_dir)  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showinfo("Datos", f"Carpeta: {self.data_dir}\n({e})")

    def _cleanup_busy(self):
        return bool((self.worker and self.worker.is_alive()) or
                    (self._loader and self._loader.is_alive()))

    def _update_cleanup_buttons(self):
        state = "disabled" if self._cleanup_busy() else "normal"
        for button in self.cleanup_buttons:
            button.configure(state=state)

    def _discard_record_state(self):
        self._load_gen += 1
        self._loader = self._load_result = self._history = self._drag = None
        self.worker = None
        self.plot.clear()
        self._mode = "idle"
        self._last_state = ""
        self._view = (0.0, WINDOW_S)
        self.ax.set_xlim(-WINDOW_S, 0)
        self.ax.set_xlabel(LIVE_XLABEL)
        for line in self.raw_lines + self.lines:
            line.set_data([], [])
            line.set_visible(False)
        self.v_status.set("Detenido")
        self.v_stats.set("")
        self.canvas.draw_idle()

    def _empty_store(self, preserve_names, save):
        name = "DEMO_SIMULADO_calibracion.json" if self.v_demo.get() else "calibracion.json"
        self.store = CalibrationStore(os.path.join(self.data_dir, name))
        for sid, sensor_name in enumerate(preserve_names, 1):
            self.store.set_name(sid, sensor_name)
            self.v_sensor_names[sid - 1].set(sensor_name)
        if save:
            self.store.save()
        self.v_cal_status.set("")
        self.refresh_cal()

    def _reset_all_ui(self):
        self._suspend_settings = True
        try:
            self._reset_all_ui_values()
        finally:
            self._suspend_settings = False

    def _reset_all_ui_values(self):
        self.v_demo.set(False)
        self.v_baud.set("921600")
        self.v_n.set(1)
        self.v_rate.set("100")
        self.v_pc_average.set(20)
        self.v_pc_average_draft.set("20")
        self.v_pc_average_error.set("")
        self.v_experience.set("medicion")
        self.v_port.set("")
        self.sensor_photos = ["" for _ in range(6)]
        for i in range(6):
            self.v_enabled[i].set(i == 0)
            self.v_sensor_names[i].set(f"S{i+1}")
            self.v_photo[i].set("Sin foto")
            self.v_sensor_live[i].set("ADC – · °C –")
        self._empty_store([f"S{i}" for i in range(1, 7)], save=False)
        self.refresh_ports()
        self._update_average_duration()
        self._update_plan_label()
        self.banner.pack_forget()
        self.ax.set_title("")
    def _cleanup(self, scope):
        if self._cleanup_busy():
            messagebox.showwarning("Limpieza", "Detenga la adquisición y espere a que termine la carga del historial.")
            return
        if scope == "all" and not messagebox.askyesno(
            "Eliminar todo",
            "Se eliminarán TODOS los datos de la aplicación dentro de la carpeta de datos: "
            "registros, calibraciones, ajustes y fotos. El programa no se elimina. ¿Continuar?",
        ):
            return
        names = [v.get() or f"S{i+1}" for i, v in enumerate(self.v_sensor_names)]
        try:
            removed = cleanup_data(self.data_dir, scope, project_root=os.path.dirname(os.path.dirname(__file__)))
        except (CleanupError, OSError) as e:
            messagebox.showerror("Limpieza", str(e))
            return
        if scope in ("records", "all"):
            self._discard_record_state()
        if scope == "calibrations":
            self._empty_store(names, save=True)
        elif scope == "all":
            self._reset_all_ui()
        self._update_cleanup_buttons()
        self.v_status.set(f"Limpieza completada ({len(removed)} elemento(s))")

    def _on_demo(self):
        if self.worker and self.worker.is_alive():
            self.v_demo.set(not self.v_demo.get())
            messagebox.showinfo("Demo", "Detenga la adquisición antes de cambiar el modo.")
            return
        self._load_store()

    def _read_cfg(self):
        try:
            return P.plan_config(self.selected_channels(), parse_float(self.v_rate.get()), 1, int(self.v_baud.get()))
        except (tk.TclError, ValueError) as e:
            raise P.ConfigError(str(e) if isinstance(e, P.ConfigError) else "Valores de configuración no numéricos.")

    def _on_cfg_change(self):
        self._update_plan_label()
        self._update_average_duration()

    def _pc_average(self):
        try:
            return min(1000, max(1, int(self.v_pc_average.get())))
        except (tk.TclError, ValueError):
            return 20

    def _update_average_duration(self):
        count = self._pc_average()
        try:
            rate = parse_float(self.v_rate.get())
            text = "sin promedio" if count == 1 else f"≈ {count / rate:.2f} s"
        except (ValueError, ZeroDivisionError):
            text = ""
        self.v_pc_average_duration.set(text)

    def _on_pc_average_change(self, *_):
        self.v_pc_average_draft.set(str(self._pc_average()))
        self.v_pc_average_error.set("")
        self._update_average_duration()
        self._save_settings()
        if self._mode == "frozen":
            self._render_frozen(preserve_xlim=True)
        elif self._mode == "history":
            self._render_history()

    def _apply_pc_average(self, _event=None):
        try:
            count = int(self.v_pc_average_draft.get().strip())
            if not 1 <= count <= 1000:
                raise ValueError
        except ValueError:
            self.v_pc_average_error.set("Inválido: use 1–1000; se conserva el valor anterior.")
            return "break"
        self.v_pc_average.set(count)
        return "break"

    def selected_channels(self):
        return tuple(i + 1 for i, v in enumerate(self.v_enabled) if v.get())

    def _save_sensor_name(self, i):
        name = self.v_sensor_names[i].get().strip() or f"S{i+1}"
        self.v_sensor_names[i].set(name)
        changed = self.store and self.store.name(i + 1) != name
        if self.store:
            self.store.set_name(i + 1, name)
            self.store.save()
        self._save_settings()
        if changed:
            self._channels_changed()

    def _metadata_changed(self):
        self._save_settings()
        if self.worker and self.worker.is_alive() and self.worker.settings.experience_label != self.v_experience.get():
            self._channels_changed()

    def attach_photo(self, i):
        p = filedialog.askopenfilename(title=f"Foto de colocación S{i+1}",
            filetypes=[("Imágenes PNG/JPEG", "*.png *.jpg *.jpeg")])
        if not p: return
        try:
            from .recorder import image_kind
            ext = image_kind(p)
            library = os.path.join(self.data_dir, "fotos_sensores")
            os.makedirs(library, exist_ok=True)
            saved = os.path.join(library, f"S{i+1}_{uuid.uuid4().hex}{ext}")
            shutil.copy2(p, saved)
        except (OSError, ValueError) as e:
            messagebox.showerror("Foto inválida", str(e)); return
        self.sensor_photos[i] = saved
        self.v_photo[i].set(os.path.basename(p))
        self._save_settings()
        self._channels_changed()

    def _channels_changed(self):
        self._save_settings()
        self._update_plan_label()
        if self.worker and self.worker.is_alive():
            self._restart_pending = bool(self.selected_channels())
            self.worker.request_stop()
            self.v_status.set("Reconfigurando: cerrando sesión actual…" if self._restart_pending else "Deteniendo: ningún canal habilitado…")
        elif self._mode == "history":
            self._render_history()
        elif self._mode == "frozen":
            self._render_frozen(preserve_xlim=True)

    def _update_plan_label(self):
        try:
            p = self._read_cfg()
            self.v_plan.set(f"Intervalo {p.interval_us} µs · {p.bytes_per_s:.0f} B/s ({p.utilisation*100:.1f} % del enlace)"
                            + ("  ⚠ " + "; ".join(p.warnings) if p.warnings else ""))
        except P.ConfigError as e:
            self.v_plan.set(f"⛔ {e}")

    # ----------------------------------------------------------- acquisition
    def start_acquisition(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            self._read_cfg()
        except P.ConfigError as e:
            messagebox.showerror("Configuración inválida", str(e))
            return
        for i in range(6): self._save_sensor_name(i)
        demo = self.v_demo.get()
        port, baud = self.v_port.get().strip(), int(self.v_baud.get())
        if not demo and not port:
            messagebox.showerror("Puerto", "Seleccione un puerto serie.")
            return
        channels = self.selected_channels()
        st = SessionSettings(port="DEMO" if demo else port, baud=baud, n_sensors=len(channels), channels=channels,
                             rate_hz=parse_float(self.v_rate.get()), oversample=1,
                             experience_label=self.v_experience.get(),
                             photo_sources={i + 1: p for i, p in enumerate(self.sensor_photos) if p and i + 1 in channels})
        factory = (lambda: FakeSerial()) if demo else (lambda: open_serial(port, baud))
        self._reset_live_view()
        self.worker = SerialWorker(factory, st, self.store, os.path.join(self.data_dir, "sesiones"), demo=demo, plot=self.plot)
        self._save_settings()
        self.worker.start()
        self._set_running(True)

    def stop_acquisition(self):
        self._restart_pending = False
        if self.worker:
            if self._mode == "live":
                self._freeze()
            self.worker.request_stop()
            self.v_status.set("Deteniendo…")

    def _set_running(self, running):
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.chk_demo.configure(state="disabled" if running else "normal")
        self._update_cleanup_buttons()

    # --------------------------------------------------------------- periodic
    def _tick(self):
        try:
            self.update_ui()
        finally:
            self._tick_timer = self.after(200, self._tick)

    def update_ui(self):
        self._update_cleanup_buttons()
        w = self.worker
        active = set(w.channels) if w else set()
        if w:
            while True:
                try:
                    lvl, txt = w.events.get_nowait()
                except Exception:
                    break
                self._log(f"[{lvl}] {txt}")
            s = w.stats()
            self.v_status.set(f"{'SIMULADO · ' if w.demo else ''}{s['state'].capitalize()}")
            self.v_stats.set(
                f"Tramas OK: {s['frames_ok']}   Perdidas: {s['lost']}   Mal formadas: {s['malformed']}   Duplicadas: {s['duplicates']}   "
                f"Reinicios: {s['reboots']}   Desbordes µs: {s['rollovers']}   Atrasos ESP32: {s['late_samples']}   "
                f"Filas CSV: {s['rows_written']}\nArchivo: {w.csv_path or '–'}")
            if not w.is_alive() and self._last_state != "fin":
                self._last_state = "fin"
                self._set_running(False)
                if w.state == "error":
                    messagebox.showwarning("Adquisición terminada con error", w.end_reason)
                if self._restart_pending:
                    self._restart_pending = False
                    self.after(50, self.start_acquisition)
                else:
                    if self._mode == "live":
                        self._freeze()
                    if self._mode == "frozen" and w.csv_path:
                        self._begin_history_load(w.csv_path)
            elif w.is_alive():
                self._last_state = "run"
        live = self._mode in ("idle", "live")
        average = self._pc_average()
        now = time.monotonic() if live else None
        for i in range(6):
            line = self.lines[i]
            raw_line = self.raw_lines[i]
            if i + 1 in active:
                if live:
                    t, y = self.plot.window(i, WINDOW_S, 1200, now=now, smoothing_count=average)
                    line.set_data(t, y)
                    line.set_label(self.store.name(i + 1))
                    line.set_visible(True)
                    if average > 1 and self.v_enabled[i].get():
                        raw_t, raw_y = self.plot.window(i, WINDOW_S, 1200, now=now, smoothing_count=1)
                        raw_line.set_data(raw_t, raw_y)
                        raw_line.set_visible(True)
                    else:
                        raw_line.set_data([], [])
                        raw_line.set_visible(False)
                lat = self.plot.latest(i)
                c = self.store.coeffs(i + 1)
                self.tv_read.item(str(i), values=(f"{i+1} – {self.store.name(i+1)}", P.GPIOS[i],
                                                  "–" if not lat else f"{lat[1]:.1f}", "–" if not lat else f"{lat[2]:.2f}",
                                                  c.mode, c.revision, c.quality))
            else:
                if live:
                    line.set_data([], [])
                    line.set_visible(False)
                    raw_line.set_data([], [])
                    raw_line.set_visible(False)
                self.tv_read.item(str(i), values=(f"{i+1} – {self.store.name(i+1)}", P.GPIOS[i], "–", "–", "Inhabilitado", "", ""))
            lat = self.plot.latest(i) if i + 1 in active else None
            self.v_sensor_live[i].set("ADC – · °C –" if not lat else f"ADC {lat[1]:.1f} · {lat[2]:.2f} °C")
        if live and active:
            self._fit_live_axes(active)
        if self._load_result is not None:
            gen, hist, err = self._load_result
            self._load_result = None
            if gen == self._load_gen and self._mode == "frozen":
                if hist is not None:
                    self._history = hist
                    self._mode = "history"
                    self.ax.set_xlabel(STOP_XLABEL)
                    self._view = self._clamp_view(hist.duration - WINDOW_S, hist.duration)
                    self._log(f"[INFO] Historial completo cargado: {hist.duration:.1f} s")
                    self._render_history()
                else:
                    self._log(f"[WARN] No se pudo cargar el historial completo: {err}")
        if live:
            self.canvas.draw_idle()
        self._update_live_adc()

    # ------------------------------------------------- vista: viva / congelada / historial
    def _fit_live_axes(self, active):
        visible = [self.lines[i - 1] for i in sorted(active)]
        backgrounds = [self.raw_lines[i - 1] for i in sorted(active) if self.raw_lines[i - 1].get_visible()]
        ys = [v for ln in backgrounds + visible for v in ln.get_ydata()]
        if ys:
            lo, hi = min(ys), max(ys)
            pad = max(0.5, (hi - lo) * 0.1)
            self.ax.set_ylim(lo - pad, hi + pad)
        self.ax.legend(handles=visible, loc="upper left", fontsize=8, ncol=len(visible))

    def _reset_live_view(self):
        """Nueva sesión: descarta historial/congelado y vuelve a la ventana deslizante de 10 s."""
        self.plot.clear()
        self._history = None
        self._load_gen += 1
        self._loader = self._load_result = self._drag = None
        self._mode = "live"
        self.ax.set_xlim(-WINDOW_S, 0)
        self.ax.set_xlabel(LIVE_XLABEL)
        for line in self.raw_lines + self.lines:
            line.set_data([], [])
            line.set_visible(False)

    def _freeze(self):
        """Congela al instante la gráfica: instantánea de los últimos 10 s; los ticks ya no la modifican."""
        self._frozen_now = time.monotonic()
        self._render_frozen()
        self._mode = "frozen"
        self.canvas.draw_idle()

    def _render_frozen(self, preserve_xlim=False):
        w = self.worker
        active = set(w.channels) if w else set()
        now = getattr(self, "_frozen_now", time.monotonic())
        old_xlim = self.ax.get_xlim()
        average = self._pc_average()
        for i in range(6):
            line = self.lines[i]
            raw_line = self.raw_lines[i]
            if i + 1 in active:
                t, y = self.plot.window(i, WINDOW_S, 1200, now=now, smoothing_count=average)
                line.set_data(t, y)
                line.set_label(self.store.name(i + 1))
                line.set_visible(True)
                if average > 1 and self.v_enabled[i].get():
                    raw_t, raw_y = self.plot.window(i, WINDOW_S, 1200, now=now, smoothing_count=1)
                    raw_line.set_data(raw_t, raw_y)
                    raw_line.set_visible(True)
                else:
                    raw_line.set_data([], [])
                    raw_line.set_visible(False)
            else:
                line.set_data([], [])
                line.set_visible(False)
                raw_line.set_data([], [])
                raw_line.set_visible(False)
        self.ax.set_xlim(old_xlim if preserve_xlim else (-WINDOW_S, 0))
        if active:
            self._fit_live_axes(active)
        self.canvas.draw_idle()

    def _begin_history_load(self, path):
        if self._loader is not None:
            return
        gen = self._load_gen
        self._log("[INFO] Cargando historial completo desde el CSV…")

        def run():
            try:
                self._load_result = (gen, load_history(path), None)
            except Exception as e:          # el hilo nunca debe morir en silencio
                self._load_result = (gen, None, str(e))
        self._loader = threading.Thread(target=run, daemon=True, name="history-loader")
        self._loader.start()

    def _clamp_view(self, v0, v1):
        dur = self._history.duration if self._history else 0.0
        span = min(max(v1 - v0, MIN_SPAN_S), max(dur, MIN_SPAN_S))
        v0 = min(max(v0, 0.0), max(dur - span, 0.0))
        return v0, v0 + span

    def _render_history(self):
        h = self._history
        if h is None:
            return
        v0, v1 = self._view
        average = self._pc_average()
        chans = h.channels
        for i in range(6):
            line = self.lines[i]
            raw_line = self.raw_lines[i]
            if i in chans:
                t, y = h.view(i, v0, v1, smoothing_count=average)
                line.set_data(t, y)
                line.set_label(self.store.name(i + 1))
                line.set_visible(True)
                if average > 1 and self.v_enabled[i].get():
                    raw_t, raw_y = h.view(i, v0, v1, smoothing_count=1)
                    raw_line.set_data(raw_t, raw_y)
                    raw_line.set_visible(True)
                else:
                    raw_line.set_data([], [])
                    raw_line.set_visible(False)
            else:
                line.set_data([], [])
                line.set_visible(False)
                raw_line.set_data([], [])
                raw_line.set_visible(False)
        self.ax.set_xlim(v0, v1)
        lim = h.ylim(chans, v0, v1, smoothing_count=average)
        raw_chans = [i for i in chans if average > 1 and self.v_enabled[i].get()]
        raw_lim = h.ylim(raw_chans, v0, v1, smoothing_count=1)
        if raw_lim:
            lim = raw_lim if lim is None else (min(lim[0], raw_lim[0]), max(lim[1], raw_lim[1]))
        if lim:
            pad = max(0.5, (lim[1] - lim[0]) * 0.1)
            self.ax.set_ylim(lim[0] - pad, lim[1] + pad)
        if chans:
            self.ax.legend(handles=[self.lines[i] for i in chans], loc="upper left", fontsize=8, ncol=len(chans))
        self.canvas.draw_idle()

    def pan_view(self, dt):
        if self._mode == "history":
            self._view = self._clamp_view(self._view[0] + dt, self._view[1] + dt)
            self._render_history()

    def zoom_view(self, factor, center):
        if self._mode == "history":
            v0, v1 = self._view
            self._view = self._clamp_view(center - (center - v0) * factor, center + (v1 - center) * factor)
            self._render_history()

    def view_all(self):
        if self._mode == "history":
            self._view = self._clamp_view(0.0, self._history.duration)
            self._render_history()

    def _on_press(self, ev):
        if self._mode == "history" and ev.button == 1 and ev.inaxes is self.ax:
            self._drag = (ev.x, self._view)

    def _on_motion(self, ev):
        if self._drag is None or self._mode != "history" or ev.x is None:
            return
        x0, (v0, v1) = self._drag
        width = max(self.ax.bbox.width, 1.0)
        dt = -(ev.x - x0) * (v1 - v0) / width
        self._view = self._clamp_view(v0 + dt, v1 + dt)
        self._render_history()

    def _on_release(self, _ev):
        self._drag = None

    def _on_scroll(self, ev):
        if self._mode == "history" and ev.inaxes is self.ax and ev.xdata is not None:
            self.zoom_view(0.8 if ev.button == "up" else 1.25, ev.xdata)

    def _log(self, text):
        self.txt.configure(state="normal")
        self.txt.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
        if int(self.txt.index("end-1c").split(".")[0]) > 300:
            self.txt.delete("1.0", "50.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    # ------------------------------------------------------------ calibration
    def _sid(self):
        return int(self.v_cal_sensor.get().removeprefix("S"))

    def _update_live_adc(self):
        i = self._sid() - 1
        a = self.plot.recent_adc(i, 1.0)
        self.v_cal_live.set(f"ADC actual (prom. 1 s): {sum(a)/len(a):.1f}  [{len(a)} muestras]" if a else "ADC actual: – (sin datos)")

    def refresh_cal(self):
        sid = self._sid()
        self.v_cal_name.set(self.store.name(sid))
        self.tv_pts.delete(*self.tv_pts.get_children())
        for k, p in enumerate(self.store.points(sid)):
            self.tv_pts.insert("", "end", iid=str(k), values=(p.timestamp, f"{p.adc:g}", f"{p.ref_c:g}", p.source))
        c = self.store.coeffs(sid)
        txt = f"Activa: {c.mode}, rev. {c.revision}, pendiente {c.slope:.5g} °C/cuenta, offset {c.intercept:.4g} °C"
        if c.mode == "calibrado":
            txt += f"\nCalidad: {c.quality} · R²={c.r2:.5f} · {c.n_points} puntos"
        try:
            f = self.store.fit(sid)
            txt += f"\nAjuste actual de los puntos: m={f.slope:.5g}, b={f.intercept:.4g}, RMSE={f.rmse:.3f} °C, máx.|res|={f.max_abs_resid:.3f} °C"
            if f.warnings:
                txt += "\n⚠ " + " ".join(f.warnings)
        except CalibrationError as e:
            txt += f"\nAjuste: no disponible ({e})"
        self.v_cal_info.set(txt)

    def _ref(self):
        try:
            return parse_float(self.v_cal_ref.get())
        except ValueError:
            messagebox.showerror("Referencia", "Escriba la temperatura de referencia (°C) como número.")
            return None

    def cal_rename(self):
        self.v_sensor_names[self._sid() - 1].set(self.v_cal_name.get())
        self._save_sensor_name(self._sid() - 1)
        self.refresh_cal()

    def cal_capture(self):
        ref = self._ref()
        if ref is None:
            return
        sid = self._sid()
        worker = self.worker
        if not self.v_enabled[sid - 1].get() or not worker or not worker.is_alive() or sid not in worker.channels:
            messagebox.showerror("Captura", "El sensor debe estar habilitado y la adquisición debe estar activa.")
            return
        a = self.plot.recent_adc(sid - 1, 1.0)
        if len(a) < 3:
            messagebox.showerror("Captura", "No hay lecturas recientes de ese sensor. Inicie la adquisición (o use 'Manual').")
            return
        mean = statistics.fmean(a)
        sd = statistics.pstdev(a)
        if sd > 5 and not messagebox.askyesno("Lectura inestable", f"Desviación {sd:.1f} cuentas en el último segundo. ¿Capturar igualmente?"):
            return
        if self._add(sid, mean, ref, "captura", f"prom {len(a)} muestras, desv {sd:.2f}"):
            self.v_cal_status.set(f"S{sid}: punto guardado ({mean:.1f} ADC, {ref:g} °C)")

    def cal_add_manual(self):
        ref = self._ref()
        if ref is None:
            return
        try:
            adc = parse_float(self.v_cal_adc.get())
        except ValueError:
            messagebox.showerror("ADC", "ADC manual no numérico.")
            return
        self._add(self._sid(), adc, ref, "manual", "")

    def _add(self, sid, adc, ref, src, note):
        try:
            self.store.add_point(sid, adc, ref, src, note=note)
            self.store.save()
        except CalibrationError as e:
            messagebox.showerror("Punto inválido", str(e)); return False
        self.refresh_cal()
        return True

    def cal_remove(self):
        sel = [int(i) for i in self.tv_pts.selection()]
        if sel:
            sid = self._sid()
            self.store.remove_points(sid, sel)
            self.refresh_cal()
            self.v_cal_status.set(f"S{sid}: punto(s) eliminado(s); recalcular y aplicar ajuste")

    def cal_reset(self):
        sid = self._sid()
        self.store.reset_sensor(sid)
        self.refresh_cal()
        self.v_cal_status.set(f"S{sid}: puntos borrados; conversión nominal")

    def cal_import(self):
        p = filedialog.askopenfilename(title="Importar puntos", filetypes=[("CSV", "*.csv")])
        if not p:
            return
        try:
            added, errs = self.store.import_points_csv(p)
            self.store.save()
        except (CalibrationError, OSError) as e:
            messagebox.showerror("Importar", str(e)); return
        messagebox.showinfo("Importar", f"{added} puntos importados." + (f"\n{len(errs)} filas omitidas:\n" + "\n".join(errs[:8]) if errs else ""))
        self.refresh_cal()

    def cal_export(self):
        p = filedialog.asksaveasfilename(title="Exportar puntos", defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                         initialfile="puntos_calibracion.csv")
        if p:
            self.store.export_points_csv(p)

    def cal_apply(self):
        sid = self._sid()
        try:
            f = self.store.fit(sid)
        except CalibrationError as e:
            messagebox.showerror("Calibración inválida", str(e)); return
        msg = f"Sensor {sid}: T = {f.slope:.6g}·ADC + {f.intercept:.4g}\nR²={f.r2:.5f}, RMSE={f.rmse:.3f} °C, calidad: {f.quality}"
        if f.warnings:
            msg += "\n\n⚠ " + "\n⚠ ".join(f.warnings)
        if self.worker and self.worker.is_alive():
            msg += "\n\nHay una sesión en curso: el cambio de revisión quedará registrado en su JSON y CSV."
        if messagebox.askokcancel("Aplicar calibración", msg + "\n\n¿Aplicar como calibración activa?"):
            self.store.apply_fit(sid); self.refresh_cal()

    def cal_revert(self):
        if messagebox.askokcancel(
            "Desactivar calibración",
            "¿Desactivar la calibración y usar la conversión nominal aproximada?\n"
            "Los puntos guardados se conservarán y se registrará una revisión nueva.",
        ):
            self.store.revert_nominal(self._sid()); self.refresh_cal()

    # ------------------------------------------------------------------ recalc
    def recalc_dialog(self):
        src = filedialog.askopenfilename(title="CSV crudo a recalcular", filetypes=[("CSV", "*.csv")],
                                         initialdir=os.path.join(self.data_dir, "sesiones"))
        if not src:
            return
        cal = filedialog.askopenfilename(title="Archivo de calibración (JSON)", filetypes=[("JSON", "*.json")],
                                         initialdir=self.data_dir, initialfile=os.path.basename(self.store.path))
        if not cal:
            return
        base, _ = os.path.splitext(src)
        out = filedialog.asksaveasfilename(title="Guardar CSV recalculado (nuevo archivo)", defaultextension=".csv",
                                           initialfile=os.path.basename(base) + "_recalc.csv", initialdir=os.path.dirname(src))
        if not out:
            return
        try:
            r = recalc_csv(src, out, cal, source="refit" if messagebox.askyesno(
                "Origen", "¿Reajustar con los puntos actuales del archivo de calibración?\n(No = usar la calibración activa guardada)") else "active")
            messagebox.showinfo("Recalculado", f"{r['rows']} filas ({r['rows_invalid']} inválidas).\nOriginal intacto.\n{out}")
        except (RecalcError, OSError) as e:
            messagebox.showerror("Recalcular", str(e))

    # ------------------------------------------------------------------- close
    def on_close(self):
        if self.worker and self.worker.is_alive():
            self.worker.request_stop()
            self.worker.join(3)
        self._save_settings()
        self.destroy()


def main(data_dir=None, demo=False):
    App(data_dir, demo).mainloop()
