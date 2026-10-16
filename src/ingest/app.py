"""Application entry point."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import traceback
from pathlib import Path

from . import APP_NAME, APP_USER_MODEL_ID, __version__


def _server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return f"{APP_NAME}-{user}-v1".replace(" ", "_")


def _send_to_running(urls: list[str], name: str | None = None) -> bool:
    from PySide6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(name or _server_name())
    if not sock.waitForConnected(1000):
        return False
    sock.write(json.dumps({"urls": urls, "show": True}).encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(2000)
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.UnconnectedState:
        sock.waitForDisconnected(1000)
    return True


def claim_instance(app, name: str | None = None):
    """Become the one running Ingest, or return None if another one is already up.

    Called before the window is built: building it takes a moment, and a second
    launch in that gap must find this instance listening instead of starting a
    second Ingest on the same queue and library.
    """
    from PySide6.QtNetwork import QLocalServer
    name = name or _server_name()
    server = QLocalServer(app)
    if server.listen(name):
        return server
    # Either another instance is listening, or a crashed one left its socket behind.
    if _send_to_running([], name):
        return None
    QLocalServer.removeServer(name)
    server.listen(name)  # if even this fails, run anyway: one window beats none
    return server


def serve_instance(server, on_message) -> None:
    """Hand each message from a later launch (its links) to on_message, however it arrives."""
    def incoming():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            state = {"buf": bytearray(), "done": False}

            def take(conn=conn, state=state):
                if state["done"]:
                    return
                state["buf"].extend(bytes(conn.readAll()))
                try:
                    data = json.loads(state["buf"].decode("utf-8") or "null")
                except ValueError:
                    return  # the rest is still on its way
                if data is None:
                    return
                state["done"] = True
                on_message(data if isinstance(data, dict) else {})
                conn.disconnectFromServer()
            conn.readyRead.connect(take)
            conn.disconnected.connect(take)
            take()  # whatever arrived before the signals were connected
    server.newConnection.connect(incoming)


def _install_excepthook(log_dir: Path) -> None:
    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "crash.log", "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass
        if sys.stderr is not None:  # None in a windowed (no console) build
            sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="Ingest", add_help=True)
    ap.add_argument("urls", nargs="*", help="links to queue")
    ap.add_argument("--screenshot", metavar="DIR", help="render every page to PNG files and exit")
    ap.add_argument("--selftest", action="store_true", help="check engines and rendering, then exit")
    ap.add_argument("--e2e", metavar="URL", help="install engines if needed, download URL headless, verify, exit")
    ap.add_argument("--profile", default="", help="output profile for --e2e (h264, prores, dnxhr, hevc, wav)")
    ap.add_argument("--report", metavar="FILE", help="write the --selftest/--screenshot/--e2e result as JSON")
    ap.add_argument("--version", action="version", version=f"Ingest {__version__}")
    args, _unknown = ap.parse_known_args(argv)

    from .core import paths, winapi
    winapi.set_app_user_model_id(APP_USER_MODEL_ID)
    _install_excepthook(paths.logs_dir())
    if args.e2e:
        from .e2e import run_e2e
        return run_e2e(args.e2e, args.profile, args.report)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    app.setStyle("Fusion")
    from .core.util import expand_protocol_links, extract_urls

    urls = extract_urls(" ".join(expand_protocol_links(args.urls)))
    headless = bool(args.screenshot or args.selftest)
    server = None
    if not headless:
        if _send_to_running(urls):
            return 0
        server = claim_instance(app)
        if server is None:  # lost a race with another launch; it has the window
            _send_to_running(urls)
            return 0

    from .ui import theme
    theme.load_fonts()
    app.setPalette(theme.palette())
    app.setFont(theme.font(13))
    app.setStyleSheet(theme.stylesheet())

    from .core.engines import Engines
    from .core.library import Library
    from .core.settings import SettingsStore
    from .ui.main_window import MainWindow, app_icon

    store = SettingsStore()
    engines = Engines(lambda: store.settings)
    library = Library()
    icon = app_icon()
    app.setWindowIcon(icon)

    if args.screenshot or args.selftest:
        from .ui.screens import run_headless
        return run_headless(app, store, engines, library, args.screenshot, args.selftest, args.report)

    win = MainWindow(store, engines, library)
    win.setWindowIcon(icon)
    if paths.is_frozen() and store.settings.browser_links:
        winapi.register_protocol(sys.executable)

    def from_other_launch(data: dict) -> None:
        win.showNormal()
        win.raise_()
        win.activateWindow()
        if data.get("urls"):
            win.show_page("queue")
            win._enqueue([str(u) for u in data["urls"]])
    if server is not None:
        serve_instance(server, from_other_launch)

    win.show()
    if urls:
        win._enqueue(urls)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    sys.exit(main())
