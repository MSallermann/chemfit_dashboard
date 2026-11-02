"""Small shared widget constructors for recurring dashboard presentation roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from chemfit_dashboard.icons import IconRole, make_icon_only, set_themed_icon

if TYPE_CHECKING:
    from collections.abc import Callable

INTERACTIVE_ZOOM_STEP = 1.15


def add_clear_action(editor: QLineEdit) -> QAction:
    """Add a portable, theme-aware trailing clear action to a line edit."""
    action = QAction(editor)
    editor.addAction(action, QLineEdit.ActionPosition.TrailingPosition)
    set_themed_icon(action, "close")
    action.setToolTip("Clear")
    action.setVisible(bool(editor.text()))
    action.triggered.connect(editor.clear)
    editor.textChanged.connect(lambda text: action.setVisible(bool(text)))
    return action


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
        icon_name: str | None = None,
        icon_role: IconRole = "normal",
        icon_only: bool = False,
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
        if icon_name is not None:
            if icon_only:
                make_icon_only(button, icon_name, tooltip, role=icon_role)
                compact = True
            else:
                set_themed_icon(button, icon_name, role=icon_role)
        if compact:
            button.setFixedWidth(28)
        else:
            button.setMinimumWidth(button.fontMetrics().horizontalAdvance(text) + 22)
        self._layout.addWidget(button)
        return button


class DetachablePanelController(QObject):
    """
    Own the complete detach/restore lifecycle for one tab panel.

    A detached panel has three coordinated pieces of mutable state: its dialog,
    the placeholder left in the tab widget, and the tab index used on restore.
    Keeping them together prevents feature windows from implementing subtly
    different lifecycle rules.
    """

    def __init__(
        self,
        owner: QWidget,
        tabs: QTabWidget,
        panel: QWidget,
        *,
        tab_label: str,
        window_title: str,
        object_prefix: str,
        tab_index: int,
        expanded_changed: Callable[[bool], None] | None = None,
        detached: Callable[[], None] | None = None,
    ) -> None:
        """Configure one tab panel and its detached-window presentation."""
        super().__init__(owner)
        self._owner = owner
        self._tabs = tabs
        self._panel = panel
        self._tab_label = tab_label
        self._window_title = window_title
        self._object_prefix = object_prefix
        self._tab_index = tab_index
        self._expanded_changed = expanded_changed
        self._detached = detached
        self._dialog: QDialog | None = None
        self._placeholder: QWidget | None = None

    @property
    def dialog(self) -> QDialog | None:
        """Return the active detached window, if any."""
        return self._dialog

    @property
    def placeholder(self) -> QWidget | None:
        """Return the tab placeholder used while detached, if any."""
        return self._placeholder

    def toggle(self) -> None:
        """Detach the panel, or restore it when already detached."""
        if self._dialog is None:
            self.detach()
        else:
            self.restore()

    def show(self) -> bool:
        """Raise the detached window and return whether one was available."""
        if self._dialog is None:
            return False
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()
        return True

    def detach(self) -> None:
        """Move the panel from its tab into a non-modal top-level window."""
        if self._dialog is not None:
            return
        current_index = self._tabs.indexOf(self._panel)
        if current_index >= 0:
            self._tab_index = current_index
            self._tabs.removeTab(current_index)

        placeholder = self._build_placeholder()
        self._placeholder = placeholder
        self._tabs.insertTab(self._tab_index, placeholder, self._tab_label)
        self._tabs.setCurrentWidget(placeholder)

        dialog = QDialog(self._owner, Qt.WindowType.Window)
        dialog.setWindowTitle(self._window_title)
        dialog.setModal(False)
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.setContentsMargins(12, 12, 12, 12)
        dialog_layout.addWidget(self._panel)
        # QTabWidget keeps removed pages explicitly hidden. Reparenting does
        # not clear that flag, so the panel must be shown after moving it.
        self._panel.show()
        dialog.finished.connect(lambda _result, target=dialog: self.restore(target))
        self._dialog = dialog
        if self._expanded_changed is not None:
            self._expanded_changed(True)  # noqa: FBT003
        if self._detached is not None:
            self._detached()
        dialog.resize(
            max(1100, self._owner.width() - 80),
            max(720, self._owner.height() - 80),
        )
        dialog.showMaximized()

    def restore(self, dialog: QDialog | None = None) -> None:
        """Return the panel to its original tab and dispose of the window."""
        expanded = self._dialog
        if expanded is None or (dialog is not None and dialog is not expanded):
            return
        self._dialog = None
        layout = expanded.layout()
        if layout is not None:
            layout.removeWidget(self._panel)

        placeholder = self._placeholder
        placeholder_was_current = (
            placeholder is not None and self._tabs.currentWidget() is placeholder
        )
        if placeholder is not None:
            placeholder_index = self._tabs.indexOf(placeholder)
            if placeholder_index >= 0:
                self._tab_index = placeholder_index
                self._tabs.removeTab(placeholder_index)
            placeholder.deleteLater()
        self._placeholder = None

        insert_at = min(self._tab_index, self._tabs.count())
        self._tab_index = self._tabs.insertTab(insert_at, self._panel, self._tab_label)
        if placeholder_was_current:
            self._tabs.setCurrentWidget(self._panel)
        if self._expanded_changed is not None:
            self._expanded_changed(False)  # noqa: FBT003
        expanded.hide()
        expanded.deleteLater()

    def _build_placeholder(self) -> QWidget:
        placeholder = QWidget()
        placeholder.setObjectName(f"{self._object_prefix}DetachedPlaceholder")
        layout = QVBoxLayout(placeholder)
        layout.addStretch()
        message = QLabel(f"{self._tab_label} is currently open in a detached window.")
        message.setObjectName(f"{self._object_prefix}DetachedMessage")
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message.setWordWrap(True)
        layout.addWidget(message)
        return_button = QPushButton(f"Return {self._tab_label} to dashboard")
        prefix = self._object_prefix[0].upper() + self._object_prefix[1:]
        return_button.setObjectName(f"return{prefix}Button")
        return_button.clicked.connect(lambda _checked=False: self.restore())
        layout.addWidget(return_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        return placeholder


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
    tooltip = (
        f"Return {feature} to the dashboard"
        if expanded
        else f"Open {feature} in a large separate window"
    )
    make_icon_only(button, "collapse" if expanded else "expand", tooltip)


def interactive_zoom_factor(steps: float) -> float:
    """Return the shared exponential zoom factor for wheel or button input."""
    return INTERACTIVE_ZOOM_STEP**steps
