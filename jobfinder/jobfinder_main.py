"""
jobfinder_main.py - Start the Ausbildung Job Finder.

    python jobfinder_main.py              start the window
    python jobfinder_main.py --self-test  check the installation (no browser window)

Optional environment variables:
    JOBFINDER_HOME      folder for settings, collected jobs and browser profile
    JOBFINDER_BROWSER   path of a Chrome/Chromium/Edge executable to use
    JOBFINDER_CHANNEL   "msedge" or "chrome" (default: Edge, then Chrome)
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def browser_options() -> dict:
    options: dict = {}
    if os.environ.get("JOBFINDER_BROWSER"):
        options["executable_path"] = os.environ["JOBFINDER_BROWSER"]
    if os.environ.get("JOBFINDER_CHANNEL"):
        options["channel"] = os.environ["JOBFINDER_CHANNEL"]
    return options


def self_test() -> int:
    from ba_parser import extract_state, parse_search
    ok = True
    sample = HERE / "tests" / "pages" / "search_koeln_25km.html"
    try:
        result = parse_search(extract_state(sample.read_text(encoding="utf-8")))
        print(f"[OK] Parser: {len(result.jobs)} Elektroniker jobs read from the sample page")
    except Exception as exc:
        ok = False
        print(f"[FAIL] Parser: {exc}")
    try:
        import importlib
        importlib.import_module("playwright.sync_api")
        print("[OK] Playwright library")
    except Exception as exc:
        ok = False
        print(f"[FAIL] Playwright library: {exc}")
    try:
        import tkinter
        print(f"[OK] Tkinter {tkinter.TkVersion}")
    except Exception as exc:
        ok = False
        print(f"[FAIL] Tkinter: {exc}")
    print("All checks passed." if ok else "Some checks FAILED.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    parser = argparse.ArgumentParser(description="Ausbildung Job Finder")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()

    from jobfinder_gui import JobFinderApp, data_dir
    home = data_dir()
    handler = logging.handlers.RotatingFileHandler(home / "jobfinder.log", maxBytes=1_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = JobFinderApp(home, browser_options=browser_options())
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
