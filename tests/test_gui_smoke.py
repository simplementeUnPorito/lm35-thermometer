import tempfile, unittest, tkinter
import csv
import os
import queue
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch


class TestGuiSmoke(unittest.TestCase):
    def test_cleanup_buttons_scope_state_and_resets(self):
        from lm35.gui import App
        with tempfile.TemporaryDirectory() as d:
            app = App(d, demo=True)
            try:
                app.update()
                self.assertEqual([b.cget("text") for b in app.cleanup_buttons],
                                 ["Eliminar registros", "Eliminar calibraciones", "Eliminar todo"])
                self.assertFalse(hasattr(app, "btn_fit"))
                os.makedirs(os.path.join(d, "sesiones"))
                with open(os.path.join(d, "sesiones", "x.csv"), "w") as f: f.write("x")
                with open(os.path.join(d, "ajustes.json"), "w") as f: f.write("{}")
                app._cleanup("records")
                self.assertFalse(os.path.exists(os.path.join(d, "sesiones")))
                self.assertTrue(os.path.exists(os.path.join(d, "ajustes.json")))

                app.v_sensor_names[0].set("Bobinado")
                app.store.add_point(1, 100, 20)
                app.store.save()
                app._cleanup("calibrations")
                self.assertEqual(app.store.name(1), "Bobinado")
                self.assertEqual(app.store.points(1), [])
                self.assertEqual(app.store.coeffs(1).revision, 0)
                self.assertTrue(os.path.exists(app.store.path))

                app.worker = Mock()
                app.worker.is_alive.return_value = True
                app._update_cleanup_buttons()
                self.assertTrue(all(str(b.cget("state")) == "disabled" for b in app.cleanup_buttons))
                with patch("lm35.gui.messagebox.showwarning") as warning:
                    app._cleanup("all")
                warning.assert_called_once()
                self.assertTrue(os.path.exists(app.store.path))

                app.worker.is_alive.return_value = False
                app.v_rate.set("50")
                app.v_pc_average.set(3)
                with patch("lm35.gui.messagebox.askyesno", return_value=True):
                    app._cleanup("all")
                self.assertEqual(os.listdir(d), [])
                self.assertFalse(app.v_demo.get())
                self.assertEqual(app.v_rate.get(), "100")
                self.assertEqual(app.v_pc_average.get(), 20)
                self.assertEqual(app.v_sensor_names[0].get(), "S1")
            finally:
                app.destroy()

    def test_graph_average_entry_keeps_invalid_draft_and_applies_enter_or_button(self):
        from lm35.gui import App
        with tempfile.TemporaryDirectory() as d:
            app = App(d, demo=True)
            try:
                app.update()
                self.assertEqual(app.ent_pc_average.winfo_class(), "TEntry")
                self.assertEqual(app.v_pc_average.get(), 20)

                with patch.object(app, "_save_settings") as save:
                    app.ent_pc_average.focus_force()
                    for invalid in ("", "abc", "0", "1001"):
                        app.v_pc_average_draft.set(invalid)
                        app.ent_pc_average.event_generate("<Return>")
                        app.update()
                        self.assertEqual(app.v_pc_average.get(), 20)
                        self.assertTrue(app.v_pc_average_error.get())
                    save.assert_not_called()

                    app.v_pc_average_draft.set("5")
                    app.ent_pc_average.event_generate("<Return>")
                    app.update()
                    self.assertEqual(app.v_pc_average.get(), 5)
                    self.assertEqual(app.v_pc_average_error.get(), "")
                    save.assert_called_once()

                app.v_pc_average_draft.set("7")
                app.btn_apply_average.invoke()
                self.assertEqual(app.v_pc_average.get(), 7)
                app.v_pc_average.set(9)
                self.assertEqual(app.v_pc_average_draft.get(), "9")
            finally:
                app.destroy()

    def test_numeric_readings_are_raw_while_graph_is_smoothed(self):
        from lm35.gui import App
        with tempfile.TemporaryDirectory() as d:
            app = App(d, demo=True)
            try:
                app.v_pc_average.set(2)
                app.worker = SimpleNamespace(
                    channels=(1,), demo=True, events=queue.Queue(), csv_path="",
                    state="running", is_alive=lambda: True,
                    stats=lambda: {**{k: 0 for k in "frames_ok lost malformed duplicates reboots rollovers late_samples rows_written".split()},
                                   "state": "adquiriendo"},
                )
                now = time.monotonic()
                app.plot.append(0, now - 0.01, 10.0, 10.0)
                app.plot.append(0, now, 100.0, 100.0)

                app.update_ui()

                self.assertEqual(float(app.lines[0].get_ydata()[-1]), 55.0)
                self.assertEqual(float(app.raw_lines[0].get_ydata()[-1]), 100.0)
                self.assertTrue(app.raw_lines[0].get_visible())
                self.assertEqual(app.raw_lines[0].get_color(), "#c8c8c8")
                self.assertAlmostEqual(app.raw_lines[0].get_alpha(), 0.5)
                self.assertAlmostEqual(app.raw_lines[0].get_linewidth(), 0.7)
                self.assertEqual(app.raw_lines[0].get_label(), "_nolegend_")
                self.assertLess(app.raw_lines[0].get_zorder(), app.lines[0].get_zorder())
                values = app.tv_read.item("0", "values")
                self.assertEqual(values[2:4], ("100.0", "100.00"))
                self.assertEqual(app.v_sensor_live[0].get(), "ADC 100.0 · 100.00 °C")

                app.v_pc_average.set(1)
                app.update_ui()
                self.assertFalse(app.raw_lines[0].get_visible())
                app.v_pc_average.set(2)
                app.v_enabled[0].set(False)
                app.update_ui()
                self.assertFalse(app.raw_lines[0].get_visible())
            finally:
                app.destroy()

    def test_calibration_tab_appends_selected_sensor_and_contains_all_controls(self):
        from lm35.gui import App
        with tempfile.TemporaryDirectory() as d:
            app = App(d, demo=True)
            try:
                app.geometry("1340x700+0+0")
                app.update()
                app.v_enabled[1].set(True)
                app.worker = Mock(channels=(1, 2))
                app.worker.is_alive.return_value = True
                app.v_cal_sensor.set("S2")
                app.cb_cal_sensor.event_generate("<<ComboboxSelected>>")
                app.v_cal_ref.set("35.0")
                now = time.monotonic()
                for adc in (300.0, 306.0, 312.0):
                    app.plot.append(1, now, adc, 0.0)

                app.cal_capture()

                points = app.store.points(2)
                self.assertEqual(len(points), 1)
                self.assertAlmostEqual(points[0].adc, 306.0)
                self.assertEqual(points[0].ref_c, 35.0)
                self.assertEqual(points[0].source, "captura")
                self.assertTrue(os.path.exists(app.store.path))
                from lm35.calibration import CalibrationStore
                self.assertAlmostEqual(CalibrationStore(app.store.path).points(2)[0].adc, 306.0)
                self.assertEqual(app.store.points(1), [])
                self.assertTrue(all(app.store.points(sid) == [] for sid in range(3, 7)))
                self.assertEqual(app.v_cal_name.get(), app.store.name(2))
                self.assertIn("S2: punto guardado", app.v_cal_status.get())
                self.assertEqual(len(app.tv_pts.get_children()), 1)
                self.assertEqual(str(app.cb_cal_sensor.cget("state")), "readonly")
                self.assertEqual(app.btn_cal_reset.cget("text"), "Reset")
                self.assertEqual(app.btn_cal_capture.cget("text"), "Append medición de calibración")
                self.assertFalse(hasattr(app, "btn_cal_show"))
                self.assertFalse(hasattr(app, "cal_show_points"))
                self.assertFalse(hasattr(app, "quick_cal_bar"))
                self.assertFalse(hasattr(app, "cb_cal_quick"))
                calibration_buttons = {
                    "Renombrar", "Append medición de calibración", "Reset", "Agregar punto manual",
                    "Eliminar punto(s)", "Importar puntos CSV…", "Exportar puntos CSV…",
                    "Calcular y aplicar ajuste", "Desactivar calibración", "Recalcular CSV crudo…",
                }
                descendants = []
                pending = list(app.cal_tab.winfo_children())
                while pending:
                    widget = pending.pop()
                    descendants.append(widget)
                    pending.extend(widget.winfo_children())
                tab_buttons = {w.cget("text") for w in descendants if w.winfo_class() == "TButton"}
                self.assertTrue(calibration_buttons <= tab_buttons)
                self.assertIn(app.cb_cal_sensor, descendants)
                all_widgets = []
                pending = list(app.winfo_children())
                while pending:
                    widget = pending.pop()
                    all_widgets.append(widget)
                    pending.extend(widget.winfo_children())
                matching_buttons = [w for w in all_widgets if w.winfo_class() == "TButton"
                                    and w.cget("text") in calibration_buttons]
                self.assertEqual(len(matching_buttons), len(calibration_buttons))
                self.assertTrue(all(w in descendants for w in matching_buttons))
                calibration_selectors = [w for w in all_widgets if w.winfo_class() == "TCombobox"
                                         and str(w.cget("textvariable")) == str(app.v_cal_sensor)]
                self.assertEqual(calibration_selectors, [app.cb_cal_sensor])
                app.v_cal_sensor.set("S6")
                app.refresh_cal()
                app.nb.select(app.cal_tab)
                app.update_idletasks()
                self.assertEqual(app.nb.select(), str(app.cal_tab))
                self.assertTrue(app.tv_pts.winfo_ismapped())
                self.assertEqual(app.v_cal_name.get(), app.store.name(6))
                app.store.add_point(6, 100, 10)
                app.store.add_point(6, 300, 30)
                app.store.add_point(6, 400, 40)
                app.refresh_cal()
                app.tv_pts.selection_set("1")
                app.cal_remove()
                self.assertEqual([(p.adc, p.ref_c) for p in app.store.points(6)], [(100, 10), (400, 40)])
                app.store.apply_fit(6)
                app.btn_cal_reset.invoke()
                self.assertEqual(app.store.points(6), [])
                self.assertEqual(app.store.coeffs(6).mode, "nominal")
                self.assertEqual(len(app.store.points(2)), 1)
                self.assertEqual(app.v_cal_status.get(), "S6: puntos borrados; conversión nominal")
                app.update_idletasks()
                self.assertEqual((app.winfo_width(), app.winfo_height()), (1340, 700))
                self.assertLessEqual(app.cal_tab.winfo_reqwidth(), app.winfo_width())
                self.assertLessEqual(app.nb.winfo_y() + app.nb.winfo_height(), app.winfo_height())
                self.assertGreater(app.canvas.get_tk_widget().winfo_height(), 100)

                app.v_enabled[1].set(False)
                with patch("lm35.gui.messagebox.showerror") as rejected:
                    app.cal_capture()
                rejected.assert_called_once()
                self.assertEqual(len(app.store.points(2)), 1)

                app.v_enabled[1].set(True)
                app.worker.is_alive.return_value = False
                with patch("lm35.gui.messagebox.showerror") as rejected:
                    app.cal_capture()
                rejected.assert_called_once()
                self.assertEqual(len(app.store.points(2)), 1)
                app.update_idletasks()
            finally:
                app.destroy()

    def test_checkboxes_restart_sparse_channels_and_disable_all(self):
        from lm35.gui import App
        with tempfile.TemporaryDirectory() as d:
            app = App(d, demo=True)
            def pump(seconds=0.5):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    app.update()
                    time.sleep(0.02)
            try:
                for i, value in enumerate(app.v_enabled): value.set(i in (1, 5))
                app.v_experience.set("prueba")
                app.v_sensor_names[1].set("bobinado")
                app.start_acquisition()
                pump()
                first = app.worker
                app.v_enabled[1].set(False)
                app._channels_changed()
                pump(0.8)
                self.assertIsNot(first, app.worker)
                self.assertEqual(app.worker.channels, (6,))
                with open(first.csv_path, newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual({r["sensor_id"] for r in rows}, {"2", "6"})
                self.assertEqual({r["sensor_name"] for r in rows if r["sensor_id"] == "2"}, {"bobinado"})
                app.v_enabled[5].set(False)
                app._channels_changed()
                pump(0.5)
                self.assertFalse(app.worker.is_alive())
                self.assertEqual(app.tv_read.item("1", "values")[2], "–")
                image = os.path.join(d, "source.png")
                with open(image, "wb") as f: f.write(b"\x89PNG\r\n\x1a\nfixture")
                with patch("lm35.gui.filedialog.askopenfilename", return_value=image): app.attach_photo(1)
                saved = app.sensor_photos[1]
                self.assertNotEqual(image, saved)
                os.remove(image)
                self.assertTrue(os.path.exists(saved))
                # All six channel rows must fit inside the application.
                app.update_idletasks()
                self.assertGreater(app.canvas.get_tk_widget().winfo_height(), 100)
            finally:
                if app.worker:
                    app.worker.request_stop()
                    app.worker.join(5)
                app.destroy()

    def test_demo_updates_plot(self):
        try:
            tkinter.Tk().destroy()
        except tkinter.TclError:
            self.skipTest("sin pantalla")
        from lm35.__main__ import main
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(["--data-dir", d, "smoke", "--seconds", "1.5"]), 0)


if __name__ == "__main__":
    unittest.main()
