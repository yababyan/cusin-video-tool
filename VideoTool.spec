# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

project_dir = Path(os.getcwd()).resolve()
datas, binaries, hiddenimports = collect_all("imageio_ffmpeg")
datas += [
    (str(project_dir / "images.jpg"), "."),
    (str(project_dir / "dota2-logo.png"), "."),
    (str(project_dir / "retro-watermark.png"), "."),
    (str(project_dir / "mode-compress.png"), "."),
    (str(project_dir / "mode-trim.png"), "."),
    (str(project_dir / "mode-retro.png"), "."),
    (str(project_dir / "mode-gif.png"), "."),
    (str(project_dir / "mode-custom.png"), "."),
    (str(project_dir / "DotA2MinimapIcons_AgADagwAAsd2IVA.png"), "."),
    (str(project_dir / "cusini_royal_video_tool.ico"), "."),
]


a = Analysis(
    [str(project_dir / "compress_video.py")],
    pathex=[str(project_dir)],
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
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VideoTool",
    icon=str(project_dir / "cusini_royal_video_tool.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
