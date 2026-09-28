"""ox-gateway supervisor.

Tasarim amaci: HICBIR gorunur terminal penceresi acmamak.
start-detached.bat bu dosyayi pythonw.exe ile baslatir (konsolsuz),
stop-gateway.bat logs\\gateway.pid uzerinden durdurur.

- Port doluysa supervisor hicbir sey yapmadan cikar (cift instance).
- uvicorn cokerse ustel backoff ile yeniden baslatir.
- logs\\gateway.stop dosyasi varsa temiz sekilde kapanir.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

HOST = "127.0.0.1"
PORT = 8756
BACKOFF = (1.0, 2.0, 3.0, 5.0, 8.0, 15.0)
MAX_FAST_CRASHES = 8          # bu kadar hizli cokerse supervisor durur
FAST_CRASH_LIMIT = 20.0       # saniye

BASE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(BASE, "logs")
LOGFILE = os.path.join(LOGS, "gateway.log")
PIDFILE = os.path.join(LOGS, "gateway.pid")
STOPFILE = os.path.join(LOGS, "gateway.stop")

# Log rotasyonu. Tum log append-only idi, sınırsiz buyuyordu.
# Windows'ta uvicorn log handle'ini bize mirasten aliyor ve her zaman acik
# tutuyor; acik dosyayi os.replace() ile yeniden adlandirmak PermissionError
# verir. Bu yuzden rotasyon YALNIZ uvicorn kapaliyken, yani supervisor'in
# dosyayi tek sahibi oldugu anda uygulanir (cocugu oldurmeden).
ROTATE_BYTES = 8 * 1024 * 1024   # aktif log bu esigi gecince dondurulur
ROTATE_KEEP = 3                  # kac nesil tutulur: gateway.log.1 .. .3
ROTATE_TRY = 6                   # dosya kilitliyse kac kez yeniden denenir
ROTATE_TRY_WAIT = 0.1            # bekleme (saniye), toplam en fazla ~0.5 sn

_child_running = False   # uvicorn ayaktayken log dosyasi kilitli
_rotate_pending = False  # esik asildi, cocuk kapaninca uygulanacak
_rotate_day = time.strftime("%Y-%m-%d")


def rotate_log(logfile: str = LOGFILE, keep: int = ROTATE_KEEP) -> bool:
    """gateway.log -> .1, eski nesiller kayar, en eskisi yok edilir.

    True donerse dondu, False donerse kilitli/kirpilti: cagiran taraf
    bunu bir kez loglar ve birakir. Icerik sadece dosyadan dosyaya
    tasinir, hicbir sekilde okunmaz/bastirilmaz.
    """
    if not os.path.exists(logfile):
        return False
    for attempt in range(ROTATE_TRY):
        try:
            # .2 -> .3, .1 -> .2  (en eski uzerine yazilir = silinir)
            for i in range(keep - 1, 0, -1):
                src = f"{logfile}.{i}"
                if os.path.exists(src):
                    os.replace(src, f"{logfile}.{i + 1}")
            os.replace(logfile, f"{logfile}.1")
        except OSError:
            # doctor.py/editor dosyayi bir an tutuyor olabilir; kisa bekle.
            if attempt + 1 < ROTATE_TRY:
                time.sleep(ROTATE_TRY_WAIT)
                continue
            return False
        with open(logfile, "a", encoding="utf-8"):
            pass  # yeni aktif dosya bos olusur
        return True
    return False


def maybe_rotate() -> None:
    """Esik veya gun degisti mi, uvicorn kapali mi diye bakar, dondurur."""
    global _rotate_pending, _rotate_day
    day = time.strftime("%Y-%m-%d")
    if not _rotate_pending:
        if day == _rotate_day:
            try:
                if os.path.getsize(LOGFILE) < ROTATE_BYTES:
                    return
            except OSError:
                return
        _rotate_pending = True
    if _child_running:
        return  # handle cocukta: bir sonraki supervisor yazisinda uygulanir
    # default parametre degil, anlik global: LOGFILE/ROTATE_KEEP testte
    # gecici dizine yonlendirilse bile dogru dosyaya bakilir.
    if rotate_log(LOGFILE, ROTATE_KEEP):
        _rotate_pending = False
        _rotate_day = day
        log(f"log donduruldu ({ROTATE_BYTES} bayt esigi, {ROTATE_KEEP} nesil)")
    else:
        log("log dondurulemedi (dosya kilitli) - sonraki denemeye birakildi")


def log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOGFILE, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"[supervisor {stamp}] {msg}\n")


def port_busy() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((HOST, PORT)) == 0


def alive(pid: int) -> bool:
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
    except Exception:
        pass
    return False


def clear_stop() -> None:
    try:
        os.remove(STOPFILE)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _port_owner_pid() -> int | None:
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                         capture_output=True, text=True, errors="replace").stdout
    for line in out.splitlines():
        if f":{PORT}" in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                try:
                    return int(parts[-1])
                except ValueError:
                    return None
    return None


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, text=True)


def _find_supervisors() -> list[int]:
    """PID dosyasina guvenme: dosya silinmis/bozuk olabilir. Imzaya goru bul."""
    if os.name != "nt":
        return []
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*supervisor.py*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", ps], capture_output=True, text=True,
                         errors="replace", timeout=30)
    pids = []
    for tok in res.stdout.split():
        if tok.isdigit():
            pids.append(int(tok))
    return pids


def do_stop() -> int:
    os.makedirs(LOGS, exist_ok=True)
    with open(STOPFILE, "w", encoding="utf-8") as fh:
        fh.write("stop")
    killed = []

    # ONCE supervisor: yasarken port sahibini oldurmek zombi uretiyor
    # (supervisor 3 sn sonra uvicorn'u yeniden baslatiyordu).
    sups: list[int] = []
    try:
        with open(PIDFILE, encoding="utf-8") as fh:
            tok = fh.read().strip()
            if tok.isdigit():
                sups.append(int(tok))
    except OSError:
        pass
    sups.extend(_find_supervisors())
    for pid in dict.fromkeys(sups):
        if pid == os.getpid():
            continue
        _kill_tree(pid)
        killed.append(f"supervisor:{pid}")

    owner = _port_owner_pid()
    if owner:
        _kill_tree(owner)
        killed.append(f"uvicorn:{owner}")

    for p in (PIDFILE, STOPFILE):
        try:
            os.remove(p)
        except OSError:
            pass
    print("[OK] " + (", ".join(killed) if killed else "calisan surun bulunamadi"))
    return 0


def main() -> int:
    global _child_running
    os.makedirs(LOGS, exist_ok=True)
    if port_busy():
        log("port 8756 zaten dolu - supervisor cikis yapiyor (cift instance)")
        return 0
    clear_stop()
    with open(PIDFILE, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    log(f"supervisor basladi (PID {os.getpid()})")

    fast = 0
    idx = 0
    try:
        while True:
            if os.path.exists(STOPFILE):
                log("stop dosyasi bulundu - supervisor kapaniyor")
                return 0
            started = time.time()
            maybe_rotate()  # uvicorn yokken dosya kilitsiz: dondurmanin zamani
            with open(LOGFILE, "a", encoding="utf-8", errors="replace") as out:
                out.write(f"\n===== uvicorn baslatildi {time.strftime('%H:%M:%S')} "
                          f"(supervisor PID {os.getpid()}) =====\n")
                out.flush()
                proc = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "gateway:app",
                     "--host", HOST, "--port", str(PORT), "--log-level", "info"],
                    cwd=BASE, stdout=out, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                _child_running = True
                try:
                    code = proc.wait()
                finally:
                    _child_running = False
            lived = time.time() - started
            if os.path.exists(STOPFILE):
                log("stop dosyasi bulundu - supervisor kapaniyor")
                return 0
            if lived < FAST_CRASH_LIMIT:
                fast += 1
            else:
                fast = 0
            log(f"uvicorn durdu (exit={code}, {lived:.1f}s yasadi)")
            # Port doluysa uvicorn "address already in use" ile cokar ve biz
            # 8 kez deneyip gereksiz yere donguye girerdik. Tek seferde cik.
            if fast and port_busy():
                log("port 8756 baska bir surece ait - supervisor cikiyor "
                    "(cift instance koruması)")
                return 0
            if fast >= MAX_FAST_CRASHES:
                log(f"ART ARDA {fast} kez hizli cokus - supervisor duruyor "
                    f"(sonsuz donguye girmemek icin)")
                return 1
            delay = BACKOFF[min(idx, len(BACKOFF) - 1)]
            idx = min(idx + 1, len(BACKOFF) - 1)
            time.sleep(delay)
    except KeyboardInterrupt:
        log("KeyboardInterrupt - kapaniyor")
        return 0
    finally:
        for p in (PIDFILE, STOPFILE):
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    if "--stop" in sys.argv:
        sys.exit(do_stop())
    sys.exit(main())
