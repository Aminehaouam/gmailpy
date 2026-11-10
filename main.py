"""
main.py - Entry point of Bewerbung Attach Assistant.

Usage:
    python main.py              start the application
    python main.py --dry-run    start with dry-run mode locked on
                                (previews only, Gmail is never modified)
    python main.py --self-test  check the installation without opening the GUI
    python main.py --version    print the version
    python main.py --data-dir D use folder D for settings, token, history, logs
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path


_WINDOWED = False  # True when started without a console (pythonw / built .exe)


def _fix_std_streams() -> None:
    """In a windowed (pythonw / PyInstaller --windowed) process sys.stdout and
    sys.stderr are None; some libraries write to them unconditionally."""
    global _WINDOWED
    if sys.stdout is None:
        _WINDOWED = True
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _show_text(title: str, text: str, error: bool = False) -> None:
    """Print text; in a windowed process (no console) show a message box."""
    print(text)
    if _WINDOWED:
        try:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            (messagebox.showerror if error else messagebox.showinfo)(title, text)
            root.destroy()
        except Exception:
            pass


def _enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BewerbungAttachAssistant")
    except Exception:
        pass


class SingleInstanceLock:
    """Prevents two copies of the app from processing PDFs at the same time."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        handle = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._handle.close()
            self._handle = None


def self_test() -> int:
    """Verify that all components work, without network access or GUI."""
    from config import APP_NAME, APP_VERSION, AppPaths, Config, DEFAULTS
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn() or ""
            results.append((name, True, str(detail)))
        except Exception as exc:  # report every failure
            results.append((name, False, f"{type(exc).__name__}: {exc}"))

    def gmail_api() -> str:
        import httplib2
        from googleapiclient.discovery import build_from_document
        from googleapiclient.discovery_cache import get_static_doc
        doc = get_static_doc("gmail", "v1")
        if not doc:
            raise RuntimeError("bundled Gmail discovery document missing")
        service = build_from_document(doc, http=httplib2.Http())
        service.users().drafts().update  # noqa: B018 - attribute must exist
        return "Gmail API client OK"

    def oauth() -> str:
        import importlib
        importlib.import_module("google_auth_oauthlib.flow")
        return "OAuth library OK"

    def database() -> str:
        from database import Database
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            rid = db.start_operation(pdf_filename="t.pdf", pdf_path="t.pdf", sha256="0" * 64, file_size=1,
                                     file_mtime=0.0, company_name="T", draft_id=None, draft_message_id=None,
                                     draft_subject=None, draft_recipients=None)
            db.finish_operation(rid, "failed", error_message="self-test")
        return "SQLite OK"

    def storage() -> str:
        from secure_storage import TokenStore
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenStore(Path(tmp) / "token.bin")
            store.save('{"self": "test"}')
            if store.load() != '{"self": "test"}':
                raise RuntimeError("token round trip failed")
            return "DPAPI encryption OK" if store.encrypted else "token file storage OK (no DPAPI on this OS)"

    def gui_toolkit() -> str:
        import tkinter
        return f"Tk {tkinter.TkVersion}"

    def mime() -> str:
        from attachment_manager import build_message_with_attachment, verify_preservation
        raw = (b"MIME-Version: 1.0\r\nTo: a@example.com\r\nSubject: Test\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\nHallo\r\n")
        pdf = b"%PDF-1.4\n%%EOF\n"
        new = build_message_with_attachment(raw, pdf, "test.pdf")
        verify_preservation(raw, new, pdf, "test.pdf")
        return "MIME builder OK"

    def parser() -> str:
        from filename_parser import FilenameParser
        result = FilenameParser(DEFAULTS["filename_parser"]).parse(
            "Amine_Haouam_Ausbildung_Elektroniker_Barth_und_Kerner_GmbH.pdf")
        if result.company != "Barth und Kerner GmbH":
            raise RuntimeError(f"unexpected company {result.company!r}")
        return "file name parser OK"

    for name, fn in (("Gmail API", gmail_api), ("OAuth", oauth), ("Database", database),
                     ("Token storage", storage), ("Tkinter", gui_toolkit), ("MIME", mime),
                     ("Parser", parser)):
        check(name, fn)

    paths = AppPaths.default()
    config = Config.load(paths)
    lines = [
        f"{APP_NAME} {APP_VERSION} self-test",
        f"Data folder:        {paths.root}",
        f"Bewerbungen folder: {config.watch_folder}",
        f"OAuth client file:  {'present' if paths.client_secret_file.exists() else 'not configured yet'}",
    ]
    ok = True
    for name, passed, detail in results:
        ok &= passed
        lines.append(f"  [{'OK' if passed else 'FAIL'}] {name}: {detail}")
    lines.append("All checks passed." if ok else "Some checks FAILED.")
    _show_text(f"{APP_NAME} self-test", "\n".join(lines), error=not ok)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _fix_std_streams()
    parser = argparse.ArgumentParser(description="Bewerbung Attach Assistant")
    parser.add_argument("--dry-run", action="store_true",
                        help="lock dry-run mode on: preview only, Gmail is never modified")
    parser.add_argument("--self-test", action="store_true", help="check the installation and exit")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--data-dir", help="folder for settings, token, history and logs")
    parser.add_argument("--minimized", action="store_true", help="start with the window minimized")
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ["BEWERBUNG_ASSISTANT_HOME"] = str(Path(args.data_dir).expanduser().resolve())

    from config import APP_NAME, APP_VERSION, AppPaths, Config

    if args.version:
        _show_text(APP_NAME, f"{APP_NAME} {APP_VERSION}")
        return 0
    if args.self_test:
        return self_test()

    paths = AppPaths.default()
    from logging_setup import setup_logging
    log_file = setup_logging(paths.log_dir)
    log = logging.getLogger("main")
    log.info("=== %s %s starting (dry-run locked: %s) ===", APP_NAME, APP_VERSION, args.dry_run)

    _enable_windows_dpi_awareness()

    try:
        import tkinter  # noqa: F401
        from tkinter import messagebox
    except ImportError:
        print("Tkinter is not available. Install Python from python.org with the 'tcl/tk' option.")
        return 1

    lock = SingleInstanceLock(paths.lock_file)
    if not lock.acquire():
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showwarning(APP_NAME, f"{APP_NAME} is already running.")
        root.destroy()
        return 1

    try:
        from database import Database
        from gui import App

        config = Config.load(paths)
        db = Database(paths.database_file)
        app = App(config, db, force_dry_run=args.dry_run)
        if args.minimized:
            app.iconify()
        log.info("Log file: %s", log_file)
        app.mainloop()
        # Release Tk objects on the main thread (Tcl must not be torn down by
        # the garbage collector running on another thread).
        del app
        import gc
        gc.collect()
    except Exception:
        log.exception("Fatal error")
        try:
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror(APP_NAME, f"A fatal error occurred. Details are in the log file:\n{log_file}")
            root.destroy()
        except Exception:
            pass
        return 1
    finally:
        lock.release()
        log.info("=== %s stopped ===", APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
