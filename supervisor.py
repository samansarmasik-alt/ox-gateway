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
                code = proc.wait()
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
