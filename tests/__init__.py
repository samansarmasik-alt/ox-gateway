# -*- coding: utf-8 -*-
"""ox-gateway regresyon test paketi.

gateway.py SALT OKUNUR: testler yalnizca iceri aktarir, hicbir sey degistirmez.
Gereken durum degisiklikleri (CONFIG, save_config, upstream) test icinde
mock/guard ile yapilir, disk ve vault ASLA dokunulmaz.
"""

import sys
from pathlib import Path

# pytest altinda da calissin diye depo kokunu sys.path'e ekle
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
