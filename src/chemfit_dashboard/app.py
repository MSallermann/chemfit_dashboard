"""
Qt application bootstrap and process-signal integration.

``run`` creates the application and main window, restores the saved session,
and keeps Python signal handling responsive while Qt owns the event loop.
"""

from __future__ import annotations

import signal
import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from chemfit_dashboard.main_window import MainWindow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from types import FrameType

    from chemfit_dashboard.extraction import ExtractionConfig


def run(
    paths: Sequence[str | Path] | None = None,
    *,
    extraction_config: ExtractionConfig | None = None,
) -> int:
    """Start the dashboard with optional locations and extraction selections."""
    instance = QApplication.instance()
    application = (
        instance if isinstance(instance, QApplication) else QApplication(sys.argv)
    )
    application.setApplicationDisplayName("ChemFit Dashboard")
    application.setApplicationName("ChemFit Context Inspector")
    application.setOrganizationName("ChemFit")
    window = MainWindow(extraction_config=extraction_config)
    if paths:
        window.coordinator.load_collection(paths)
    window.show()
    interrupted = False

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        nonlocal interrupted
        interrupted = True
        application.quit()

    previous_handler = signal.signal(signal.SIGINT, interrupt)
    signal_pulse = QTimer(application)
    signal_pulse.setInterval(100)
    signal_pulse.timeout.connect(lambda: None)
    signal_pulse.start()
    try:
        exit_code = application.exec()
    except KeyboardInterrupt:
        interrupted = True
        application.quit()
        exit_code = 0
    finally:
        signal_pulse.stop()
        signal.signal(signal.SIGINT, previous_handler)
    return 130 if interrupted else exit_code
