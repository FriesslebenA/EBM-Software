# -*- mode: python ; coding: utf-8 -*-

import os
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

# Platform-Plugins explizit hinzufügen (PyInstaller-Hook lässt qwindows.dll
# manchmal weg — ohne dieses Plugin startet die App unter Windows nicht)
import glob as _glob
_pyside6_plugins = os.path.join(
    os.path.dirname(os.path.abspath("abs_path_optimizer.py")),
    ".venv", "Lib", "site-packages", "PySide6", "plugins",
)
for _dll in _glob.glob(os.path.join(_pyside6_plugins, "platforms", "*.dll")):
    binaries.append((_dll, "PySide6/plugins/platforms"))
for _dll in _glob.glob(os.path.join(_pyside6_plugins, "styles", "*.dll")):
    binaries.append((_dll, "PySide6/plugins/styles"))
for _dll in _glob.glob(os.path.join(_pyside6_plugins, "imageformats", "*.dll")):
    binaries.append((_dll, "PySide6/plugins/imageformats"))


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
    [],
    exclude_binaries=True,
    name='SequenceOptimiser',
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
    version='version_info.txt',
    icon='icon.ico' if os.path.exists('icon.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='SequenceOptimiser'
)
