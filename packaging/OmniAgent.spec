# -*- mode: python ; coding: utf-8 -*-
"""Yerel macOS uygulama paketi; derleme komutu packaging/build_macos.sh içindedir."""

from pathlib import Path
import os
import tomllib

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path(SPECPATH).resolve().parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
SIGNING_IDENTITY = os.environ.get("OMNIAGENT_CODESIGN_IDENTITY") or None
datas, binaries, hiddenimports = collect_all("customtkinter")
hiddenimports += collect_submodules("omniagent")

analysis = Analysis(
    [str(ROOT / "src" / "omniagent" / "ui" / "app.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="OmniAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=SIGNING_IDENTITY,
    entitlements_file=None,
)
collected = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OmniAgent",
)
app = BUNDLE(
    collected,
    name="OmniAgent.app",
    icon=str(ROOT / "build" / "OmniAgent.icns"),
    bundle_identifier="com.omniagent.desktop",
    info_plist={
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSMicrophoneUsageDescription": "Sesli komutları almak için mikrofona erişim gerekir.",
        "NSSpeechRecognitionUsageDescription": "Sesli komutları yazıya çevirmek için konuşma tanıma gerekir.",
        "NSCameraUsageDescription": "İstediğiniz fotoğrafı çekmek için kameraya erişim gerekir.",
        "NSAppleEventsUsageDescription": "İstediğiniz Chrome ve uygulama işlemlerini yürütmek için otomasyon gerekir.",
    },
)
