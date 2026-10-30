from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths

SETTINGS_FILENAME = "settings.ini"
COLOR_SCHEME_SETTING = "appearance/color_scheme"
FONT_SCALE_SETTING = "appearance/font_scale"
COMPONENT_LABEL_METADATA_KEY_SETTING = "display/component_label_metadata_key"
COMBO_CHEVRON_PATH = (
    Path(__file__).with_name("icons").joinpath("chevron_down.svg").as_posix()
)


@dataclass(frozen=True, slots=True)
class ColorScheme:
    key: str
    label: str
    window: str
    chrome: str
    panel: str
    panel_alt: str
    input: str
    border: str
    border_strong: str
    text: str
    text_strong: str
    muted: str
    accent: str
    accent_soft: str
    accent_text: str
    button: str
    button_hover: str
    button_border: str
    selection: str
    chart: str
    graph: str
    hierarchy_edge: str
    dependency_edge: str
    warning: str
    danger: str
    danger_border: str
    node: str
    node_complete: str
    node_skipped: str
    node_partial: str
    node_missing: str
    node_border: str
    status_complete: str
    status_skipped: str
    status_partial: str
    status_missing: str
    series: tuple[str, ...]


COLOR_SCHEMES = (
    ColorScheme(
        "midnight",
        "Midnight",
        "#111820",
        "#0d141b",
        "#0f171e",
        "#131c24",
        "#18232c",
        "#263641",
        "#64808e",
        "#dce5ec",
        "#f4f8fa",
        "#81909c",
        "#70ddbb",
        "#173d39",
        "#effffb",
        "#1d665d",
        "#23766b",
        "#2b8378",
        "#214b4a",
        "#111a22",
        "#101820",
        "#496573",
        "#d8a453",
        "#f2bd67",
        "#ff4d5f",
        "#fff2f4",
        "#1d3039",
        "#17443f",
        "#4b3c22",
        "#4a2d2a",
        "#252d33",
        "#55707d",
        "#67d7b4",
        "#f2bd67",
        "#ed8b73",
        "#7d8996",
        ("#70ddbb", "#72a7ff", "#f3b35c", "#d88cff", "#ff7f8d", "#92d65c"),
    ),
    ColorScheme(
        "light",
        "Light",
        "#f5f7f9",
        "#e8edf1",
        "#eef2f5",
        "#ffffff",
        "#ffffff",
        "#c7d1d9",
        "#758895",
        "#26343d",
        "#111a20",
        "#647681",
        "#087f6d",
        "#d7eee9",
        "#ffffff",
        "#087f6d",
        "#0a6c5e",
        "#056456",
        "#cfe9e4",
        "#ffffff",
        "#f3f7f9",
        "#7f9aa8",
        "#a96700",
        "#a96700",
        "#c6283e",
        "#ffffff",
        "#e4edf1",
        "#ccebe4",
        "#f7e7bd",
        "#f6d7d2",
        "#e4e8eb",
        "#79909c",
        "#087f6d",
        "#9a6200",
        "#b13d2d",
        "#687983",
        ("#087f6d", "#2667b5", "#b06a00", "#7d45b5", "#c6283e", "#4e8613"),
    ),
    ColorScheme(
        "nord",
        "Nord",
        "#2e3440",
        "#242933",
        "#292f3a",
        "#343b49",
        "#3b4352",
        "#4c566a",
        "#7f8ca3",
        "#d8dee9",
        "#eceff4",
        "#9aa7b8",
        "#88c0d0",
        "#3b515b",
        "#f7ffff",
        "#446b78",
        "#527f8d",
        "#5e8794",
        "#425b66",
        "#303744",
        "#2b313d",
        "#71849b",
        "#ebcb8b",
        "#ebcb8b",
        "#bf616a",
        "#fff2f4",
        "#3b4656",
        "#365b57",
        "#65583c",
        "#62464b",
        "#3a414e",
        "#718096",
        "#8fbcbb",
        "#ebcb8b",
        "#d08770",
        "#8996a8",
        ("#88c0d0", "#81a1c1", "#ebcb8b", "#b48ead", "#bf616a", "#a3be8c"),
    ),
    ColorScheme(
        "high_contrast",
        "High Contrast",
        "#000000",
        "#000000",
        "#050505",
        "#0b0b0b",
        "#101010",
        "#a0a0a0",
        "#ffffff",
        "#ffffff",
        "#ffffff",
        "#d0d0d0",
        "#00ffff",
        "#003f3f",
        "#000000",
        "#006f6f",
        "#009090",
        "#00ffff",
        "#005050",
        "#000000",
        "#000000",
        "#ffffff",
        "#ffff00",
        "#ffff00",
        "#ff4055",
        "#ffffff",
        "#101820",
        "#003f3f",
        "#4a3a00",
        "#4a1010",
        "#202020",
        "#ffffff",
        "#00ffff",
        "#ffff00",
        "#ff806d",
        "#d0d0d0",
        ("#00ffff", "#61a8ff", "#ffff00", "#ff75ff", "#ff4055", "#72ff72"),
    ),
)

DEFAULT_COLOR_SCHEME = "midnight"
_SCHEMES = {scheme.key: scheme for scheme in COLOR_SCHEMES}
_active_scheme = _SCHEMES[DEFAULT_COLOR_SCHEME]


def color_scheme(key: str | None) -> ColorScheme:
    return _SCHEMES.get(key or "", _SCHEMES[DEFAULT_COLOR_SCHEME])


def active_color_scheme() -> ColorScheme:
    return _active_scheme


def set_active_color_scheme(key: str | None) -> ColorScheme:
    global _active_scheme  # noqa: PLW0603
    _active_scheme = color_scheme(key)
    return _active_scheme


def application_settings_path(config_directory: str | Path | None = None) -> Path:
    if config_directory is None:
        config_directory = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation
        )
    directory = Path(config_directory) if config_directory else Path.home() / ".config"
    return directory / SETTINGS_FILENAME


def application_settings(config_directory: str | Path | None = None) -> QSettings:
    return QSettings(
        str(application_settings_path(config_directory)), QSettings.Format.IniFormat
    )


def stylesheet_for(scheme: ColorScheme) -> str:
    return f"""
QWidget {{
    background: {scheme.window};
    color: {scheme.text};
    font-family: "Inter", "Noto Sans", sans-serif;
    font-size: 13px;
}}
QMainWindow, QMenuBar, QMenu, QStatusBar {{ background: {scheme.chrome}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {scheme.selection}; }}
#appHeader {{ background: {scheme.chrome}; border-bottom: 1px solid {scheme.border}; }}
#brand {{ color: {scheme.text_strong}; font-size: 15px; font-weight: 700; letter-spacing: 2px; }}
#eyebrow, #sectionLabel {{ color: {scheme.accent}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
#inspectorTitle {{ color: {scheme.text_strong}; font-size: 25px; font-weight: 650; }}
#plotSelection {{ color: {scheme.text_strong}; font-size: 14px; font-weight: 650; }}
#actionGroupLabel {{ color: {scheme.accent}; font-size: 10px; font-weight: 750; letter-spacing: 1px; }}
#mutedLabel {{ color: {scheme.muted}; }}
#sidePanel {{ background: {scheme.panel}; border-right: 1px solid {scheme.border}; }}
#treePanel {{ background: {scheme.panel_alt}; border-right: 1px solid {scheme.border}; }}
#inspectorActions {{
    background: {scheme.panel_alt}; border: 1px solid {scheme.border}; border-radius: 7px;
}}
#inspectorActions QPushButton {{
    background: {scheme.panel}; color: {scheme.text_strong}; border-color: {scheme.border_strong};
    padding: 7px 11px;
}}
#inspectorActions QPushButton:hover {{
    background: {scheme.input}; border-color: {scheme.accent};
}}
#inspectorActions QPushButton[primaryAction="true"] {{
    background: {scheme.button}; color: {scheme.accent_text}; border-color: {scheme.button_border};
}}
#inspectorActions QPushButton[extractAction="true"] {{
    background: {scheme.accent_soft}; color: {scheme.text_strong}; border-color: {scheme.accent};
}}
#inspectorActions QPushButton:disabled {{
    background: {scheme.panel}; color: {scheme.muted}; border-color: {scheme.border};
}}
QPushButton {{
    background: {scheme.button}; border: 1px solid {scheme.button_border}; border-radius: 5px;
    color: {scheme.accent_text}; font-weight: 600; padding: 8px 13px;
}}
QPushButton:hover {{ background: {scheme.button_hover}; }}
QPushButton:disabled {{ color: {scheme.muted}; background: {scheme.panel}; border-color: {scheme.border}; }}
QCheckBox {{
    background: {scheme.panel_alt}; border: 1px solid {scheme.border}; border-radius: 5px;
    color: {scheme.text}; padding: 5px 8px; spacing: 7px;
}}
QCheckBox:hover {{ background: {scheme.input}; border-color: {scheme.border_strong}; color: {scheme.text_strong}; }}
QCheckBox:checked {{
    background: {scheme.accent_soft}; border-color: {scheme.accent}; color: {scheme.text_strong};
}}
QCheckBox::indicator {{
    width: 15px; height: 15px; background: {scheme.chrome};
    border: 2px solid {scheme.border_strong}; border-radius: 3px;
}}
QCheckBox::indicator:hover {{ border-color: {scheme.text}; }}
QCheckBox::indicator:checked {{ background: {scheme.accent}; border-color: {scheme.text_strong}; }}
QCheckBox:disabled {{ color: {scheme.muted}; border-color: {scheme.border}; }}
QCheckBox::indicator:disabled {{ background: {scheme.panel}; border-color: {scheme.border}; }}
QLineEdit, QComboBox {{
    background: {scheme.input}; border: 1px solid {scheme.border}; border-radius: 5px;
    padding: 8px 9px; selection-background-color: {scheme.selection};
}}
QComboBox {{ padding-right: 36px; }}
QComboBox:hover {{ border-color: {scheme.accent}; }}
QComboBox::drop-down {{
    background: {scheme.panel_alt}; border: none; border-left: 1px solid {scheme.border};
    border-top-right-radius: 5px; border-bottom-right-radius: 5px; width: 30px;
}}
QComboBox::drop-down:hover {{ background: {scheme.accent_soft}; }}
QComboBox::down-arrow {{ image: url("{COMBO_CHEVRON_PATH}"); width: 11px; height: 7px; }}
QComboBox QAbstractItemView {{
    background: {scheme.input}; border: 1px solid {scheme.border_strong};
    selection-background-color: {scheme.selection}; selection-color: {scheme.text_strong};
}}
QListView, QTreeView, QTableView {{
    background: {scheme.chart}; alternate-background-color: {scheme.panel_alt};
    border: 1px solid {scheme.border}; border-radius: 4px; outline: none;
    selection-background-color: {scheme.selection}; selection-color: {scheme.text_strong};
}}
QListView::item {{ padding: 9px 7px; border-bottom: 1px solid {scheme.border}; }}
QTreeView::item, QTableView::item {{ padding: 4px; }}
QTableWidget#extractionSelections QLineEdit {{
    background: {scheme.input}; border: 1px solid {scheme.accent}; border-radius: 2px;
    padding: 1px 4px; margin: 0;
}}
QTableWidget#extractionSelections QToolButton {{
    background: transparent; color: {scheme.text}; border: 1px solid transparent;
    border-radius: 3px; padding: 3px 5px;
}}
QTableWidget#extractionSelections QToolButton:hover {{
    background: {scheme.input}; border-color: {scheme.border_strong};
}}
QTableWidget#extractionSelections QToolButton:checked {{
    background: {scheme.accent_soft}; color: {scheme.accent}; border-color: {scheme.accent};
}}
QHeaderView::section {{
    background: {scheme.input}; color: {scheme.muted}; border: none;
    border-right: 1px solid {scheme.border}; border-bottom: 1px solid {scheme.border};
    padding: 7px; font-size: 11px; font-weight: 650;
}}
QTabWidget::pane {{ border: 1px solid {scheme.border}; border-radius: 4px; }}
QTabBar::tab {{
    background: {scheme.panel}; color: {scheme.muted}; padding: 9px 14px;
    border: 1px solid {scheme.border}; border-bottom: none;
}}
QTabBar::tab:selected {{ background: {scheme.panel_alt}; color: {scheme.accent}; }}
QTabWidget#workspaceTabs::pane {{
    background: {scheme.panel_alt}; border: 1px solid {scheme.border_strong};
    border-top: 3px solid {scheme.accent}; border-radius: 6px;
}}
QTabBar#workspaceTabBar::tab {{
    background: {scheme.panel}; color: {scheme.text};
    border: 1px solid {scheme.border_strong}; border-bottom: none;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    font-size: 14px; font-weight: 700; min-width: 150px;
    padding: 12px 20px; margin-right: 4px;
}}
QTabBar#workspaceTabBar::tab:hover {{
    background: {scheme.input}; color: {scheme.text_strong}; border-color: {scheme.accent};
}}
QTabBar#workspaceTabBar::tab:selected {{
    background: {scheme.accent_soft}; color: {scheme.accent};
    border: 2px solid {scheme.accent}; border-bottom: none;
    padding-top: 13px;
}}
QSplitter::handle {{ background: {scheme.border}; width: 1px; }}
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {scheme.border}; }}
"""
