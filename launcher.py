"""ox-gateway supervisor baslatici (tam ayristirma).

Sorun: start-detached.bat dogrudan pythonw baslattiginda surec, cagiran
kabugun (agent terminali, CI, PowerShell) surec agacinda kalir. O kabuk
kapaninca Windows surec agacini da olduruyor -> gateway RASTGELE dustu
(gunluk kullanımda en cok gorulen sorun).

Bu dosya kisa omurlu: supervisor'i DETACHED_PROCESS +
CREATE_NEW_PROCESS_GROUP + CREATE_BREAKAWAY_FROM_JOB bayraklariyla baslatir.
Boylece cagiran kabuk kapaninca da supervisor yasar.

Konsol acmaz, stdout/stderr tamamen kapatilir (pipe birakma sorunu yok).
"""
from __future__ import annotations

import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SUPERVISOR = os.path.join(BASE, "supervisor.py")
LOGDIR = os.path.join(BASE, "logs")


def pythonw() -> str:
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(cand):
        return cand
    # Fallback: pyenv/-0.0 env'lerde pythonw yaninda yoksa normal exe
    return exe


def main() -> int:
    os.makedirs(LOGDIR, exist_ok=True)
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP",
                 "CREATE_BREAKAWAY_FROM_JOB", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    # Windows uvicorn uzerinden konsol olmadan calisir; supervisor
    # stdout'u log dosyasina yaziyor, burada sadece bos/null disiyoruz.
    proc = subprocess.Popen(
        [pythonw(), SUPERVISOR],
        cwd=BASE,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    print(f"supervisor baslatildi (PID {proc.pid})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
