"""Build and package the native ChemFit Dashboard application."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "ChemFit Dashboard"
BUNDLE = ROOT / "dist" / APP_NAME
RELEASE = ROOT / "release"
APPIMAGE_TOOL_URL = (
    "https://github.com/AppImage/appimagetool/releases/download/1.9.1/"
    "appimagetool-x86_64.AppImage"
)


def _run(command: list[str | Path], *, env: dict[str, str] | None = None) -> None:
    printable = " ".join(map(str, command))
    print(f"+ {printable}", flush=True)
    subprocess.run(  # noqa: S603 - every command is assembled from trusted inputs
        [str(part) for part in command],
        cwd=ROOT,
        env=env,
        check=True,
    )


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def build_bundle() -> None:
    """Create the platform-local PyInstaller directory or macOS app bundle."""
    _run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            ROOT / "ChemFitDashboard.spec",
        ]
    )


def _machine() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)


def package_linux() -> Path:
    """Wrap the standalone directory in a portable x86-64 AppImage."""
    if _machine() != "x86_64":
        msg = "Linux AppImage packaging currently supports x86-64 only"
        raise RuntimeError(msg)

    appdir = ROOT / "build" / "ChemFitDashboard.AppDir"
    _remove(appdir)
    library = appdir / "usr" / "lib" / "chemfit-dashboard"
    shutil.copytree(BUNDLE, library)
    shutil.copy2(ROOT / "packaging" / "linux" / "AppRun", appdir / "AppRun")
    shutil.copy2(
        ROOT / "packaging" / "linux" / "chemfit-dashboard.desktop",
        appdir / "chemfit-dashboard.desktop",
    )
    shutil.copy2(
        ROOT / "res" / "chemfit_dashboard-256.png",
        appdir / "chemfit-dashboard.png",
    )
    (appdir / "AppRun").chmod(0o755)

    configured_tool = os.environ.get("APPIMAGETOOL")
    tool = (
        Path(configured_tool).expanduser().resolve()
        if configured_tool
        else ROOT / "build" / "appimagetool-x86_64.AppImage"
    )
    if not tool.exists():
        print(f"Downloading {APPIMAGE_TOOL_URL}", flush=True)
        urllib.request.urlretrieve(APPIMAGE_TOOL_URL, tool)  # noqa: S310
    tool.chmod(0o755)

    RELEASE.mkdir(exist_ok=True)
    output = RELEASE / "ChemFit-Dashboard-Linux-x86_64.AppImage"
    _remove(output)
    environment = os.environ.copy()
    environment["ARCH"] = "x86_64"
    _run(
        [tool, "--appimage-extract-and-run", appdir, output],
        env=environment,
    )
    return output


def package_macos() -> Path:
    """Put the generated .app bundle in a drag-to-Applications DMG."""
    architecture = _machine()
    if architecture not in {"arm64", "x86_64"}:
        msg = f"Unsupported macOS architecture: {architecture}"
        raise RuntimeError(msg)

    app = ROOT / "dist" / f"{APP_NAME}.app"
    dmg_root = ROOT / "build" / "dmg-root"
    _remove(dmg_root)
    dmg_root.mkdir(parents=True)
    shutil.copytree(app, dmg_root / app.name, symlinks=True)
    (dmg_root / "Applications").symlink_to("/Applications")

    RELEASE.mkdir(exist_ok=True)
    output = RELEASE / f"ChemFit-Dashboard-macOS-{architecture}.dmg"
    _remove(output)
    _run(
        [
            "hdiutil",
            "create",
            "-volname",
            APP_NAME,
            "-srcfolder",
            dmg_root,
            "-ov",
            "-format",
            "UDZO",
            output,
        ]
    )
    return output


def _inno_setup_compiler() -> Path:
    configured = os.environ.get("ISCC")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        / "Inno Setup 6"
        / "ISCC.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        / "Inno Setup 6"
        / "ISCC.exe",
    ]
    discovered = shutil.which("ISCC") or shutil.which("iscc")
    if discovered:
        candidates.insert(0, Path(discovered))
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    msg = "Inno Setup 6 was not found; install it or set the ISCC environment variable"
    raise RuntimeError(msg)


def package_windows() -> Path:
    """Build a per-user Windows installer with Inno Setup."""
    if _machine() != "x86_64":
        msg = "Windows packaging currently supports x86-64 only"
        raise RuntimeError(msg)
    app_version = version("chemfit-dashboard")
    RELEASE.mkdir(exist_ok=True)
    _run(
        [
            _inno_setup_compiler(),
            f"/DAppVersion={app_version}",
            ROOT / "packaging" / "windows" / "installer.iss",
        ]
    )
    return RELEASE / "ChemFit-Dashboard-Windows-x86_64.exe"


def package_installer() -> Path:
    if sys.platform == "linux":
        return package_linux()
    if sys.platform == "darwin":
        return package_macos()
    if sys.platform == "win32":
        return package_windows()
    msg = f"Desktop packaging is not supported on {sys.platform}"
    raise RuntimeError(msg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--bundle-only",
        action="store_true",
        help="build the runnable PyInstaller application without an installer",
    )
    mode.add_argument(
        "--skip-bundle",
        action="store_true",
        help="reuse the existing dist bundle and only rebuild the installer",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if not arguments.skip_bundle:
        build_bundle()
    if arguments.bundle_only:
        print(f"Runnable application: {BUNDLE}")
        return 0
    output = package_installer()
    print(f"Installer: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
