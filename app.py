#!/usr/bin/env python3
"""
Точка входа для Windows exe и локального GUI.

  python app.py                 — веб-интерфейс (React)
  IGTagParser.exe               — то же
  IGTagParser.exe --probe       — внутренний воркер (не вызывать вручную)
  IGTagParser.exe --run_tags    — сбор без GUI
  python app.py --tk            — старый Tkinter GUI
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _crash(message: str) -> None:
    try:
        from paths import BASE_DIR, ensure_layout

        ensure_layout()
        path = BASE_DIR / "data" / "crash.log"
        path.write_text(message, encoding="utf-8")
    except Exception:
        path = ROOT / "crash.log"
        try:
            path.write_text(message, encoding="utf-8")
        except Exception:
            path = None
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        tip = f"\n\nПодробности: {path}" if path else ""
        messagebox.showerror("IG Tag Parser — ошибка", message[:1500] + tip)
        root.destroy()
    except Exception:
        pass


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

            code = main()
            # curl_cffi/instagrapi оставляют non-daemon потоки —
            # без _exit windowed exe после CLI-сбора не закрывается.
            if getattr(sys, "frozen", False):
                os._exit(code)
            return code
        if mode in ("--tk", "tk"):
            from gui_app import run_gui
            return run_gui()
        if mode in ("--gui", "gui"):
            from api_server import run_ui_server
            return run_ui_server()
        if mode in ("-h", "--help"):
            print(__doc__)
            return 0

    from api_server import run_ui_server
    return run_ui_server()


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    try:
        raise SystemExit(_dispatch())
    except SystemExit:
        raise
    except Exception:
        _crash(traceback.format_exc())
        raise SystemExit(1)
