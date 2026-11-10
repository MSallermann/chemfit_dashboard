"""PyInstaller definition for the standalone ChemFit Dashboard application."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(SPEC).resolve().parent
APP_NAME = "ChemFit Dashboard"

if sys.platform == "win32":
    app_icon = ROOT / "res" / "chemfit_dashboard.ico"
elif sys.platform == "darwin":
    app_icon = ROOT / "res" / "chemfit_dashboard.icns"
else:
    app_icon = None

analysis = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (
            str(ROOT / "src" / "chemfit_dashboard" / "icons"),
            "chemfit_dashboard/icons",
        )
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # These optional integrations may be present in a developer environment,
    # but the dashboard neither imports nor needs them at runtime.
    excludes=[
        "PySide6.QtNetwork",
        "numpy",
        "psutil",
        "threadpoolctl",
        "yaml",
    ],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(app_icon) if app_icon is not None else None,
)

application = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        application,
        name=f"{APP_NAME}.app",
        icon=str(app_icon),
        bundle_identifier="io.github.msallermann.chemfit-dashboard",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleName": APP_NAME,
            "NSHighResolutionCapable": True,
        },
    )
