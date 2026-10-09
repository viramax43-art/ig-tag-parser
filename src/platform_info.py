"""Windows runtime info for logs and UI (Win10 / Win11, WebView2)."""
from __future__ import annotations

import platform
import sys


def _windows_build() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except Exception:
        return None


def windows_label() -> str:
    if sys.platform != "win32":
        return platform.system()
    build = _windows_build()
    if build is not None and build >= 22000:
        return "Windows 11"
    if build is not None and build >= 10240:
        return "Windows 10"
    return f"Windows {platform.release()}"


def webview2_installed() -> bool | None:
    """None if not Windows or registry check failed."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        subkeys = (
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
            r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
            r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        )
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in subkeys:
                try:
                    with winreg.OpenKey(hive, sub) as key:
                        winreg.QueryValueEx(key, "pv")
                        return True
                except OSError:
                    continue
    except Exception:
        pass
    return False


def runtime_summary() -> dict:
    build = _windows_build()
    label = windows_label()
    wv2 = webview2_installed()
    short = f"{label} ({platform.machine()})"
    if build:
        short = f"{label} build {build} ({platform.machine()})"
    return {
        "platform": "windows" if sys.platform == "win32" else platform.system().lower(),
        "os_label": label,
        "os_build": build,
        "arch": platform.machine(),
        "webview2": wv2,
        "short": short,
        "instagram_note": (
            "Запросы к Instagram идут только через прокси аккаунтов; "
            "интерфейс программы — localhost, доступ к instagram.com для UI не нужен."
        ),
    }


def log_runtime_banner(log_fn) -> None:
    info = runtime_summary()
    log_fn(f"[i] ОС: {info['short']}")
    if info.get("webview2") is False:
        log_fn(
            "[i] WebView2 не найден — окно может открыться в браузере. "
            "На Windows 10 установите «Microsoft Edge WebView2 Runtime» (Evergreen)."
        )
    log_fn(f"[i] {info['instagram_note']}")
