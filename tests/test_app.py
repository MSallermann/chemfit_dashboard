from __future__ import annotations

import signal

from PySide6.QtWidgets import QApplication

import chemfit_dashboard.app as dashboard_app


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
