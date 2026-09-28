# -*- coding: utf-8 -*-
"""supervisor.py log rotasyonu testleri (gecici dizin, hicbir surec yok).

Kapsam: rotate_log(logfile, keep) ve maybe_rotate().
supervisor hicbir zaman GERCEK gateway BASLATMAZ; yalnizca iki saf
fonksiyon cagrilir. LOGFILE/PIDFILE/STOPFILE modul globali her testte
gecici dizine yonlendirilir ve test sonunda ESKI degerine dondurulur;
gercek logs\\gateway.log, gateway.pid, gateway.stop DOKUNULMAZ.

Esik 8 MB'dir; testler 8 MB yazmaz, seyrek (sparse) dosya olusturur.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import supervisor


class TempLogMixin:
    """supervisor'in tum yol/state global'ini gecici dizine yonlendirir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ox-sup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.logfile = os.path.join(self.tmp.name, "gateway.log")
        yeni = {
            "LOGS": self.tmp.name,
            "LOGFILE": self.logfile,
            "PIDFILE": os.path.join(self.tmp.name, "gateway.pid"),
            "STOPFILE": os.path.join(self.tmp.name, "gateway.stop"),
            "_child_running": False,
            "_rotate_pending": False,
            "_rotate_day": time.strftime("%Y-%m-%d"),
        }
        for ad, deger in yeni.items():
            eski = getattr(supervisor, ad)
            self.addCleanup(setattr, supervisor, ad, eski)
            setattr(supervisor, ad, deger)

    def write_log(self, text="", size=None):
        """Aktif logu yazar; size verilirse seyrek dosya olusturur."""
        with open(self.logfile, "a", encoding="utf-8") as fh:
            fh.write(text)
            if size is not None:
                fh.truncate(size)
        return self.logfile

    def over_threshold(self):
        """Esigi (ROTATE_BYTES) gercekten asan log: 8 MB yazmadan truncate."""
        return self.write_log("", size=supervisor.ROTATE_BYTES + 1)

    def generations(self):
        """gateway.log.1 .. .N -> sirali nesil numaralari."""
        out = []
        for ad in os.listdir(self.tmp.name):
            kuyruk = ad.rsplit(".", 1)[-1]
            if ad.startswith("gateway.log.") and kuyruk.isdigit():
                out.append(int(kuyruk))
        return sorted(out)

    def gen_text(self, n):
        return Path(f"{self.logfile}.{n}").read_text(encoding="utf-8")


class TestRotateLog(TempLogMixin, unittest.TestCase):
    def test_constants_match_documented_values(self):
        self.assertEqual(supervisor.ROTATE_BYTES, 8 * 1024 * 1024)
        self.assertEqual(supervisor.ROTATE_KEEP, 3)

    def test_missing_file_returns_false_without_raising(self):
        self.assertFalse(supervisor.rotate_log(self.logfile, supervisor.ROTATE_KEEP))

    def test_missing_directory_returns_false_without_raising(self):
        yok = os.path.join(self.tmp.name, "olmayan-dizin", "gateway.log")
        self.assertFalse(supervisor.rotate_log(yok, supervisor.ROTATE_KEEP))

    def test_rotate_creates_exactly_one_generation(self):
        self.write_log("birinci nesil\n")
        self.assertTrue(supervisor.rotate_log(self.logfile, supervisor.ROTATE_KEEP))
        self.assertEqual(self.generations(), [1], "tek donus tek nesil olusturmalı")
        self.assertEqual(self.gen_text(1), "birinci nesil\n")
        self.assertTrue(os.path.exists(self.logfile), "aktif log yeniden olusmali")
        self.assertEqual(Path(self.logfile).read_text(encoding="utf-8"), "",
                         "yeni aktif dosya bos olusmali")

    def test_generations_shift_and_oldest_is_deleted(self):
        keep = supervisor.ROTATE_KEEP
        for i in range(1, keep + 3):          # esigi asacak kadar dondur
            self.write_log(f"n{i}\n")
            self.assertTrue(supervisor.rotate_log(self.logfile, keep))
        self.assertEqual(self.generations(), list(range(1, keep + 1)),
                         f"nesil sayisi ROTATE_KEEP={keep} ile sinirli kalmali")
        self.assertFalse([p for p in os.listdir(self.tmp.name)
                          if p not in ("gateway.log", "gateway.log.1",
                                       "gateway.log.2", "gateway.log.3")],
                         "fazla nesil birikmemeli")
        # .1 = en yeni, .ROTATE_KEEP = en eski
        self.assertEqual(self.gen_text(1), f"n{keep + 2}\n")
        self.assertEqual(self.gen_text(keep), f"n{keep}\n")

    def test_content_is_moved_not_duplicated(self):
        self.write_log("tek kopya\n")
        supervisor.rotate_log(self.logfile, supervisor.ROTATE_KEEP)
        self.assertEqual(self.gen_text(1), "tek kopya\n")
        self.assertEqual(Path(self.logfile).read_text(encoding="utf-8"), "")

    def test_rotation_failure_does_not_raise_and_keeps_log(self):
        """Dosya kilitliyse rotate_log False doner; aktif log KAYBOLMAZ."""
        self.write_log("kayip olmasin\n")
        with mock.patch.object(supervisor.os, "replace",
                               side_effect=PermissionError(32, "dosya kilitli")) as rp, \
             mock.patch.object(supervisor.time, "sleep") as slp:
            sonuc = supervisor.rotate_log(self.logfile, supervisor.ROTATE_KEEP)
        self.assertFalse(sonuc, "kilitli dosyada dondurme False donmeli")
        self.assertEqual(slp.call_count, supervisor.ROTATE_TRY - 1,
                         "tum denemeler tukenmeden vazgecilmemeli")
        self.assertGreaterEqual(rp.call_count, supervisor.ROTATE_TRY)
        self.assertEqual(self.generations(), [], "yarim nesil olusmamali")
        self.assertEqual(Path(self.logfile).read_text(encoding="utf-8"), "kayip olmasin\n",
                         "basarisiz dondurmede aktif log silinmemeli")


class TestMaybeRotate(TempLogMixin, unittest.TestCase):
    def test_no_rotation_below_threshold(self):
        self.write_log("kucuk log\n")
        with mock.patch.object(supervisor, "rotate_log") as rl:
            supervisor.maybe_rotate()
        rl.assert_not_called()
        self.assertEqual(self.generations(), [])
        self.assertFalse(supervisor._rotate_pending)

    def test_missing_logfile_is_silent_noop(self):
        with mock.patch.object(supervisor, "rotate_log") as rl:
            supervisor.maybe_rotate()          # dosya yok: OSError yutulmali
        rl.assert_not_called()
        self.assertFalse(supervisor._rotate_pending)

    def test_no_rename_while_child_running(self):
        """uvicorn ayaktayken log handle kilitli: rename DENENMEMELI."""
        self.over_threshold()
        supervisor._child_running = True
        with mock.patch.object(supervisor.os, "replace") as rp:
            supervisor.maybe_rotate()          # istisna firlatmamali
        rp.assert_not_called()
        self.assertTrue(supervisor._rotate_pending,
                        "esik asildi: cocuk kapaninca uygulanmak uzere beklemeli")
        self.assertEqual(self.generations(), [], "cocuk varken dosyaya dokunulmamali")
        self.assertEqual(os.path.getsize(self.logfile), supervisor.ROTATE_BYTES + 1)

    def test_rotates_when_child_is_down(self):
        self.over_threshold()
        supervisor.maybe_rotate()
        self.assertEqual(self.generations(), [1])
        self.assertEqual(os.path.getsize(self.logfile + ".1"), supervisor.ROTATE_BYTES + 1)
        self.assertFalse(supervisor._rotate_pending, "basarili dondurmede bekleyen is temizlenmeli")
        # yeni aktif dosya bos olusur, sonra supervisor'in notu yazilir
        yeni = Path(self.logfile).read_text(encoding="utf-8")
        self.assertIn("log donduruldu", yeni)
        self.assertLess(len(yeni), 200)

    def test_pending_rotation_applies_after_child_stops(self):
        self.over_threshold()
        supervisor._child_running = True
        supervisor.maybe_rotate()
        self.assertEqual(self.generations(), [])
        supervisor._child_running = False
        supervisor.maybe_rotate()
        self.assertEqual(self.generations(), [1], "cocuk durunca bekleyen dondurme uygulanmali")
        self.assertFalse(supervisor._rotate_pending)


class TestSupervisorGlobalsRestored(unittest.TestCase):
    """Diger testlerden sonra supervisor'in gercek yollari geri gelmeli.

    Sinif adi alfabetik olarak en sonda oldugu icin bu kontrol diger
    testler BITMISTEN sonra calisir.
    """

    def test_paths_and_state_restored(self):
        self.assertEqual(supervisor.LOGFILE,
                         os.path.join(supervisor.BASE, "logs", "gateway.log"))
        self.assertEqual(os.path.basename(supervisor.PIDFILE), "gateway.pid")
        self.assertEqual(os.path.basename(supervisor.STOPFILE), "gateway.stop")
        self.assertFalse(supervisor._child_running)
        self.assertFalse(supervisor._rotate_pending)

    def test_no_test_process_was_spawned(self):
        """rotate_log/maybe_rotate saf fonksiyonlardir: surec cagiramazlar."""
        import inspect
        kaynak = (inspect.getsource(supervisor.rotate_log)
                  + inspect.getsource(supervisor.maybe_rotate))
        for yasak in ("subprocess", "Popen", "os.system", "taskkill", "netstat"):
            self.assertNotIn(yasak, kaynak,
                             f"rotasyon yolu {yasak} cagiramaz (surec dogmamali)")


if __name__ == "__main__":
    unittest.main()
