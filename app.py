#!/usr/bin/env python3
"""
Точка входа для Windows exe и локального GUI.

  python app.py                 — окно управления
  IGTagParser.exe               — то же
  IGTagParser.exe --probe       — внутренний воркер (не вызывать вручную)
  IGTagParser.exe --run_tags    — сбор без GUI
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _dispatch() -> int:
    import os

    if len(sys.argv) > 1:
        mode = sys.argv[1]
        # Срезаем служебный флаг, чтобы дочерние main() видели свои аргументы.
        rest = sys.argv[2:]
        sys.argv = [sys.argv[0], *rest]

        # Worker-режимы: os._exit — иначе фоновые потоки (curl/instagrapi)
        # оставляют процесс живым и родитель зависает на wait().
        if mode in ("--probe", "probe"):
            from probe import main
            os._exit(main())
        if mode in ("--refresh_tokens", "refresh_tokens"):
            from refresh_tokens import main
            os._exit(main())
        if mode in ("--export_xlsx", "export_xlsx"):
            from export_xlsx import main
            return main()
        if mode in ("--run_tags", "run_tags"):
            from run_tags import main
            return main()
        if mode in ("--gui", "gui"):
            from gui_app import run_gui
            return run_gui()
        if mode in ("-h", "--help"):
            print(__doc__)
            return 0

    from gui_app import run_gui
    return run_gui()


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    raise SystemExit(_dispatch())
