from __future__ import annotations

from PySide6.QtGui import QPalette


def palette_is_dark(widget) -> bool:
    color = widget.palette().color(QPalette.Window)
    return color.lightness() < 128


def apply_workspace_theme(app, devices_workspace, console) -> str:
    """Apply one light/dark-aware theme to the workspace and console."""
    dark = palette_is_dark(app)
    theme = "dark" if dark else "light"

    colors = (
        {
            "surface": "#20242a",
            "surface_alt": "#292f36",
            "surface_hover": "#343b44",
            "border": "#4a535d",
            "text": "#f2f4f7",
            "muted": "#b8c0ca",
            "accent": "#4da3ff",
            "log": "#171a1f",
            "selection": "#245c8f",
        }
        if dark
        else
        {
            "surface": "#ffffff",
            "surface_alt": "#f3f5f7",
            "surface_hover": "#e7ebef",
            "border": "#b7bec6",
            "text": "#18212a",
            "muted": "#56616c",
            "accent": "#0067b8",
            "log": "#fbfcfd",
            "selection": "#cfe8ff",
        }
    )

    devices_workspace.setObjectName("devicesWorkspace")
    devices_workspace.setStyleSheet(
        f"""
        QWidget#devicesWorkspace {{
            background: {colors['surface']};
            color: {colors['text']};
        }}
        QWidget#devicesWorkspace QLabel#workspaceTitle {{
            color: {colors['text']};
            font-size: 17px;
            font-weight: 700;
        }}
        QWidget#devicesWorkspace QFrame#deviceCard {{
            background: {colors['surface']};
            border: 1px solid {colors['border']};
            border-radius: 8px;
        }}
        QWidget#devicesWorkspace QPushButton#deviceCardHeader {{
            color: {colors['text']};
            background: {colors['surface_alt']};
            border: 0;
            border-radius: 7px;
            padding: 9px 10px;
            text-align: left;
            font-weight: 700;
        }}
        QWidget#devicesWorkspace QPushButton#deviceCardHeader:hover {{
            background: {colors['surface_hover']};
        }}
        QWidget#devicesWorkspace QComboBox,
        QWidget#devicesWorkspace QLineEdit {{
            color: {colors['text']};
            background: {colors['surface']};
            border: 1px solid {colors['border']};
            border-radius: 4px;
            padding: 5px 7px;
            selection-background-color: {colors['selection']};
        }}
        QWidget#devicesWorkspace QComboBox::drop-down {{
            width: 24px;
            border-left: 1px solid {colors['border']};
        }}
        QWidget#devicesWorkspace QLabel {{
            color: {colors['text']};
        }}
        QWidget#cardsWidget {{
            background-color: {colors['surface']};
        }}
        QScrollArea {{
            background-color: {colors['surface']};
            border: none;
        }}
        QScrollArea > QWidget > QWidget {{
            background-color: {colors['surface']};
        }}
        QPushButton#deviceCardHeader {{
            min-height: 32px;
        }}
        QFrame#deviceCard {{
            margin: 2px;
        }}
        QWidget#devicesWorkspace QComboBox::drop-down {{
            width: 24px;
            border-left: 1px solid {colors['border']};
        }}
        QWidget#devicesWorkspace QComboBox::down-arrow {{
            image: none;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 7px solid {colors['text']};
            margin-right: 4px;
        }}
        QWidget#devicesWorkspace QComboBox {{
            min-height: 28px;
            padding-right: 24px;
        }}
        """
    )

    console.setObjectName("operationsConsole")
    system_log = getattr(console, "system_log", None)
    if system_log is not None:
        system_log.setObjectName("systemLog")
    recent_events = getattr(console, "recent_events", None)
    if recent_events is not None:
        recent_events.setObjectName("recentEvents")
    operator_events = getattr(console, "operator_events", None)
    if operator_events is not None:
        operator_events.setObjectName("operatorEvents")

    console.setStyleSheet(
        f"""
        QWidget#operationsConsole {{
            background: {colors['surface']};
            color: {colors['text']};
        }}
        QWidget#operationsConsole QFrame#operationsSection {{
            background: {colors['surface']};
            border: 1px solid {colors['border']};
            border-radius: 8px;
        }}
        QWidget#operationsConsole QLabel#operationsSectionTitle {{
            color: {colors['text']};
            font-size: 12px;
            font-weight: 700;
            padding-bottom: 2px;
        }}
        QWidget#operationsConsole QTextEdit#systemLog {{
            color: {colors['text']};
            background: {colors['log']};
            border: 1px solid {colors['border']};
            border-radius: 5px;
            padding: 5px;
            selection-background-color: {colors['selection']};
        }}
        QWidget#operationsConsole QListWidget#recentEvents {{
            color: {colors['text']};
            background: {colors['log']};
            border: 1px solid {colors['border']};
            border-radius: 5px;
            selection-background-color: {colors['selection']};
        }}
        QWidget#operationsConsole QWidget#operatorEvents,
        QWidget#operationsConsole QWidget#operatorEvents QWidget {{
            color: {colors['text']};
            background: transparent;
        }}
        QWidget#operationsConsole QWidget#operatorEvents QLineEdit,
        QWidget#operationsConsole QWidget#operatorEvents QComboBox {{
            color: {colors['text']};
            background: {colors['surface']};
            border: 1px solid {colors['border']};
            border-radius: 4px;
            padding: 4px 6px;
            selection-background-color: {colors['selection']};
        }}
        QWidget#operationsConsole QWidget#operatorEvents QLabel {{
            color: {colors['muted']};
        }}
        """
    )
    return theme
