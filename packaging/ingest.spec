# PyInstaller spec for the Windows build.  Run from the repo root:
#     pyinstaller --noconfirm packaging/ingest.spec
# Produces dist/Ingest/Ingest.exe (one-folder build: fast start, no temp unpacking).

import re
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller
version = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/ingest/__init__.py").read_text()).group(1)
parts = (version.split(".") + ["0", "0", "0"])[:4]

(ROOT / "packaging" / "version_info.txt").write_text(f"""
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({', '.join(parts)}), prodvers=({', '.join(parts)}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Ingest'),
      StringStruct('FileDescription', 'Ingest - downloads ready for Premiere Pro and After Effects'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'Ingest'),
      StringStruct('OriginalFilename', 'Ingest.exe'),
      StringStruct('ProductName', 'Ingest'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")

EXCLUDES = [
    "tkinter", "unittest", "pydoc_data", "test", "lib2to3",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtSvg", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtXml",
    "PySide6.QtConcurrent", "PySide6.QtDBus",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT / "src")],
    datas=[(str(ROOT / "assets" / "fonts"), "assets/fonts"),
           (str(ROOT / "plugins"), "plugins"),
           (str(ROOT / "assets" / "icon.ico"), "assets"),
           (str(ROOT / "assets" / "icon.png"), "assets"),
           (str(ROOT / "assets" / "fonts" / "OFL.txt"), "licenses")],
    hiddenimports=["PySide6.QtNetwork"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Ingest",
    icon=str(ROOT / "assets" / "icon.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Ingest", upx=False)  # noqa: F821
