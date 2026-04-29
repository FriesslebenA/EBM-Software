# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


def safe_collect(collector, package_name):
    try:
        return collector(package_name)
    except Exception:
        return []


hiddenimports = [
    "numpy",
    "PIL",
    "PIL.Image",
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtWidgets",
    "shiboken6",
]
datas = safe_collect(collect_data_files, "PySide6")
binaries = (
    safe_collect(collect_dynamic_libs, "PySide6")
    + safe_collect(collect_dynamic_libs, "shiboken6")
)


a = Analysis(
    ['abs_path_optimizer.py'],
    pathex=[],
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
    name='SequenceOptimiser',
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
