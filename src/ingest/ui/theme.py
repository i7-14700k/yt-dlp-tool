"""Design tokens and fonts. See docs/DESIGN.md for the reasoning behind each value."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF

from ..core import paths

# ------------------------------------------------------------------ color
BG = QColor("#000000")
S1 = QColor("#0B0B0B")
S2 = QColor("#141414")
S3 = QColor("#1E1E1E")
S4 = QColor("#2A2A2A")
FG = QColor("#FFFFFF")
FG2 = QColor("#A6A6A6")
FG3 = QColor("#7A7A7A")
FG4 = QColor("#4A4A4A")
INV_FG2 = QColor("#4D4D4D")  # secondary text on a white (selected) fill


def hexa(c: QColor) -> str:
    return c.name(QColor.HexRgb)


# ------------------------------------------------------------------ geometry
R_SM, R_MD, R_LG = 4, 6, 8
CONTROL_H = 32
PANEL_W = 344

# ------------------------------------------------------------------ type
SANS = "Instrument Sans"
MONO = "Fragment Mono"
_families: dict[int, str] = {}
_loaded = False


def load_fonts() -> None:
    global _loaded, SANS, MONO
    if _loaded:
        return
    _loaded = True
    files = {
        400: "InstrumentSans-Regular.ttf",
        500: "InstrumentSans-Medium.ttf",
        600: "InstrumentSans-SemiBold.ttf",
        700: "InstrumentSans-Bold.ttf",
    }
    for weight, name in files.items():
        fid = QFontDatabase.addApplicationFont(str(paths.asset("fonts", name)))
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if not fams:
            continue
        # DirectWrite groups every weight under "Instrument Sans"; GDI-style databases list
        # Medium and SemiBold as their own families. Use whichever really holds the weight.
        styles = QFontDatabase.styles(fams[0])
        wanted = {500: "Medium", 600: "SemiBold"}.get(weight)
        _families[weight] = fams[0] if (wanted is None or wanted in styles) else fams[-1]
    mid = QFontDatabase.addApplicationFont(str(paths.asset("fonts", "FragmentMono-Regular.ttf")))
    mono = QFontDatabase.applicationFontFamilies(mid) if mid >= 0 else []
    if mono:
        MONO = mono[0]
    if 400 not in _families:
        SANS = "Segoe UI"


def font(px: float, weight: int = 400, mono: bool = False, tabular: bool = False) -> QFont:
    family = MONO if mono else _families.get(weight, SANS)
    f = QFont(family)
    f.setPixelSize(max(1, int(round(px))))
    if not mono:
        f.setWeight(QFont.Weight(weight))
    f.setHintingPreference(QFont.PreferNoHinting)
    f.setStyleStrategy(QFont.PreferAntialias)
    if tabular and not mono:
        try:
            f.setFeature(QFont.Tag("tnum"), 1)
        except (AttributeError, TypeError):
            pass
    return f


def palette():
    from PySide6.QtGui import QPalette
    pal = QPalette()
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        pal.setColor(group, QPalette.Window, BG)
        pal.setColor(group, QPalette.WindowText, FG if group != QPalette.Disabled else FG4)
        pal.setColor(group, QPalette.Base, S2)
        pal.setColor(group, QPalette.AlternateBase, S1)
        pal.setColor(group, QPalette.Text, FG if group != QPalette.Disabled else FG4)
        pal.setColor(group, QPalette.Button, S2)
        pal.setColor(group, QPalette.ButtonText, FG)
        pal.setColor(group, QPalette.Highlight, FG)
        pal.setColor(group, QPalette.HighlightedText, BG)
        pal.setColor(group, QPalette.ToolTipBase, FG)
        pal.setColor(group, QPalette.ToolTipText, BG)
        pal.setColor(group, QPalette.PlaceholderText, FG3)
        pal.setColor(group, QPalette.Link, FG)
    return pal


def text_width(f: QFont, text: str) -> float:
    return QFontMetricsF(f).horizontalAdvance(text)


# ------------------------------------------------------------------ global stylesheet
def stylesheet() -> str:
    return f"""
* {{ outline: none; }}
QWidget {{ background: transparent; }}
QMainWindow, #Root {{ background: {hexa(BG)}; }}
QToolTip {{
    background: {hexa(FG)}; color: {hexa(BG)}; border: none; padding: 5px 8px;
    border-radius: {R_SM}px; font-family: "{SANS}"; font-size: 12px;
}}
QScrollArea, QAbstractScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 2px 2px 0; border: none; }}
QScrollBar::handle:vertical {{ background: transparent; min-height: 32px; border-radius: 3px; margin: 0 2px; }}
QScrollBar[active="true"]::handle:vertical {{ background: {hexa(S4)}; }}
QScrollBar::handle:vertical:hover {{ background: #3A3A3A; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0 2px 2px 2px; border: none; }}
QScrollBar::handle:horizontal {{ background: transparent; min-width: 32px; border-radius: 3px; margin: 2px 0; }}
QScrollBar[active="true"]::handle:horizontal {{ background: {hexa(S4)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {hexa(S2)}; color: {hexa(FG)}; border: none; border-radius: {R_MD}px;
    padding: 0 10px; selection-background-color: {hexa(FG)}; selection-color: {hexa(BG)};
    font-family: "{SANS}"; font-size: 13px;
}}
QPlainTextEdit, QTextEdit {{ padding: 8px 10px; }}
QLineEdit:hover, QPlainTextEdit:hover {{ background: {hexa(S3)}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ background: {hexa(S3)}; }}
QLineEdit:disabled {{ color: {hexa(FG4)}; background: {hexa(S1)}; }}
QMenu {{
    background: {hexa(S2)}; border: none; border-radius: {R_LG}px; padding: 6px;
    font-family: "{SANS}"; font-size: 13px;
}}
QMenu::item {{ padding: 7px 28px 7px 12px; border-radius: {R_SM}px; color: {hexa(FG)}; background: transparent; }}
QMenu::item:selected {{ background: {hexa(S3)}; }}
QMenu::item:disabled {{ color: {hexa(FG4)}; }}
QMenu::separator {{ height: 1px; background: {hexa(S3)}; margin: 5px 6px; }}
QMenu::icon {{ padding-left: 8px; }}
QListView, QTableView, QTreeView {{ background: transparent; border: none; }}
QAbstractItemView {{ selection-background-color: transparent; }}
QSplitter::handle {{ background: transparent; }}
"""
