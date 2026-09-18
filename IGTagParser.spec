# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller: IG Tag Parser GUI."""
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

datas = []
binaries = []
hiddenimports = [
    "pyotp",
    "dotenv",
    "xlsxwriter",
    "curl_cffi",
    "instagrapi",
    "pydantic",
    "gui_app",
    "run_tags",
    "probe",
    "accounts",
    "login",
    "session_req",
    "refresh_tokens",
    "export_xlsx",
    "paths",
]

for pkg in ("curl_cffi", "instagrapi", "certifi"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

hiddenimports += collect_submodules("instagrapi")

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
