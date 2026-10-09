# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: IG Tag Parser GUI."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
ROOT = Path(SPECPATH)

datas = []
binaries = []
hiddenimports = [
    "pyotp",
    "dotenv",
    "xlsxwriter",
    "curl_cffi",
    "instagrapi",
    "pydantic",
    "fastapi",
    "uvicorn",
    "starlette",
    "gui_app",
    "api_server",
    "run_tags",
    "probe",
    "accounts",
    "login",
    "session_req",
    "refresh_tokens",
    "export_xlsx",
    "paths",
    "state_db",
    "proxy_pool",
    "settings",
    "webview",
]

ui_dist = ROOT / "ui" / "dist"
if ui_dist.exists():
    datas.append((str(ui_dist), "ui/dist"))

for pkg in ("curl_cffi", "instagrapi", "certifi", "uvicorn", "webview"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

hiddenimports += collect_submodules("instagrapi")
hiddenimports += collect_submodules("uvicorn")
try:
    hiddenimports += collect_submodules("webview")
except Exception:
    pass

a = Analysis(
    ["app.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IGTagParser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="IGTagParser",
)
