"""Entry point used by PyInstaller desktop bundles."""

from __future__ import annotations

from multiprocessing import freeze_support

from chemfit_dashboard.cli import main

if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
