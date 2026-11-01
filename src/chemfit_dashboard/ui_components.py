"""Small shared widget constructors for recurring dashboard presentation roles."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

INTERACTIVE_ZOOM_STEP = 1.15


class ActionRow(QWidget):
    """Shared horizontal container for named items and compact row actions."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        margins: tuple[int, int, int, int] = (2, 1, 2, 1),
        spacing: int = 3,
    ) -> None:
        """Initialize one action row with shared compact layout defaults."""
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(*margins)
        self._layout.setSpacing(spacing)

    def add_widget(self, widget: QWidget, stretch: int = 0) -> None:
        """Append non-action content using the row's shared spacing policy."""
        self._layout.addWidget(widget, stretch)

    def add_action(
        self,
        name: str,
        text: str,
        tooltip: str,
        *,
        checkable: bool = False,
        compact: bool = False,
        enabled: bool = True,
    ) -> QToolButton:
        """Create and append a consistently sized, accessible row action."""
        button = QToolButton(self)
        button.setObjectName(name)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setCheckable(checkable)
        button.setAutoRaise(True)
        button.setEnabled(enabled)
        if compact:
            button.setFixedWidth(28)
        else:
            button.setMinimumWidth(button.fontMetrics().horizontalAdvance(text) + 22)
        self._layout.addWidget(button)
        return button


def muted_label(
    text: str = "",
    *,
    word_wrap: bool = False,
    parent: QWidget | None = None,
) -> QLabel:
    """Create a secondary-text label with consistent styling and wrapping."""
    label = QLabel(text, parent)
    label.setObjectName("mutedLabel")
    label.setWordWrap(word_wrap)
    return label


def panel_layout(parent: QWidget) -> QVBoxLayout:
    """Create the standard flush, eight-pixel-spaced feature-panel layout."""
    layout = QVBoxLayout(parent)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    return layout


def expand_button(feature: str) -> QPushButton:
    """Create the shared detach/return control for an expandable panel."""
    button = QPushButton()
    set_expand_button_state(button, feature, expanded=False)
    return button


def set_expand_button_state(
    button: QPushButton, feature: str, *, expanded: bool
) -> None:
    """Apply consistent text and help for a detachable panel control."""
    button.setText("Collapse" if expanded else "Expand")
    button.setToolTip(
        f"Return {feature} to the dashboard"
        if expanded
        else f"Open {feature} in a large separate window"
    )


def interactive_zoom_factor(steps: float) -> float:
    """Return the shared exponential zoom factor for wheel or button input."""
    return INTERACTIVE_ZOOM_STEP**steps
