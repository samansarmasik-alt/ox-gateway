# -*- coding: utf-8 -*-
"""doctor.py / bench.py CLI duman testleri.

Dokumanlar: ikisi de kutuphane DEGIL, komut satiridir; yalnizca uc sey
sinanir: temiz import, belgelenen bayraklar, olu gateway'de izsiz ve
sifirdisi cikis. Upstream'e HICBIR istek atilmaz.

Guvenlik: doctor yapilandirmasi gecici dizine yonlendirilir; gercek
config.json, ~/.config/opencode/opencode.json, ~/.claude/settings.json ve
logs\\ dosyalari HICBIR OKUNMAZ. Anahtar degeri ne basilir ne yazilir.
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bench
import doctor
import supervisor


def free_port() -> int:
    """Kapali bir loopback portu: baglanilamaz, upstream'e cikilmaz."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DoctorEnvMixin:
    """doctor.main'i gecici dosya sistemi + olu gateway ile calistirir."""

    def isolated_doctor(self, stack, tmp: Path, base: str | None, port: int | None):
        (tmp / "config.json").write_text(json.dumps({
            "active_mode": "1", "model": "test-model", "protocol": "anthropic",
            "provider_models": {"1": "test-model", "2": "test-model"},
        }), encoding="utf-8")
        (tmp / "logs").mkdir(exist_ok=True)
        yollar = {
            "CONFIG_PATH": tmp / "config.json",
            "LOG_DIR": tmp / "logs",
            "LOG_PATH": tmp / "logs" / "gateway.log",
            "PID_PATH": tmp / "logs" / "gateway.pid",
            "OPENCODE_PATH": tmp / "opencode.json",
            "CLAUDE_PATH": tmp / "claude.json",
            "CHECKS": [],
            "NET": {"timeout": 1.0, "no_network": False},
        }
        if base is not None:
            yollar["GATEWAY"] = base
            yollar["PORT"] = port
        for ad, deger in yollar.items():
            stack.enter_context(mock.patch.object(doctor, ad, deger))
        out, err = io.StringIO(), io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        return out, err


class TestDoctorCli(DoctorEnvMixin, unittest.TestCase):
    def setUp(self):
        # doctor.main os.chdir(BASE) yapar; test surucunun cwd'sini dondur
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tempfile.gettempdir())

    def test_module_exposes_documented_names(self):
        for ad in ("main", "render", "render_quiet", "as_json", "verdict", "CHECKS"):
            self.assertTrue(hasattr(doctor, ad), f"doctor.{ad} yok")
        self.assertEqual(doctor.GATEWAY, "http://127.0.0.1:8756")

    def test_rotation_constants_match_supervisor(self):
        """doctor.py sabitleri supervisor.py'yi kopyalar; ayrismasi rozetleri bozar."""
        self.assertEqual(doctor.ROTATE_BYTES, supervisor.ROTATE_BYTES)
        self.assertEqual(doctor.ROTATE_KEEP, supervisor.ROTATE_KEEP)

    def test_help_lists_documented_flags(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(sys, "argv", ["doctor.py", "--help"]):
            with self.assertRaises(SystemExit) as cm:
                doctor.main()
        self.assertEqual(cm.exception.code, 0)
        for bayrak in ("--json", "--quiet", "--no-network", "--timeout"):
            self.assertIn(bayrak, out.getvalue())

    def test_net_get_reports_error_for_dead_gateway(self):
        """net_get hicbir durumda istisna atmaz, bos cevap + hata metni doner.

        Gercek ag cagrisi: tek istek, olu loopback portu, upstream YOK.
        """
        port = free_port()
        with mock.patch.object(doctor, "GATEWAY", f"http://127.0.0.1:{port}"), \
             mock.patch.object(doctor, "NET", {"timeout": 0.5, "no_network": False}):
            veri, hata = doctor.net_get("/api/hello", 0.5, False)
        self.assertIsNone(veri)
        self.assertTrue(hata, "hata metni bos olmamali")

    def test_net_get_skipped_when_no_network(self):
        veri, hata = doctor.net_get("/api/hello", 0.5, True)
        self.assertIsNone(veri)
        self.assertIn("atlandi", hata)

    def test_dead_gateway_fails_without_traceback(self):
        """Olu gateway: en az bir FAIL, izsiz cikis, temiz rapor.

        Ag katmani `net_get` ile taklit edilir: firewall acik kapali
        dongusu (ConnectTimeout ~0.7 sn x 5 cagri) testi 10 sn uzatirdi.
        Olu gateway'in GERCEK davranisi yukaridaki net_get testinde
        sinanir; burada sinanan doctor'in raporlama karari.
        """
        port = free_port()
        with tempfile.TemporaryDirectory(prefix="ox-doctor-") as d, \
             contextlib.ExitStack() as st:
            out, err = self.isolated_doctor(st, Path(d),
                                            f"http://127.0.0.1:{port}", port)
            st.enter_context(mock.patch.object(
                doctor, "net_get", return_value=(None, "baglanilamadi: ConnectError")))
            st.enter_context(mock.patch.object(sys, "argv", ["doctor.py", "--quiet"]))
            rc = doctor.main()
            cikti, hata = out.getvalue(), err.getvalue()
        self.assertEqual(rc, 1, "olu gateway icin en az bir FAIL olmali")
        self.assertIn("VERDICT", cikti)
        self.assertIn("port + /api/hello", cikti)
        self.assertIn("/api/conn", cikti)
        self.assertNotIn("Traceback", cikti + hata, "tani araci izsiz olmali")
        self.assertEqual(hata, "", "temiz rapor stderr'e yazilmamali")

    def test_no_network_run_is_valid_json(self):
        with tempfile.TemporaryDirectory(prefix="ox-doctor-") as d, \
             contextlib.ExitStack() as st:
            out, err = self.isolated_doctor(st, Path(d), None, None)
            st.enter_context(mock.patch.object(
                sys, "argv", ["doctor.py", "--no-network", "--json"]))
            rc = doctor.main()
            cikti, hata = out.getvalue(), err.getvalue()
        self.assertIn(rc, (0, 1), "0=OK/WARN, 1=FAIL; 2=arac kendi coktu")
        j = json.loads(cikti)
        self.assertIn("verdict", j)
        self.assertIn("checks", j)
        self.assertIsInstance(j["checks"], list)
        self.assertNotIn("Traceback", hata)
        adlar = {c["name"] for c in j["checks"]}
        self.assertIn("port + /api/hello", adlar, "kontrol raporu eksik olmamali")


class TestBenchCli(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["bench.py", *argv]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bench.main()
        return rc, out.getvalue(), err.getvalue()

    def test_diag_keys_track_empty_stream_counter(self):
        """bench sayaclari /api/diag ile ayni isimleri kullanmali.

        Eslesmezse 'bos-akis sayisi' raporda sessizce hep 0 gorunur.
        """
        self.assertEqual(bench.DIAG_KEYS,
                         ("turns", "empty_turns", "empty_stream_turns", "truncated_turns"))
        self.assertEqual(bench.DEFAULT_BASE, "http://127.0.0.1:8756")

    def test_help_lists_documented_flags(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["bench.py", "--help"]), \
             contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                bench.main()
        self.assertEqual(cm.exception.code, 0)
        for bayrak in ("--model", "--mode", "--n", "--concurrency", "--json",
                       "--out", "--quiet", "--timeout"):
            self.assertIn(bayrak, out.getvalue())

    def test_dead_gateway_exits_two_without_traceback(self):
        rc, out, err = self._run(["--base", f"http://127.0.0.1:{free_port()}",
                                  "--n", "1", "--json"])
        self.assertEqual(rc, 2, "gateway'a ulasilamazsa olcum yapilmaz")
        self.assertIn("ULASILAMADI", err.upper())
        self.assertEqual(out, "", "olcum baslamadan rapor basilmamali")
        self.assertNotIn("Traceback", out + err)

    def test_timeout_must_be_positive(self):
        with mock.patch.object(sys, "argv", ["bench.py", "--timeout", "0"]), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                bench.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--timeout", err.getvalue())

    def test_diag_row_records_fallback_evidence(self):
        """/api/diag 'last' kaydi satira islenir: sessiz model degisimi kaniti."""
        row: dict = {}
        bench._apply_diag(row, {"model": "yedek-model", "requested_model": "istenen-model",
                                "empty_stream": False, "models_tried": [],
                                "upstream_error": None, "client_max_tokens": 131072,
                                "sent_max_tokens": 32000})
        self.assertEqual(row["model_used"], "yedek-model")
        self.assertEqual(row["model_requested"], "istenen-model")
        self.assertTrue(row["fallback"], "model != requested_model ise sessiz gecis")
        self.assertEqual(row["chain"], "model_flaky")
        self.assertIsNone(row["empty_stream"])
        self.assertEqual(row["client_max_tokens"], 131072)
        self.assertEqual(row["sent_max_tokens"], 32000)

    def test_diag_row_chain_status_from_empty_stream(self):
        row: dict = {}
        bench._apply_diag(row, {"model": "m", "requested_model": "m", "empty_stream": True,
                                "models_tried": ["m", "yedek"], "upstream_error": "bos cevap"})
        self.assertTrue(row["empty_stream"])
        self.assertFalse(row["fallback"], "ayni model: gecis kaniti yok")
        self.assertEqual(row["chain"], "chain_down")
        self.assertEqual(row["models_tried"], ["m", "yedek"])
        self.assertEqual(row["upstream_error"], "bos cevap")
        self.assertEqual(row["upstream_error"], row["upstream_error"][:160])


if __name__ == "__main__":
    unittest.main()
