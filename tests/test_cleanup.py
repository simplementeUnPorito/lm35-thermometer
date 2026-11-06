import os
import tempfile
import subprocess
import unittest
from pathlib import Path

from lm35.cleanup import CleanupError, cleanup_data


def put(path: Path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestCleanup(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "junctions son de Windows")
    def test_windows_junction_is_rejected_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "datos"
            external = base / "outside"
            put(external / "keep.txt", "safe")
            put(root / "calibracion.json", "safe")
            link = root / "sesiones"
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaises(CleanupError):
                    cleanup_data(root, "all")
                self.assertEqual((external / "keep.txt").read_text(), "safe")
                self.assertTrue((root / "calibracion.json").exists())
            finally:
                os.rmdir(link)  # Removes only the junction, leaving its target intact.

    def test_records_remove_only_known_output_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "datos"
            for name in ("sesiones", "demo_verificacion", "verificacion", "verificacion_gui",
                         "validacion_hardware_sin_sensores"):
                put(root / name / "result.csv")
            put(root / "carpeta_del_usuario" / "nota.txt")
            put(root / "archivo_del_usuario.txt")
            put(root / "calibracion.json")

            removed = cleanup_data(root, "records")

            self.assertEqual(set(removed), {"sesiones", "demo_verificacion", "verificacion",
                                            "verificacion_gui", "validacion_hardware_sin_sensores"})
            self.assertTrue((root / "carpeta_del_usuario" / "nota.txt").exists())
            self.assertTrue((root / "archivo_del_usuario.txt").exists())
            self.assertTrue((root / "calibracion.json").exists())

    def test_calibrations_remove_exact_known_files_and_temps_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "datos"
            known = ("calibracion.json", "calibracion_history.jsonl",
                     "DEMO_SIMULADO_calibracion.json", "DEMO_SIMULADO_calibracion_history.jsonl",
                     "calibracion_recuperada.json", "calibracion_recuperada_history.jsonl")
            for name in known:
                put(root / name)
                put(root / (name + ".tmp"))
            put(root / "ajustes.json")
            put(root / "fotos_sensores" / "S1.png")
            put(root / "mi_calibracion.json")

            cleanup_data(root, "calibrations")

            self.assertTrue(all(not (root / name).exists() for name in known))
            self.assertTrue(all(not (root / (name + ".tmp")).exists() for name in known))
            self.assertTrue((root / "ajustes.json").exists())
            self.assertTrue((root / "fotos_sensores" / "S1.png").exists())
            self.assertTrue((root / "mi_calibracion.json").exists())

    def test_all_removes_contents_but_not_data_root_or_external_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "datos"
            put(root / "sesiones" / "a.csv")
            put(root / "ajustes.json")
            put(root / "fotos_sensores" / "S1.png")
            put(root / "otro_archivo_de_app.bin")
            external = base / "outside.txt"
            put(external, "safe")

            cleanup_data(root, "all")

            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(external.read_text(encoding="utf-8"), "safe")

    def test_rejects_dangerous_roots(self):
        with self.assertRaises(CleanupError):
            cleanup_data(Path.cwd(), "all", project_root=Path.cwd())
        with self.assertRaises(CleanupError):
            cleanup_data(Path.home(), "all")

    def test_rejects_symlink_without_touching_external_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "datos"
            external = base / "outside"
            put(external / "keep.txt", "safe")
            root.mkdir()
            try:
                os.symlink(external, root / "sesiones", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("el entorno no permite crear symlinks")

            with self.assertRaises(CleanupError):
                cleanup_data(root, "records")
            self.assertEqual((external / "keep.txt").read_text(encoding="utf-8"), "safe")


if __name__ == "__main__":
    unittest.main()
