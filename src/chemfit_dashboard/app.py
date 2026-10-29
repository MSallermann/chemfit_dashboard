from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QApplication

from chemfit_dashboard.appearance import application_settings
from chemfit_dashboard.main_window import MainWindow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def run(paths: Sequence[str | Path] | None = None) -> int:
    instance = QApplication.instance()
    application = (
        instance if isinstance(instance, QApplication) else QApplication(sys.argv)
    )
    application.setApplicationName("ChemFit Context Inspector")
    application.setOrganizationName("ChemFit")
    window = MainWindow(
        initial_paths=list(paths or []), settings=application_settings()
    )
    window.show()
    return application.exec()
