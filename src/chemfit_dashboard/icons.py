"""Portable SVG icons tinted from the active dashboard color scheme."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from PySide6.QtCore import QByteArray, QObject, QRectF, QSize, Qt
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QWidget

from chemfit_dashboard.appearance import ColorScheme, active_color_scheme

IconRole = Literal["normal", "accent", "danger"]
_ICON_DIRECTORY = Path(__file__).with_name("icons")
_NAME_PROPERTY = "chemfitIconName"
_ROLE_PROPERTY = "chemfitIconRole"


def themed_icon(
    name: str,
    scheme: ColorScheme | None = None,
    *,
    role: IconRole = "normal",
) -> QIcon:
    """Return a cached icon rendered for every required Qt state."""
    active_scheme = scheme or active_color_scheme()
    return _themed_icon(name, active_scheme, role)


@lru_cache(maxsize=256)
def _themed_icon(name: str, scheme: ColorScheme, role: IconRole) -> QIcon:
    source = (_ICON_DIRECTORY / f"{name}.svg").read_text(encoding="utf-8")
    normal_color = {
        "normal": scheme.text_strong,
        "accent": scheme.accent,
        "danger": scheme.danger,
    }[role]
    active_color = scheme.danger_border if role == "danger" else scheme.accent
    icon = QIcon()
    for size in (16, 20, 24, 32):
        icon.addPixmap(_render_svg(source, normal_color, size), QIcon.Mode.Normal)
        icon.addPixmap(_render_svg(source, scheme.muted, size), QIcon.Mode.Disabled)
        icon.addPixmap(_render_svg(source, active_color, size), QIcon.Mode.Active)
        icon.addPixmap(_render_svg(source, active_color, size), QIcon.Mode.Selected)
    return icon


def _render_svg(source: str, color: str, size: int) -> QPixmap:
    """Render one monochrome SVG without depending on platform icon themes."""
    renderer = QSvgRenderer(
        QByteArray(source.replace("#000000", color).encode("utf-8"))
    )
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def set_themed_icon(
    target: QAction | QAbstractButton,
    name: str,
    *,
    role: IconRole = "normal",
    scheme: ColorScheme | None = None,
) -> None:
    """Assign an icon while retaining enough metadata for theme changes."""
    target.setProperty(_NAME_PROPERTY, name)
    target.setProperty(_ROLE_PROPERTY, role)
    target.setIcon(themed_icon(name, scheme, role=role))


def make_icon_only(
    button: QAbstractButton,
    name: str,
    tooltip: str,
    *,
    role: IconRole = "normal",
    icon_size: int = 18,
) -> None:
    """Turn a button into an accessible, consistently sized icon action."""
    button.setText("")
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setProperty("iconOnly", "true")
    button.setIconSize(QSize(icon_size, icon_size))
    set_themed_icon(button, name, role=role)


def refresh_themed_icons(root: QWidget, scheme: ColorScheme) -> None:
    """Retint all registered buttons and actions below ``root``."""
    targets: list[QObject] = [root, *root.findChildren(QObject)]
    for target in targets:
        name = target.property(_NAME_PROPERTY)
        if not isinstance(name, str) or not name:
            continue
        role = target.property(_ROLE_PROPERTY)
        icon_role = cast(
            "IconRole",
            role
            if isinstance(role, str) and role in {"normal", "accent", "danger"}
            else "normal",
        )
        if isinstance(target, (QAction, QAbstractButton)):
            target.setIcon(themed_icon(name, scheme, role=icon_role))
