"""
Theme stylesheets matching OBS Studio design conventions.
Dark theme closely mirrors OBS default, light theme is a clean alternative.
"""

# OBS-style color palette
COLORS_DARK = {
    "bg_window": "#1e1e1e",
    "bg_panel": "#272727",
    "bg_input": "#1a1a1a",
    "bg_group": "#2d2d2d",
    "bg_hover": "#353535",
    "bg_pressed": "#404040",
    "bg_selected": "#1a5fb4",
    "border": "#3c3c3c",
    "border_focus": "#4a9eff",
    "text": "#d4d4d4",
    "text_dim": "#808080",
    "text_bright": "#ffffff",
    "accent": "#4a9eff",
    "accent_hover": "#5aafff",
    "success": "#3ec45c",
    "warning": "#e8a83c",
    "danger": "#e04040",
    "graph_bg": "#1a1a1a",
    "graph_grid": "#333333",
    "graph_bitrate": "#4a9eff",
    "graph_average": "#e8a83c",
    "graph_low": "#e8a83c",
    "graph_disc": "#e04040",
    "tab_active": "#2d2d2d",
    "tab_inactive": "#222222",
    "scrollbar": "#444444",
    "scrollbar_bg": "#2a2a2a",
}

COLORS_LIGHT = {
    "bg_window": "#f0f0f0",
    "bg_panel": "#ffffff",
    "bg_input": "#ffffff",
    "bg_group": "#f8f8f8",
    "bg_hover": "#e8e8e8",
    "bg_pressed": "#d0d0d0",
    "bg_selected": "#1a5fb4",
    "border": "#cccccc",
    "border_focus": "#4a9eff",
    "text": "#2d2d2d",
    "text_dim": "#888888",
    "text_bright": "#000000",
    "accent": "#1a5fb4",
    "accent_hover": "#2a6fd4",
    "success": "#2ea44f",
    "warning": "#d18616",
    "danger": "#cf222e",
    "graph_bg": "#ffffff",
    "graph_grid": "#e0e0e0",
    "graph_bitrate": "#1a5fb4",
    "graph_average": "#d18616",
    "graph_low": "#d18616",
    "graph_disc": "#cf222e",
    "tab_active": "#ffffff",
    "tab_inactive": "#e8e8e8",
    "scrollbar": "#bbbbbb",
    "scrollbar_bg": "#e8e8e8",
}


def build_stylesheet(c: dict) -> str:
    return f"""
    /* === Global === */
    QWidget {{
        background-color: {c['bg_window']};
        color: {c['text']};
        font-family: "Segoe UI", "SF Pro Display", "Helvetica Neue", sans-serif;
        font-size: 13px;
    }}

    /* === Main Window === */
    QMainWindow {{
        background-color: {c['bg_window']};
    }}

    /* === Tab Widget (OBS-style) === */
    QTabWidget::pane {{
        border: 1px solid {c['border']};
        background-color: {c['bg_panel']};
        border-radius: 4px;
        margin-top: -1px;
    }}
    QTabBar::tab {{
        background-color: {c['tab_inactive']};
        color: {c['text_dim']};
        border: 1px solid {c['border']};
        border-bottom: none;
        padding: 8px 20px;
        margin-right: 2px;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
    }}
    QTabBar::tab:selected {{
        background-color: {c['tab_active']};
        color: {c['text_bright']};
        border-bottom: 2px solid {c['accent']};
    }}
    QTabBar::tab:hover:!selected {{
        background-color: {c['bg_hover']};
        color: {c['text']};
    }}

    /* === Group Box (OBS panel style) === */
    QGroupBox {{
        background-color: {c['bg_group']};
        border: 1px solid {c['border']};
        border-radius: 6px;
        margin-top: 14px;
        padding: 16px 12px 12px 12px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 12px;
        padding: 0 6px;
        color: {c['text']};
    }}

    /* === Inputs === */
    QLineEdit, QSpinBox, QDoubleSpinBox {{
        background-color: {c['bg_input']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 4px;
        padding: 6px 8px;
        selection-background-color: {c['bg_selected']};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {c['border_focus']};
    }}

    QComboBox {{
        background-color: {c['bg_input']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 4px;
        padding: 6px 28px 6px 8px;
        min-height: 20px;
    }}
    QComboBox:focus {{
        border-color: {c['border_focus']};
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 24px;
        border-left: 1px solid {c['border']};
        border-top-right-radius: 4px;
        border-bottom-right-radius: 4px;
        background-color: {c['bg_group']};
    }}
    QComboBox::down-arrow {{
        width: 10px;
        height: 10px;
        image: none;
        border-left: 3px solid transparent;
        border-right: 3px solid transparent;
        border-top: 5px solid {c['text']};
        margin-top: 2px;
    }}
    QComboBox::down-arrow:on {{
        border-top: none;
        border-bottom: 5px solid {c['text']};
    }}
    QComboBox QAbstractItemView {{
        background-color: {c['bg_panel']};
        color: {c['text']};
        border: 1px solid {c['border']};
        selection-background-color: {c['bg_selected']};
        outline: none;
    }}
    QComboBox QAbstractItemView::item {{
        padding: 4px 8px;
        min-height: 22px;
    }}
    QComboBox QAbstractItemView::item:selected {{
        background-color: {c['bg_selected']};
        color: #ffffff;
    }}

    /* === Buttons (OBS-style flat) === */
    QPushButton {{
        background-color: {c['bg_group']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 4px;
        padding: 7px 18px;
        font-weight: 500;
    }}
    QPushButton:hover {{
        background-color: {c['bg_hover']};
        border-color: {c['border_focus']};
    }}
    QPushButton:pressed {{
        background-color: {c['bg_pressed']};
    }}
    QPushButton:disabled {{
        color: {c['text_dim']};
        background-color: {c['bg_window']};
        border-color: {c['border']};
    }}
    QPushButton[accent="true"] {{
        background-color: {c['accent']};
        color: #ffffff;
        border: none;
    }}
    QPushButton[accent="true"]:hover {{
        background-color: {c['accent_hover']};
    }}
    QPushButton[danger="true"] {{
        background-color: {c['danger']};
        color: #ffffff;
        border: none;
    }}

    /* === Checkbox & Radio === */
    QCheckBox {{
        spacing: 8px;
        color: {c['text']};
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {c['border']};
        border-radius: 3px;
        background-color: {c['bg_input']};
    }}
    QCheckBox::indicator:checked {{
        background-color: {c['accent']};
        border-color: {c['accent']};
    }}

    /* === Labels === */
    QLabel {{
        color: {c['text']};
        background-color: transparent;
    }}
    QLabel[heading="true"] {{
        font-size: 16px;
        font-weight: 700;
        color: {c['text_bright']};
    }}
    QLabel[dim="true"] {{
        color: {c['text_dim']};
        font-size: 11px;
    }}
    QLabel[status_good="true"] {{
        color: {c['success']};
    }}
    QLabel[status_warn="true"] {{
        color: {c['warning']};
    }}
    QLabel[status_bad="true"] {{
        color: {c['danger']};
    }}

    /* === Scroll Area === */
    QScrollArea {{
        border: none;
        background-color: transparent;
    }}
    QScrollBar:vertical {{
        background-color: {c['scrollbar_bg']};
        width: 10px;
        margin: 0;
        border-radius: 5px;
    }}
    QScrollBar::handle:vertical {{
        background-color: {c['scrollbar']};
        border-radius: 5px;
        min-height: 30px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}

    /* === Text Edit / Log === */
    QTextEdit, QPlainTextEdit {{
        background-color: {c['bg_input']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 4px;
        padding: 6px;
        font-family: "Consolas", "SF Mono", "Fira Code", monospace;
        font-size: 12px;
    }}

    /* === List Widget === */
    QListWidget {{
        background-color: {c['bg_input']};
        color: {c['text']};
        border: 1px solid {c['border']};
        border-radius: 4px;
        padding: 4px;
    }}
    QListWidget::item {{
        padding: 6px;
        border-radius: 3px;
    }}
    QListWidget::item:selected {{
        background-color: {c['bg_selected']};
        color: #ffffff;
    }}
    QListWidget::item:hover {{
        background-color: {c['bg_hover']};
    }}

    /* === Splitter === */
    QSplitter::handle {{
        background-color: {c['border']};
    }}

    /* === Status Bar === */
    QStatusBar {{
        background-color: {c['bg_panel']};
        border-top: 1px solid {c['border']};
        color: {c['text_dim']};
        font-size: 11px;
    }}

    /* === Menu Bar === */
    QMenuBar {{
        background-color: {c['bg_panel']};
        border-bottom: 1px solid {c['border']};
        color: {c['text']};
    }}
    QMenuBar::item:selected {{
        background-color: {c['bg_hover']};
    }}
    QMenu {{
        background-color: {c['bg_panel']};
        border: 1px solid {c['border']};
        color: {c['text']};
        padding: 4px 0;
    }}
    QMenu::item {{
        padding: 6px 24px;
    }}
    QMenu::item:selected {{
        background-color: {c['bg_selected']};
        color: #ffffff;
    }}
    QMenu::separator {{
        height: 1px;
        background-color: {c['border']};
        margin: 4px 0;
    }}

    /* === Slider === */
    QSlider::groove:horizontal {{
        height: 4px;
        background-color: {c['border']};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background-color: {c['accent']};
        width: 14px;
        height: 14px;
        margin: -5px 0;
        border-radius: 7px;
    }}

    /* === ToolTip === */
    QToolTip {{
        background-color: {c['bg_panel']};
        color: {c['text']};
        border: 1px solid {c['border']};
        padding: 4px 8px;
        border-radius: 4px;
    }}
    """


DARK_STYLESHEET = build_stylesheet(COLORS_DARK)
LIGHT_STYLESHEET = build_stylesheet(COLORS_LIGHT)
