# -*- mode: python ; coding: utf-8 -*-

from importlib.util import find_spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


def safe_collect_all(package_name):
    try:
        return collect_all(package_name)
    except Exception:
        return ([], [], [])


def safe_collect_support_dir(module_name, directory_name):
    try:
        module_spec = find_spec(module_name)
    except Exception:
        return []
    if module_spec is None or module_spec.origin is None:
        return []

    directory_path = Path(module_spec.origin).resolve().parent.parent / directory_name
    if not directory_path.is_dir():
        return []

    collected = []
    for file_path in directory_path.rglob("*"):
        if file_path.is_file():
            relative_parent = file_path.parent.relative_to(directory_path)
            target_dir = str(Path(directory_name) / relative_parent).replace("\\", "/")
            collected.append((str(file_path), target_dir))
    return collected


datas = []
binaries = []
hiddenimports = []
for package_name in (
    "cadquery",
    "cadquery_ocp",
    "OCP",
    "vtk",
    "vtkmodules",
    "ezdxf",
    "multimethod",
    "nlopt",
    "typish",
    "casadi",
    "path",
):
    package_datas, package_binaries, package_hiddenimports = safe_collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

datas += safe_collect_support_dir("vtkmodules", "vtk.libs")
datas += safe_collect_support_dir("OCP", "cadquery_ocp.libs")


a = Analysis(
    ["step_layer_generator.py"],
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
    name="StepLayerGenerator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    version="step_version_info.txt",
    codesign_identity=None,
    entitlements_file=None,
)
