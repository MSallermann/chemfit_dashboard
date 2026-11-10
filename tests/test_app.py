from __future__ import annotations

import signal

from PySide6.QtWidgets import QApplication

import chemfit_dashboard.app as dashboard_app
from chemfit_dashboard.extraction import ExtractionConfig


def test_terminal_interrupt_quits_event_loop_and_returns_interrupt_status(monkeypatch):
    class Window:
        def __init__(self, **_kwargs):
            pass

        def show(self) -> None:
            pass

    def interrupt_during_event_loop(_application: QApplication) -> int:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        return 0

    previous_handler = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(dashboard_app, "MainWindow", Window)
    monkeypatch.setattr(QApplication, "exec", interrupt_during_event_loop)

    assert dashboard_app.run() == 130
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_passes_startup_extraction_configuration_to_window(monkeypatch):
    config = ExtractionConfig(())
    received = []

    class Window:
        def __init__(self, **kwargs):
            received.append(kwargs)

        def show(self) -> None:
            pass

    monkeypatch.setattr(dashboard_app, "MainWindow", Window)
    monkeypatch.setattr(QApplication, "exec", lambda _application: 0)

    assert dashboard_app.run(extraction_config=config) == 0
    assert received == [{"extraction_config": config}]
