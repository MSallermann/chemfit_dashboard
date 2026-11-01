"""Module entry point forwarding ``python -m chemfit_dashboard`` to the CLI."""

from __future__ import annotations

from chemfit_dashboard.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
