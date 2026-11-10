"""
gui.py - Tkinter/ttk desktop interface of Bewerbung Attach Assistant.

Threading model: all Gmail and file operations run on ONE background worker
thread (so Gmail calls are serialised and the window never freezes).
Results are handed back to the Tk main thread through a queue that is polled
with `after()`.  Tk widgets are only touched on the main thread.

Every modal question goes through an overridable `ask_*` / `show_*` method,
which keeps the workflow testable.
"""

from __future__ import annotations

import gc
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from attachment_manager import (
    AttachError,
    AttachmentManager,
    AttachPlan,
    AttachResult,
    DraftChangedError,
    DuplicateAttachmentError,
    LocalFileChangedError,
)
from config import APP_NAME, APP_VERSION, Config
from database import (
    SEND_FAILED,
    SEND_SENT,
    Database,
    HistoryRecord,
    INCOMPLETE_STATUSES,
    STATUS_ALREADY_ATTACHED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    STATUS_UNVERIFIED,
)
from file_scanner import FileScanner, PdfFile, ScanError, ScanResult
from filename_parser import FilenameParser, ParseResult
from gmail_client import (
    CredentialsFileError,
    DraftInfo,
    GmailAuthError,
    GmailClient,
    GmailError,
    human_size,
    install_client_secret_file,
    revoke_token,
)
from logging_setup import SecretRedactingFilter
from matching_engine import (
    MULTIPLE,
    NO_MATCH,
    POSSIBLE,
    SINGLE_POSSIBLE,
    SINGLE_STRONG,
    STRONG,
    WEAK,
    WEAK_ONLY,
    DraftMatch,
    MatchingEngine,
    MatchOutcome,
)
from secure_storage import TokenStore

log = logging.getLogger(__name__)

COLORS = {
    "bg": "#f3f5f8",
    "card": "#ffffff",
    "border": "#d8dee8",
    "text": "#1f2933",
    "muted": "#5f6b7a",
    "header": "#1e3a5f",
    "header_text": "#ffffff",
    "header_sub": "#c7d4e6",
    "accent": "#2563eb",
    "accent_active": "#1d4ed8",
    "success": "#15803d",
    "success_active": "#166534",
    "danger": "#b91c1c",
    "disabled_bg": "#e5e9f0",
    "disabled_fg": "#98a2b3",
    "ok_bg": "#e6f4ea", "ok_fg": "#14532d",
    "warn_bg": "#fff3dc", "warn_fg": "#7a4500",
    "stop_bg": "#fde8e8", "stop_fg": "#7f1d1d",
    "info_bg": "#e8f0fe", "info_fg": "#1e3a8a",
    "neutral_bg": "#eef1f5", "neutral_fg": "#3e4a59",
}

BANNER_ICONS = {"ok": "✔", "warn": "⚠", "stop": "✖", "info": "ℹ", "neutral": "•"}


# --------------------------------------------------------------------------
# Background worker
# --------------------------------------------------------------------------

class Worker:
    """Runs callables one at a time on a single background thread; results
    come back to the Tk thread via poll().

    The thread is a daemon thread: if the window is closed while a task is
    still waiting (e.g. for the browser sign-in), the process can exit
    immediately.  Interrupted Gmail updates are detected via the write-ahead
    history record at the next start."""

    def __init__(self) -> None:
        self._tasks: "queue.Queue[tuple | None]" = queue.Queue()
        self._results: "queue.Queue[tuple]" = queue.Queue()
        self.pending = 0
        self._thread = threading.Thread(target=self._run, name="bewerbung-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._tasks.get()
            if item is None:
                return
            fn, on_done = item
            future: Future = Future()
            try:
                future.set_result(fn())
            except BaseException as exc:  # handed to the Tk thread
                future.set_exception(exc)
            self._results.put(("done", on_done, future))
            del item, fn, on_done, future

    def submit(self, fn: Callable[[], Any], on_done: Callable[[Future], None]) -> None:
        self.pending += 1
        self._tasks.put((fn, on_done))

    def post(self, callback: Callable[..., None], *args: Any) -> None:
        """Thread-safe: schedule callback(*args) on the Tk thread."""
        self._results.put(("call", callback, args))

    def poll(self) -> None:
        while True:
            try:
                kind, callback, payload = self._results.get_nowait()
            except queue.Empty:
                return
            if kind == "done":
                self.pending -= 1
                callback(payload)
            else:
                callback(*payload)

    def shutdown(self) -> None:
        self._tasks.put(None)


class QueueLogHandler(logging.Handler):
    """Forwards (redacted) log lines to the activity log tab."""

    def __init__(self, sink: "queue.Queue[str]"):
        super().__init__(level=logging.INFO)
        self.sink = sink
        self.addFilter(SecretRedactingFilter())
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put_nowait(self.format(record))
        except Exception:  # pragma: no cover
            pass


def open_in_file_manager(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        messagebox.showerror(APP_NAME, f"Could not open {path}:\n{exc}")


# --------------------------------------------------------------------------
# Styling
# --------------------------------------------------------------------------

def _setup_style(root: tk.Tk) -> dict[str, str]:
    import tkinter.font as tkfont

    family = "Segoe UI" if sys.platform == "win32" else ("Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")
    if family not in tkfont.families(root):
        family = tkfont.nametofont("TkDefaultFont").actual("family")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        try:
            tkfont.nametofont(name).configure(family=family, size=10)
        except tk.TclError:
            pass
    fonts = {
        "family": family,
        "title": (family, 17, "bold"),
        "subtitle": (family, 10),
        "section": (family, 11, "bold"),
        "bold": (family, 10, "bold"),
        "normal": (family, 10),
        "small": (family, 9),
        "mono": ("Consolas" if sys.platform == "win32" else "DejaVu Sans Mono", 9),
    }

    style = ttk.Style(root)
    style.theme_use("clam")
    c = COLORS
    root.configure(background=c["bg"])
    style.configure(".", background=c["bg"], foreground=c["text"], font=fonts["normal"])
    style.configure("TFrame", background=c["bg"])
    style.configure("Card.TFrame", background=c["card"])
    style.configure("TLabel", background=c["bg"], foreground=c["text"])
    style.configure("Card.TLabel", background=c["card"], foreground=c["text"])
    style.configure("CardMuted.TLabel", background=c["card"], foreground=c["muted"], font=fonts["small"])
    style.configure("CardKey.TLabel", background=c["card"], foreground=c["muted"], font=fonts["normal"])
    style.configure("CardValue.TLabel", background=c["card"], foreground=c["text"], font=fonts["bold"])
    style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"], font=fonts["small"])
    style.configure("Header.TFrame", background=c["header"])
    style.configure("HeaderTitle.TLabel", background=c["header"], foreground=c["header_text"], font=fonts["title"])
    style.configure("HeaderSub.TLabel", background=c["header"], foreground=c["header_sub"], font=fonts["subtitle"])
    style.configure("PillOn.TLabel", background="#dcfce7", foreground="#14532d", font=fonts["bold"], padding=(10, 4))
    style.configure("PillOff.TLabel", background="#e5e7eb", foreground="#374151", font=fonts["bold"], padding=(10, 4))
    style.configure("PillWarn.TLabel", background="#fef3c7", foreground="#78350f", font=fonts["bold"], padding=(10, 4))
    style.configure("PillDry.TLabel", background="#fde68a", foreground="#713f12", font=fonts["bold"], padding=(10, 4))
    style.configure("PillLive.TLabel", background="#fecaca", foreground="#7f1d1d", font=fonts["bold"], padding=(10, 4))

    style.configure("Card.TLabelframe", background=c["card"], bordercolor=c["border"], relief="solid",
                    borderwidth=1, padding=(14, 10))
    style.configure("Card.TLabelframe.Label", background=c["card"], foreground=c["header"], font=fonts["section"])
    style.configure("Card.TCheckbutton", background=c["card"], foreground=c["text"])
    style.map("Card.TCheckbutton", background=[("active", c["card"])])

    style.configure("TButton", padding=(12, 6), background="#ffffff", foreground=c["text"],
                    bordercolor=c["border"], focuscolor=c["accent"], lightcolor="#ffffff", darkcolor="#ffffff")
    style.map("TButton",
              background=[("disabled", c["disabled_bg"]), ("pressed", "#e2e8f0"), ("active", "#eef2f7")],
              foreground=[("disabled", c["disabled_fg"])])
    for name, base, active in (("Accent", c["accent"], c["accent_active"]),
                               ("Success", c["success"], c["success_active"]),
                               ("Danger", c["danger"], "#991b1b")):
        style.configure(f"{name}.TButton", background=base, foreground="#ffffff", bordercolor=base,
                        lightcolor=base, darkcolor=base, font=fonts["bold"])
        style.map(f"{name}.TButton",
                  background=[("disabled", c["disabled_bg"]), ("pressed", active), ("active", active)],
                  foreground=[("disabled", c["disabled_fg"])],
                  bordercolor=[("disabled", c["border"])])

    for kind in ("ok", "warn", "stop", "info", "neutral"):
        style.configure(f"Banner{kind.title()}.TLabel", background=c[f"{kind}_bg"], foreground=c[f"{kind}_fg"],
                        font=fonts["bold"], padding=(12, 9))

    style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=c["text"],
                    rowheight=26, bordercolor=c["border"])
    style.configure("Treeview.Heading", background="#eef1f5", foreground=c["text"], font=fonts["bold"],
                    relief="flat", padding=(6, 4))
    style.map("Treeview", background=[("selected", "#dbe7fd")], foreground=[("selected", c["text"])])
    style.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=(12, 8, 12, 0))
    style.configure("TNotebook.Tab", padding=(16, 7), font=fonts["bold"], background="#e3e8ef",
                    foreground=c["muted"])
    style.map("TNotebook.Tab", background=[("selected", c["card"])], foreground=[("selected", c["header"])])
    style.configure("Status.TFrame", background="#e9edf2")
    style.configure("Status.TLabel", background="#e9edf2", foreground=c["muted"], font=fonts["small"])
    style.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor="#d9e1ec",
                    bordercolor="#d9e1ec", lightcolor=c["accent"], darkcolor=c["accent"])
    return fonts


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------

class _ModalDialog(tk.Toplevel):
    """Base class for modal dialogs centred over the main window."""

    def __init__(self, parent: tk.Misc, title: str):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.configure(background=COLORS["card"])
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.result: Any = None
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _e: self.cancel())

    def show(self) -> Any:
        self.update_idletasks()
        parent = self.master.winfo_toplevel()
        width = max(self.winfo_reqwidth(), 420)
        height = self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 3)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_force()
        self.wait_window(self)
        # Collect the dialog's Tk variables now, on the Tk thread.  (If the
        # cyclic garbage collector ran later on the worker thread, Tkinter
        # would try to call Tcl from the wrong thread.)
        gc.collect()
        return self.result

    def cancel(self) -> None:
        self.result = None
        self.destroy()


class ConfirmAttachDialog(_ModalDialog):
    """Final confirmation listing everything that will happen."""

    def __init__(self, parent: tk.Misc, plan: AttachPlan, match: DraftMatch | None,
                 dry_run: bool, manual: bool, fonts: dict, send: bool = False):
        super().__init__(parent, "Dry run - preview only" if dry_run else
                         ("Confirm attachment and sending" if send else "Confirm attachment"))
        frame = ttk.Frame(self, style="Card.TFrame", padding=20)
        frame.pack(fill="both", expand=True)

        if dry_run:
            heading = "Dry run: check what WOULD happen (Gmail is not modified)"
        elif send:
            heading = "Attach this PDF and SEND the e-mail now?"
        else:
            heading = "Attach this PDF to the Gmail draft?"
        ttk.Label(frame, text=heading, style="Card.TLabel", font=fonts["section"]).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        draft = plan.draft
        existing = (", ".join(a.describe() for a in plan.existing_attachments)
                    if plan.existing_attachments else "(none)")
        rows = [
            ("PDF file", f"{plan.pdf.name}  ({human_size(plan.pdf.size)})"),
            ("Extracted company", plan.company),
            ("Gmail recipient", draft.recipients_display()),
            ("Gmail subject", draft.subject or "(no subject)"),
            ("Draft last saved", draft.date_display),
            ("Existing attachments", existing),
            ("New attachment", f"{plan.pdf.name}  ({human_size(plan.pdf.size)}, application/pdf)"),
        ]
        if match is not None:
            rows.append(("Why this draft", f"{match.strength_label}: {match.evidence_summary()}"))
        for index, (key, value) in enumerate(rows, start=1):
            ttk.Label(frame, text=key + ":", style="CardKey.TLabel").grid(
                row=index, column=0, sticky="nw", padx=(0, 14), pady=3)
            ttk.Label(frame, text=value, style="CardValue.TLabel", wraplength=560, justify="left").grid(
                row=index, column=1, sticky="nw", pady=3)

        if send and not dry_run:
            notes = ["The PDF is added and then the e-mail is SENT immediately to the recipient above.",
                     "A sent e-mail cannot be recalled."]
        elif send:
            notes = ["Dry run: nothing is attached and nothing is sent."]
        else:
            notes = ["Only the attachment is added. The e-mail will NOT be sent."]
        notes.append("The text, formatting and existing attachments of the draft stay unchanged.")
        if not dry_run:
            notes.append("Tip: close this draft in Gmail (browser/app) first, otherwise Gmail's "
                         "autosave could overwrite the change.")
        notes += [f"Note: {w}" for w in plan.warnings]
        banner_style = "BannerInfo.TLabel"
        if manual or (match is not None and match.strength != STRONG):
            banner_style = "BannerWarn.TLabel"
            notes.insert(0, "WARNING: this draft was not identified by a strong match. Make sure "
                            "recipient and subject belong to this company.")
        ttk.Label(frame, text="\n".join(notes), style=banner_style, wraplength=700, justify="left").grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="ew", pady=(14, 10))

        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Go back", command=self.cancel).pack(side="right")
        confirm_text = ("Run dry run (no changes)" if dry_run else
                        ("Attach PDF and send e-mail" if send else "Attach PDF to draft"))
        confirm = ttk.Button(buttons, text=confirm_text, style="Success.TButton", command=self._confirm)
        confirm.pack(side="right", padx=(0, 8))
        frame.columnconfigure(1, weight=1)
        confirm.focus_set()

    def _confirm(self) -> None:
        self.result = True
        self.destroy()


class DraftPickerDialog(_ModalDialog):
    """Lists all drafts (ranked by evidence) for manual selection."""

    def __init__(self, parent: tk.Misc, matches: list[DraftMatch], rejected: set[str], company: str):
        super().__init__(parent, "Select a Gmail draft")
        self.geometry("980x480")
        frame = ttk.Frame(self, style="Card.TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame, style="Card.TLabel", wraplength=920, justify="left",
            text=(f"All drafts, best matches for '{company}' first. Drafts without a strong match "
                  "need an extra confirmation. The newest draft is never chosen automatically."),
        ).pack(anchor="w", pady=(0, 10))
        columns = ("match", "subject", "recipient", "date", "attachments")
        tree_frame = ttk.Frame(frame, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for col, title, width, stretch in (("match", "Match", 130, False), ("subject", "Subject", 290, True),
                                           ("recipient", "Recipient", 240, True),
                                           ("date", "Last saved", 140, False),
                                           ("attachments", "Attachments", 110, False)):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=width, minwidth=70, anchor="w", stretch=stretch)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._items: dict[str, DraftMatch] = {}
        for match in matches:
            draft = match.draft
            label = match.strength_label + (" (rejected)" if draft.draft_id in rejected else "")
            item = self.tree.insert("", "end", values=(
                label, draft.subject or "(no subject)", draft.primary_recipient(),
                draft.date_display, f"{len(draft.attachments)} file(s)" if draft.attachments else "-",
            ))
            self._items[item] = match
        self.tree.bind("<Double-1>", lambda _e: self._choose())
        self.evidence = ttk.Label(frame, style="CardMuted.TLabel", wraplength=920, justify="left", text="")
        self.evidence.pack(anchor="w", pady=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="Use selected draft", style="Accent.TButton",
                   command=self._choose).pack(side="right", padx=(0, 8))

    def _on_select(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if selection:
            self.evidence.configure(text="Evidence: " + self._items[selection[0]].evidence_summary())

    def _choose(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "Please select a draft first.", parent=self)
            return
        self.result = self._items[selection[0]]
        self.destroy()


class PdfPickerDialog(_ModalDialog):
    """Choose a specific (older) unprocessed PDF - still one PDF at a time."""

    def __init__(self, parent: tk.Misc, candidates: list[tuple[Path, Any, str]]):
        super().__init__(parent, "Choose a PDF")
        self.geometry("860x420")
        frame = ttk.Frame(self, style="Card.TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, style="Card.TLabel", wraplength=800, justify="left",
                  text="Choose ONE PDF to process. Only files marked 'ready' can be selected.").pack(
            anchor="w", pady=(0, 10))
        tree_frame = ttk.Frame(frame, style="Card.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("name", "modified", "size", "status"),
                                 show="headings", selectmode="browse")
        for col, title, width in (("name", "File", 380), ("modified", "Modified", 140),
                                  ("size", "Size", 80), ("status", "Status", 220)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="w", stretch=True)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("blocked", foreground=COLORS["disabled_fg"])
        self._items: dict[str, tuple[Path, str]] = {}
        for path, st, status in candidates:
            modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            item = self.tree.insert("", "end", values=(path.name, modified, human_size(st.st_size), status),
                                    tags=() if status == "ready" else ("blocked",))
            self._items[item] = (path, status)
        self.tree.bind("<Double-1>", lambda _e: self._choose())
        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="Use selected PDF", style="Accent.TButton",
                   command=self._choose).pack(side="right", padx=(0, 8))

    def _choose(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "Please select a PDF first.", parent=self)
            return
        path, status = self._items[selection[0]]
        if status != "ready":
            messagebox.showwarning(APP_NAME, f"This PDF cannot be selected:\n{status}", parent=self)
            return
        self.result = path
        self.destroy()


class CompanyDialog(_ModalDialog):
    """Ask for / correct the company name."""

    def __init__(self, parent: tk.Misc, filename: str, explanation: str, initial: str):
        super().__init__(parent, "Company name")
        frame = ttk.Frame(self, style="Card.TFrame", padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=filename, style="CardValue.TLabel", wraplength=560).pack(anchor="w")
        ttk.Label(frame, text=explanation, style="Card.TLabel", wraplength=560, justify="left").pack(
            anchor="w", pady=(8, 10))
        ttk.Label(frame, text="Company name (as it appears in the draft):", style="CardKey.TLabel").pack(anchor="w")
        self.var = tk.StringVar(value=initial)
        entry = ttk.Entry(frame, textvariable=self.var, width=60)
        entry.pack(fill="x", pady=(4, 12))
        entry.focus_set()
        entry.select_range(0, "end")
        entry.bind("<Return>", lambda _e: self._ok())
        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="Use this name", style="Accent.TButton", command=self._ok).pack(
            side="right", padx=(0, 8))

    def _ok(self) -> None:
        value = " ".join(self.var.get().split())
        if not value:
            messagebox.showwarning(APP_NAME, "Please enter a company name.", parent=self)
            return
        self.result = value
        self.destroy()


class SettingsDialog(_ModalDialog):
    """Edit the most important parser and scanner settings."""

    def __init__(self, parent: tk.Misc, config: Config):
        super().__init__(parent, "Settings")
        self.config_obj = config
        parser = config.filename_parser
        scanner = config.scanner
        frame = ttk.Frame(self, style="Card.TFrame", padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="File name rules", style="Card.TLabel", font=("", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(frame, style="CardMuted.TLabel", wraplength=600, justify="left",
                  text="Expected: <Applicant name>_<Application type>_<Job title>_<Company>.pdf  "
                       "Lists are comma separated.").grid(row=1, column=0, columnspan=2, sticky="w")
        self.vars: dict[str, tk.Variable] = {}
        fields = [
            ("expected_applicant_name", "Applicant name (empty = any)", parser.get("expected_applicant_name", "")),
            ("applicant_name_parts", "Name parts (if applicant name is empty)", str(parser.get("applicant_name_parts", 2))),
            ("application_types", "Application types", ", ".join(parser.get("application_types", []))),
            ("job_titles", "Job titles", ", ".join(parser.get("job_titles", []))),
            ("ignored_trailing_tokens", "Ignored version markers (regex)", ", ".join(parser.get("ignored_trailing_tokens", []))),
            ("min_file_age_seconds", "Ignore files modified in the last (seconds)", str(scanner.get("min_file_age_seconds", 3.0))),
        ]
        for row, (key, label, value) in enumerate(fields, start=2):
            ttk.Label(frame, text=label + ":", style="CardKey.TLabel").grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            var = tk.StringVar(value=value)
            ttk.Entry(frame, textvariable=var, width=52).grid(row=row, column=1, sticky="ew", pady=4)
            self.vars[key] = var
        row = len(fields) + 2
        self.vars["require_application_type"] = tk.BooleanVar(value=parser.get("require_application_type", True))
        self.vars["require_job_title"] = tk.BooleanVar(value=parser.get("require_job_title", True))
        ttk.Checkbutton(frame, text="Application type is required", style="Card.TCheckbutton",
                        variable=self.vars["require_application_type"]).grid(row=row, column=1, sticky="w")
        ttk.Checkbutton(frame, text="Job title is required (recommended)", style="Card.TCheckbutton",
                        variable=self.vars["require_job_title"]).grid(row=row + 1, column=1, sticky="w")
        ttk.Label(frame, style="CardMuted.TLabel", wraplength=600, justify="left",
                  text=f"Advanced settings: {config.paths.config_file}").grid(
            row=row + 2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.grid(row=row + 3, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=(0, 8))
        frame.columnconfigure(1, weight=1)

    def _save(self) -> None:
        def as_list(key: str) -> list[str]:
            return [v.strip() for v in str(self.vars[key].get()).split(",") if v.strip()]

        try:
            parts = int(str(self.vars["applicant_name_parts"].get()).strip() or "2")
            min_age = float(str(self.vars["min_file_age_seconds"].get()).strip().replace(",", ".") or "3")
            if parts < 0 or min_age < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Name parts and seconds must be positive numbers.", parent=self)
            return
        import re
        for pattern in as_list("ignored_trailing_tokens"):
            try:
                re.compile(pattern)
            except re.error as exc:
                messagebox.showerror(APP_NAME, f"Invalid version marker pattern '{pattern}': {exc}", parent=self)
                return
        parser = self.config_obj.filename_parser
        parser["expected_applicant_name"] = str(self.vars["expected_applicant_name"].get()).strip()
        parser["applicant_name_parts"] = parts
        parser["application_types"] = as_list("application_types")
        parser["job_titles"] = as_list("job_titles")
        parser["ignored_trailing_tokens"] = as_list("ignored_trailing_tokens")
        parser["require_application_type"] = bool(self.vars["require_application_type"].get())
        parser["require_job_title"] = bool(self.vars["require_job_title"].get())
        self.config_obj.scanner["min_file_age_seconds"] = min_age
        self.result = True
        self.destroy()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

GmailFactory = Callable[..., GmailClient]


def _modal(method: Callable[..., Any]) -> Callable[..., Any]:
    """Marks a method that opens a dialog, so the automatic check waits."""
    import functools

    @functools.wraps(method)
    def wrapper(self: "App", *args: Any, **kwargs: Any) -> Any:
        self._modal_depth += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self._modal_depth -= 1
    return wrapper


class App(tk.Tk):
    POLL_MS = 50

    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        token_store: TokenStore | None = None,
        gmail_factory: GmailFactory | None = None,
        force_dry_run: bool = False,
        auto_connect: bool = True,
    ):
        super().__init__()
        self.config_obj = config
        self.db = db
        self.paths = config.paths
        self.token_store = token_store or TokenStore(config.paths.token_file)
        self.gmail_factory: GmailFactory = gmail_factory or GmailClient.connect
        self.force_dry_run = force_dry_run
        self.worker = Worker()
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self._log_handler = QueueLogHandler(self.log_queue)
        logging.getLogger().addHandler(self._log_handler)

        # Workflow state
        self.gmail: GmailClient | None = None
        self.gmail_state = "disconnected"  # disconnected / connecting / connected / expired
        self.current_pdf: PdfFile | None = None
        self.parse_result: ParseResult | None = None
        self.company: str | None = None
        self.drafts: list[DraftInfo] | None = None
        self.drafts_truncated = False
        self.outcome: MatchOutcome | None = None
        self.selected_match: DraftMatch | None = None
        self.manual_match: DraftMatch | None = None
        self.rejected_draft_ids: set[str] = set()
        self.busy = False
        self.last_result: AttachResult | None = None
        self._tree_items: dict[str, DraftMatch] = {}
        self._history_items: dict[str, HistoryRecord] = {}
        # Automatic checking
        self._auto_flow = False          # current PDF was found by the automatic check
        self._auto_seen: set[str] = set()  # PDFs (SHA-256) already shown automatically
        self._modal_depth = 0            # >0 while a dialog is open

        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1220x840")
        self.minsize(1000, 700)
        self.fonts = _setup_style(self)
        self.report_callback_exception = self._on_tk_exception  # type: ignore[assignment]
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.dry_run_var = tk.BooleanVar(value=True if force_dry_run else config.dry_run)
        self.show_dry_runs_var = tk.BooleanVar(value=True)
        self.folder_var = tk.StringVar(value=str(config.watch_folder))
        self.auto_check_var = tk.BooleanVar(value=config.auto_check)
        self.auto_send_var = tk.BooleanVar(value=config.auto_send)

        self._build_menu()
        self._build_header()
        self._build_statusbar()  # packed before the body so it is never squeezed out
        self._build_body()
        self._reset_workflow(keep_status=True)
        self._update_dry_run_ui()
        self.refresh_history()
        self.after(self.POLL_MS, self._poll)
        self.after(self.config_obj.auto_check_interval_ms, self._auto_tick)

        incomplete = self.db.list_incomplete()
        if incomplete:
            self._set_status(f"{len(incomplete)} earlier operation(s) need to be checked - "
                             "they are verified automatically when Gmail is connected.")
        if auto_connect and self.token_store.exists():
            self.after(200, lambda: self.connect_gmail(interactive=False))

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Select Bewerbungen folder...", command=self.browse_folder)
        file_menu.add_command(label="Select OAuth credentials file...", command=self.select_credentials_file)
        file_menu.add_command(label="Settings...", command=self.open_settings)
        file_menu.add_separator()
        file_menu.add_command(label="Open data folder", command=lambda: open_in_file_manager(self.paths.root))
        file_menu.add_command(label="Open log folder", command=lambda: open_in_file_manager(self.paths.log_dir))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        gmail_menu = tk.Menu(menubar, tearoff=False)
        gmail_menu.add_command(label="Connect Gmail", command=lambda: self.connect_gmail(interactive=True))
        gmail_menu.add_command(label="Disconnect and remove token", command=self.disconnect_gmail)
        menubar.add_cascade(label="Gmail", menu=gmail_menu)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="How it works", command=self._show_help)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menubar)

    def _build_header(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(22, 14))
        header.pack(fill="x")
        # The status pills are packed first so they always get their full width.
        right = ttk.Frame(header, style="Header.TFrame")
        right.pack(side="right", anchor="n")
        self.gmail_pill = ttk.Label(right, text="● Gmail: not connected", style="PillOff.TLabel")
        self.gmail_pill.pack(anchor="e")
        self.mode_pill = ttk.Label(right, text="", style="PillDry.TLabel")
        self.mode_pill.pack(anchor="e", pady=(6, 0))
        left = ttk.Frame(header, style="Header.TFrame")
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text=APP_NAME, style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(left, text="Attaches your newest application PDF to the matching Gmail draft. "
                             "Sending only if you enable it.", style="HeaderSub.TLabel").pack(anchor="w", pady=(2, 0))

    def _card(self, parent: tk.Misc, title: str) -> ttk.Labelframe:
        return ttk.Labelframe(parent, text=title, style="Card.TLabelframe")

    def _build_body(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(10, 0))
        self.attach_tab = ttk.Frame(self.notebook, padding=(4, 10, 4, 4))
        self.history_tab = ttk.Frame(self.notebook, padding=(4, 10, 4, 4))
        self.log_tab = ttk.Frame(self.notebook, padding=(4, 10, 4, 4))
        self.notebook.add(self.attach_tab, text="Attach")
        self.notebook.add(self.history_tab, text="Processing history")
        self.notebook.add(self.log_tab, text="Activity log")
        self._build_attach_tab(self.attach_tab)
        self._build_history_tab(self.history_tab)
        self._build_log_tab(self.log_tab)

    def _build_attach_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=2, uniform="cols")
        tab.columnconfigure(1, weight=3, uniform="cols")
        tab.rowconfigure(1, weight=1)

        # 1. Folder & connection
        setup = self._card(tab, "1  Folder and Gmail connection")
        setup.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        setup.columnconfigure(1, weight=1)
        ttk.Label(setup, text="Bewerbungen folder:", style="CardKey.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        folder_entry = ttk.Entry(setup, textvariable=self.folder_var, state="readonly")
        folder_entry.grid(row=0, column=1, sticky="ew")
        self.browse_button = ttk.Button(setup, text="Browse...", command=self.browse_folder)
        self.browse_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Button(setup, text="Open", command=lambda: open_in_file_manager(Path(self.folder_var.get()))).grid(
            row=0, column=3, padx=(8, 0))
        actions = ttk.Frame(setup, style="Card.TFrame")
        actions.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        self.connect_button = ttk.Button(actions, text="Connect Gmail", style="Accent.TButton",
                                         command=lambda: self.connect_gmail(interactive=True))
        self.connect_button.pack(side="left")
        self.scan_button = ttk.Button(actions, text="Scan for newest PDF", style="Accent.TButton",
                                      command=self.scan_newest)
        self.scan_button.pack(side="left", padx=(10, 0))
        self.dry_run_check = ttk.Checkbutton(
            actions, text="Dry-run mode (preview only - Gmail is never modified)",
            variable=self.dry_run_var, style="Card.TCheckbutton", command=self._on_dry_run_toggled)
        self.dry_run_check.pack(side="right")
        if self.force_dry_run:
            self.dry_run_check.state(["disabled"])
        auto_row = ttk.Frame(setup, style="Card.TFrame")
        auto_row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.auto_check_check = ttk.Checkbutton(
            auto_row, text="Check automatically for new PDFs (no need to click Scan)",
            variable=self.auto_check_var, style="Card.TCheckbutton", command=self._on_auto_toggled)
        self.auto_check_check.pack(side="left")
        self.auto_send_check = ttk.Checkbutton(
            auto_row, text="Send e-mail automatically after attaching (only for one clear match)",
            variable=self.auto_send_var, style="Card.TCheckbutton", command=self._on_auto_send_toggled)
        self.auto_send_check.pack(side="right")

        # 2. PDF card
        pdf_card = self._card(tab, "2  Application PDF")
        pdf_card.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        pdf_card.columnconfigure(1, weight=1)
        self.pdf_labels: dict[str, ttk.Label] = {}
        for row, (key, title) in enumerate((("file", "File"), ("company", "Company"), ("size", "File size"),
                                            ("modified", "Modified"), ("hash", "SHA-256"))):
            ttk.Label(pdf_card, text=title + ":", style="CardKey.TLabel").grid(row=row, column=0, sticky="nw", pady=4, padx=(0, 10))
            label = ttk.Label(pdf_card, text="-", style="CardValue.TLabel", wraplength=330, justify="left")
            label.grid(row=row, column=1, sticky="nw", pady=4)
            self.pdf_labels[key] = label
        self.edit_company_button = ttk.Button(pdf_card, text="Edit company...", command=self.edit_company)
        self.edit_company_button.grid(row=5, column=1, sticky="w", pady=(6, 0))
        self.pdf_notes = ttk.Label(pdf_card, text="", style="CardMuted.TLabel", wraplength=380, justify="left")
        self.pdf_notes.grid(row=6, column=0, columnspan=2, sticky="nw", pady=(12, 0))
        pdf_card.rowconfigure(7, weight=1)
        self.pick_pdf_button = ttk.Button(pdf_card, text="Choose a different PDF...", command=self.pick_pdf)
        self.pick_pdf_button.grid(row=8, column=0, columnspan=2, sticky="sw", pady=(10, 0))

        # 3. Matching draft card
        draft_card = self._card(tab, "3  Matching Gmail draft")
        draft_card.grid(row=1, column=1, sticky="nsew")
        draft_card.columnconfigure(0, weight=1)
        draft_card.rowconfigure(1, weight=1)
        self.banner = ttk.Label(draft_card, text="", style="BannerNeutral.TLabel", wraplength=640, justify="left")
        self.banner.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tree_frame = ttk.Frame(draft_card, style="Card.TFrame")
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = ("match", "subject", "recipient", "date")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=5, selectmode="browse")
        for col, title, width, stretch in (("match", "Match", 115, False), ("subject", "Subject", 220, True),
                                           ("recipient", "Recipient", 170, True),
                                           ("date", "Last saved", 140, False)):
            self.tree.heading(col, text=title, anchor="w")
            self.tree.column(col, width=width, minwidth=80, anchor="w", stretch=stretch)
        self.tree.tag_configure(STRONG, foreground="#14532d")
        self.tree.tag_configure(POSSIBLE, foreground="#7a4500")
        self.tree.tag_configure(WEAK, foreground="#6b7280")
        self.tree.tag_configure("manual", foreground="#7f1d1d")
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_candidate_select)

        details = ttk.Frame(draft_card, style="Card.TFrame")
        details.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        details.columnconfigure(1, weight=1)
        self.detail_labels: dict[str, ttk.Label] = {}
        for row, (key, title) in enumerate((("recipient", "Recipient"), ("subject", "Subject"),
                                            ("date", "Last saved"), ("attachments", "Existing attachments"),
                                            ("new", "New attachment"), ("why", "Why it matched"))):
            ttk.Label(details, text=title + ":", style="CardKey.TLabel").grid(row=row, column=0, sticky="nw", pady=3, padx=(0, 10))
            label = ttk.Label(details, text="-", style="CardValue.TLabel", wraplength=520, justify="left")
            label.grid(row=row, column=1, sticky="nw", pady=3)
            self.detail_labels[key] = label
        draft_card.bind("<Configure>", self._on_draft_card_resize)
        pdf_card.bind("<Configure>", self._on_pdf_card_resize)

        # Action buttons
        action_bar = ttk.Frame(tab)
        action_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.confirm_button = ttk.Button(action_bar, text="Confirm and Attach", style="Success.TButton",
                                         command=self.confirm_and_attach)
        self.confirm_button.pack(side="left")
        self.reject_button = ttk.Button(action_bar, text="Reject Match", command=self.reject_match)
        self.reject_button.pack(side="left", padx=(10, 0))
        self.select_other_button = ttk.Button(action_bar, text="Select Another Draft...", command=self.select_another_draft)
        self.select_other_button.pack(side="left", padx=(10, 0))
        self.cancel_button = ttk.Button(action_bar, text="Cancel", command=self.cancel_operation)
        self.cancel_button.pack(side="left", padx=(10, 0))
        self.action_hint = ttk.Label(action_bar, text="", style="Muted.TLabel", wraplength=480, justify="right")
        self.action_hint.pack(side="right")

    def _on_draft_card_resize(self, event: tk.Event) -> None:
        width = max(300, event.width - 60)
        self.banner.configure(wraplength=width)
        for label in self.detail_labels.values():
            label.configure(wraplength=max(240, width - 170))

    def _on_pdf_card_resize(self, event: tk.Event) -> None:
        width = max(220, event.width - 140)
        for label in self.pdf_labels.values():
            label.configure(wraplength=width)
        self.pdf_notes.configure(wraplength=max(240, event.width - 40))

    def _build_history_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(top, text="Refresh", command=self.refresh_history).pack(side="left")
        self.mark_success_button = ttk.Button(top, text="Mark as attached (checked in Gmail)",
                                              command=lambda: self._resolve_selected(STATUS_SUCCESS))
        self.mark_success_button.pack(side="left", padx=(8, 0))
        self.mark_failed_button = ttk.Button(top, text="Mark as not attached (allow retry)",
                                             command=lambda: self._resolve_selected(STATUS_FAILED))
        self.mark_failed_button.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(top, text="Show dry runs", variable=self.show_dry_runs_var,
                        command=self.refresh_history).pack(side="right")

        frame = ttk.Frame(tab)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        columns = ("date", "status", "pdf", "company", "subject", "recipient", "draft")
        self.history_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col, title, width, stretch in (("date", "Date", 150, False), ("status", "Status", 175, False),
                                           ("pdf", "PDF file", 240, True), ("company", "Company", 140, True),
                                           ("subject", "Draft subject", 200, True),
                                           ("recipient", "Recipient", 170, True), ("draft", "Draft ID", 110, False)):
            self.history_tree.heading(col, text=title, anchor="w")
            self.history_tree.column(col, width=width, minwidth=70, anchor="w", stretch=stretch)
        self.history_tree.tag_configure(STATUS_SUCCESS, foreground="#14532d")
        self.history_tree.tag_configure(STATUS_ALREADY_ATTACHED, foreground="#14532d")
        self.history_tree.tag_configure(STATUS_FAILED, foreground="#991b1b")
        self.history_tree.tag_configure(STATUS_UNVERIFIED, foreground="#9a3412", background="#fff7ed")
        self.history_tree.tag_configure(STATUS_IN_PROGRESS, foreground="#9a3412", background="#fff7ed")
        self.history_tree.tag_configure(STATUS_DRY_RUN, foreground="#6b7280")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.history_tree.yview)
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.history_tree.xview)
        self.history_tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_select)
        self.history_details = tk.Text(tab, height=10, wrap="word", relief="flat", background="#ffffff",
                                       foreground=COLORS["text"], font=self.fonts["small"], padx=10, pady=8)
        self.history_details.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.history_details.configure(state="disabled")

    def _build_log_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.log_text = tk.Text(tab, wrap="word", relief="flat", background="#ffffff",
                                foreground=COLORS["text"], font=self.fonts["mono"], padx=10, pady=8)
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        bottom = ttk.Frame(tab)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(bottom, text="Full log file (OAuth secrets are never logged):", style="Muted.TLabel").pack(side="left")
        ttk.Button(bottom, text="Open log folder",
                   command=lambda: open_in_file_manager(self.paths.log_dir)).pack(side="right")

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, style="Status.TFrame", padding=(14, 6))
        bar.pack(fill="x", side="bottom", pady=(10, 0))
        self.status_label = ttk.Label(bar, text="Ready.", style="Status.TLabel")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bar, mode="determinate", length=180, value=0)
        self.progress.pack(side="right")

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    def _poll(self) -> None:
        try:
            self.worker.poll()
            lines = []
            while True:
                try:
                    lines.append(self.log_queue.get_nowait())
                except queue.Empty:
                    break
            if lines:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", "\n".join(lines) + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        finally:
            self.after(self.POLL_MS, self._poll)

    def _set_status(self, text: str) -> None:
        text = " ".join(text.split())
        self.status_label.configure(text=text if len(text) <= 170 else text[:169] + "…")

    def _run_task(
        self,
        status: str,
        fn: Callable[[], Any],
        on_success: Callable[[Any], None],
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """Run fn on the worker thread; call on_success/on_error on the Tk thread."""
        self.busy = True
        self._set_status(status)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._refresh_buttons()

        def done(future: Future) -> None:
            self.busy = False
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self._refresh_buttons()
            error = future.exception()
            if error is None:
                on_success(future.result())
            elif on_error is not None:
                on_error(error)
            else:
                self._handle_error(error)

        self.worker.submit(fn, done)

    def _handle_error(self, error: BaseException, context: str = "") -> None:
        if isinstance(error, GmailAuthError):
            self._set_gmail_state("expired")
        log.error("%s%s", f"{context}: " if context else "", error,
                  exc_info=(type(error), error, error.__traceback__) if not isinstance(error, (GmailError, AttachError, ScanError)) else None)
        message = str(error) or type(error).__name__
        if not isinstance(error, (GmailError, AttachError, ScanError)):
            message = f"Unexpected error: {type(error).__name__}: {message}\n\nDetails are in the log file."
        self._set_status(message.splitlines()[0])
        self.show_error(message)

    def _on_tk_exception(self, exc_type: type, exc: BaseException, tb: Any) -> None:
        log.error("Unhandled GUI error: %s", "".join(traceback.format_exception(exc_type, exc, tb)))
        self.show_error(f"Unexpected error: {exc}\n\nDetails are in the log file.")

    def _set_banner(self, kind: str, text: str) -> None:
        icon = BANNER_ICONS.get(kind, "")
        self.banner.configure(style=f"Banner{kind.title()}.TLabel", text=f"{icon}  {text}" if text else "")

    # Overridable interaction methods (tests replace these) -------------
    @_modal
    def show_info(self, message: str, title: str = APP_NAME) -> None:
        messagebox.showinfo(title, message, parent=self)

    @_modal
    def show_warning(self, message: str, title: str = APP_NAME) -> None:
        messagebox.showwarning(title, message, parent=self)

    @_modal
    def show_error(self, message: str, title: str = APP_NAME) -> None:
        messagebox.showerror(title, message, parent=self)

    @_modal
    def ask_yes_no(self, message: str, title: str = APP_NAME, warning: bool = False) -> bool:
        icon = messagebox.WARNING if warning else messagebox.QUESTION
        return bool(messagebox.askyesno(title, message, parent=self, icon=icon))

    @_modal
    def ask_company(self, filename: str, explanation: str, initial: str) -> str | None:
        return CompanyDialog(self, filename, explanation, initial).show()

    @_modal
    def ask_confirm_plan(self, plan: AttachPlan, match: DraftMatch | None, manual: bool) -> bool:
        dialog = ConfirmAttachDialog(self, plan, match, self.dry_run_active, manual, self.fonts,
                                     send=self.send_enabled)
        return bool(dialog.show())

    @_modal
    def ask_pick_draft(self, matches: list[DraftMatch]) -> DraftMatch | None:
        return DraftPickerDialog(self, matches, self.rejected_draft_ids, self.company or "").show()

    @_modal
    def ask_pick_pdf(self, candidates: list) -> Path | None:
        return PdfPickerDialog(self, candidates).show()

    @_modal
    def ask_directory(self, initial: str) -> str:
        return filedialog.askdirectory(parent=self, initialdir=initial, title="Select the Bewerbungen folder")

    @_modal
    def ask_credentials_file(self) -> str:
        return filedialog.askopenfilename(
            parent=self, title="Select the OAuth client JSON file (Desktop app)",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])

    # ------------------------------------------------------------------
    # State display
    # ------------------------------------------------------------------
    @property
    def dry_run_active(self) -> bool:
        return self.force_dry_run or bool(self.dry_run_var.get())

    @property
    def send_enabled(self) -> bool:
        return bool(self.auto_send_var.get())

    def _save_config(self) -> None:
        try:
            self.config_obj.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)

    def _on_auto_toggled(self) -> None:
        self.config_obj.auto_check = bool(self.auto_check_var.get())
        self._save_config()
        log.info("Automatic checking %s", "enabled" if self.config_obj.auto_check else "disabled")

    def _on_auto_send_toggled(self) -> None:
        if self.auto_send_var.get() and not self.ask_yes_no(
                "Enable automatic sending?\n\n"
                "When the app finds exactly ONE clear (strong) matching draft for a new PDF, it will "
                "attach the PDF and SEND the e-mail immediately, without asking.\n\n"
                "Ambiguous or uncertain matches still wait for you. A sent e-mail cannot be recalled. "
                "While dry-run mode is on, nothing is sent.", title="Automatic sending", warning=True):
            self.auto_send_var.set(False)
        self.config_obj.auto_send = bool(self.auto_send_var.get())
        self._save_config()
        log.info("Automatic sending %s", "enabled" if self.config_obj.auto_send else "disabled")
        self._update_dry_run_ui()

    def _update_dry_run_ui(self) -> None:
        if self.dry_run_active:
            self.mode_pill.configure(text="DRY-RUN: Gmail is not modified", style="PillDry.TLabel")
            self.confirm_button.configure(text="Confirm (Dry Run)")
        elif self.send_enabled:
            self.mode_pill.configure(text="LIVE: attach AND SEND automatically", style="PillLive.TLabel")
            self.confirm_button.configure(text="Confirm, Attach and Send")
        else:
            self.mode_pill.configure(text="LIVE: drafts will be updated", style="PillLive.TLabel")
            self.confirm_button.configure(text="Confirm and Attach")

    def _on_dry_run_toggled(self) -> None:
        if self.force_dry_run:
            self.dry_run_var.set(True)
            return
        self.config_obj.dry_run = bool(self.dry_run_var.get())
        try:
            self.config_obj.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
        log.info("Dry-run mode %s", "enabled" if self.dry_run_active else "disabled")
        self._update_dry_run_ui()

    def _set_gmail_state(self, state: str) -> None:
        self.gmail_state = state
        if state == "connected" and self.gmail is not None:
            self.gmail_pill.configure(text=f"● Gmail: {self.gmail.account_email or 'connected'}", style="PillOn.TLabel")
            self.connect_button.configure(text="Reconnect Gmail")
        elif state == "connecting":
            self.gmail_pill.configure(text="● Gmail: connecting...", style="PillWarn.TLabel")
        elif state == "expired":
            self.gmail = None
            self.gmail_pill.configure(text="● Gmail: sign-in expired", style="PillWarn.TLabel")
            self.connect_button.configure(text="Connect Gmail")
        else:
            self.gmail = None
            self.gmail_pill.configure(text="● Gmail: not connected", style="PillOff.TLabel")
            self.connect_button.configure(text="Connect Gmail")
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        def enable(widget: ttk.Widget, on: bool) -> None:
            widget.state(["!disabled"] if on else ["disabled"])

        idle = not self.busy
        enable(self.connect_button, idle)
        enable(self.scan_button, idle)
        enable(self.browse_button, idle)
        enable(self.pick_pdf_button, idle)
        enable(self.edit_company_button, idle and self.current_pdf is not None)
        can_confirm = (idle and self.selected_match is not None and self.current_pdf is not None
                       and self.gmail is not None and self.company is not None)
        enable(self.confirm_button, can_confirm)
        enable(self.reject_button, idle and self.selected_match is not None)
        enable(self.select_other_button, idle and self.drafts is not None and self.company is not None)
        enable(self.cancel_button, idle and self.current_pdf is not None)

        hint = ""
        if idle and self.current_pdf is not None and self.selected_match is None:
            if self.outcome is not None and self.outcome.status == MULTIPLE:
                hint = "Select the correct draft in the list to continue."
            elif self.outcome is not None and self.outcome.is_stop:
                hint = "Nothing will be attached automatically. Edit the company name or select a draft manually."
            elif self.gmail is None:
                hint = "Connect Gmail to find the matching draft."
        self.action_hint.configure(text=hint)

    def _reset_workflow(self, keep_status: bool = False) -> None:
        self._auto_flow = False
        self.current_pdf = None
        self.parse_result = None
        self.company = None
        self.drafts = None
        self.outcome = None
        self.selected_match = None
        self.manual_match = None
        self.rejected_draft_ids = set()
        for label in self.pdf_labels.values():
            label.configure(text="-")
        self.pdf_notes.configure(text="")
        self._clear_candidates()
        self._set_banner("neutral", "Click 'Scan for newest PDF' to start. Exactly one PDF is processed per run.")
        if not keep_status:
            self._set_status("Ready.")
        self._refresh_buttons()

    def _clear_candidates(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._tree_items = {}
        for label in self.detail_labels.values():
            label.configure(text="-")

    def _show_pdf(self, pdf: PdfFile, notes: list[str]) -> None:
        self.pdf_labels["file"].configure(text=pdf.name)
        self.pdf_labels["company"].configure(text=self.company or "(not determined)")
        self.pdf_labels["size"].configure(text=human_size(pdf.size))
        self.pdf_labels["modified"].configure(text=datetime.fromtimestamp(pdf.mtime).strftime("%Y-%m-%d %H:%M:%S"))
        self.pdf_labels["hash"].configure(text=f"{pdf.sha256[:16]}...{pdf.sha256[-8:]}")
        self.pdf_notes.configure(text="\n".join(notes))

    def _show_match_details(self, match: DraftMatch | None, note: str = "") -> None:
        if match is None:
            for label in self.detail_labels.values():
                label.configure(text="-")
            if note:
                self.detail_labels["why"].configure(text=note)
            return
        draft = match.draft
        self.detail_labels["recipient"].configure(text=draft.recipients_display())
        self.detail_labels["subject"].configure(text=draft.subject or "(no subject)")
        self.detail_labels["date"].configure(text=draft.date_display)
        self.detail_labels["attachments"].configure(text=draft.attachments_display())
        if self.current_pdf is not None:
            duplicate = [a for a in draft.attachments if a.filename.casefold() == self.current_pdf.name.casefold()]
            new_text = f"{self.current_pdf.name} ({human_size(self.current_pdf.size)})"
            if duplicate:
                new_text += "  -  WARNING: a file with this name is already attached"
            self.detail_labels["new"].configure(text=new_text)
        why = f"{match.strength_label}: {match.evidence_summary()}"
        if note:
            why = f"{why}\n{note}"
        self.detail_labels["why"].configure(text=why)

    # ------------------------------------------------------------------
    # Folder / settings / credentials
    # ------------------------------------------------------------------
    def browse_folder(self) -> None:
        if self.busy:
            return
        chosen = self.ask_directory(self.folder_var.get())
        if not chosen:
            return
        folder = Path(chosen)
        self.folder_var.set(str(folder))
        self.config_obj.watch_folder = folder
        try:
            self.config_obj.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
        log.info("Bewerbungen folder set to %s", folder)
        self._reset_workflow()

    @_modal
    def open_settings(self) -> None:
        if self.busy:
            return
        if SettingsDialog(self, self.config_obj).show():
            try:
                self.config_obj.save()
                log.info("Settings saved")
            except OSError as exc:
                self.show_error(f"Could not save settings: {exc}")
            if self.current_pdf is not None:
                self.show_info("Settings saved. Scan again to apply the new file name rules.")

    def select_credentials_file(self) -> bool:
        chosen = self.ask_credentials_file()
        if not chosen:
            return False
        try:
            install_client_secret_file(Path(chosen), self.paths)
        except (CredentialsFileError, OSError) as exc:
            self.show_error(f"This file cannot be used:\n\n{exc}")
            return False
        self.show_info("OAuth credentials saved. You can now connect Gmail.")
        return True

    # ------------------------------------------------------------------
    # Gmail connection
    # ------------------------------------------------------------------
    def connect_gmail(self, interactive: bool = True) -> None:
        if self.busy:
            return
        if interactive and not self.paths.client_secret_file.exists():
            self.show_info(
                "To connect Gmail, select the OAuth client file (JSON) you created in the Google "
                "Cloud Console (type 'Desktop app'). See README.md for step-by-step instructions.")
            if not self.select_credentials_file():
                return
        self._set_gmail_state("connecting")

        def url_callback(url: str) -> None:
            self.worker.post(self._set_status, "Complete the sign-in in your browser. "
                                               "(If no browser opened, see the Activity log.)")
            log.info("If your browser did not open, visit this sign-in link: %s", url)

        def task() -> GmailClient:
            return self.gmail_factory(
                self.paths, self.token_store, interactive=interactive,
                timeout_seconds=self.config_obj.oauth_timeout_seconds, url_callback=url_callback)

        def on_success(client: GmailClient) -> None:
            self.gmail = client
            self._set_gmail_state("connected")
            self._set_status(f"Connected to Gmail as {client.account_email}.")
            self._run_recovery_then_continue()

        def on_error(error: BaseException) -> None:
            if not interactive:
                self._set_gmail_state("expired" if isinstance(error, GmailAuthError) else "disconnected")
                self._set_status(f"Gmail not connected: {error}")
                log.info("Automatic Gmail connection failed: %s", error)
                return
            self._set_gmail_state("disconnected")
            self._handle_error(error)

        status = ("Waiting for the Google sign-in in your browser..." if interactive
                  else "Connecting to Gmail with the saved authorization...")
        self._run_task(status, task, on_success, on_error)

    def disconnect_gmail(self) -> None:
        if self.busy:
            return
        if not self.ask_yes_no("Disconnect Gmail and remove the stored authorization from this computer?"):
            return
        self._run_task("Disconnecting...", lambda: revoke_token(self.token_store),
                       lambda _r: (self._set_gmail_state("disconnected"),
                                   self._set_status("Gmail disconnected; token removed.")))

    def _run_recovery_then_continue(self) -> None:
        if not self.db.list_incomplete() or self.gmail is None:
            self._continue_after_connect()
            return
        manager = AttachmentManager(self.gmail, self.db)

        def on_success(messages: list[str]) -> None:
            self.refresh_history()
            if messages:
                self.show_info("Earlier interrupted or unverified operations were checked:\n\n"
                               + "\n".join(f"- {m}" for m in messages))
            self._continue_after_connect()

        self._run_task("Checking earlier interrupted operations...", manager.recover_interrupted, on_success)

    def _continue_after_connect(self) -> None:
        if self.current_pdf is not None and self.company:
            self.load_drafts_and_match()
        else:
            self._refresh_buttons()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def _make_scanner(self) -> FileScanner:
        return FileScanner(self.db, self.config_obj.scanner)

    def _ensure_folder(self) -> Path | None:
        folder = Path(self.folder_var.get())
        if folder.is_dir():
            return folder
        if not folder.exists() and self.ask_yes_no(
                f"The folder does not exist:\n{folder}\n\nCreate it now?"):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                log.info("Created folder %s", folder)
                return folder
            except OSError as exc:
                self.show_error(f"Could not create the folder: {exc}")
                return None
        if folder.exists():
            self.show_error(f"This is not a folder:\n{folder}")
        return None

    def scan_newest(self) -> None:
        if self.busy:
            return
        folder = self._ensure_folder()
        if folder is None:
            return
        self._reset_workflow()
        scanner = self._make_scanner()
        self._run_task(f"Scanning {folder} ...", lambda: scanner.scan(folder), self._on_scan_result)

    def _on_scan_result(self, result: ScanResult) -> None:
        if result.selected is None:
            details = "\n".join(f"- {s.name}: {s.reason}" for s in result.skipped[:10])
            self._set_banner("info", result.message + (f"\n\nSkipped:\n{details}" if details else ""))
            self._set_status(result.message)
            return
        notes = []
        newer = [s for s in result.skipped if s.name != result.selected.name]
        if newer:
            notes.append("Skipped files:\n" + "\n".join(f"- {s.name}: {s.reason}" for s in newer[:6]))
        if result.previous_failure is not None:
            notes.append(f"Note: a previous attempt on {result.previous_failure.local_time()} failed: "
                         f"{result.previous_failure.error_message or 'unknown error'}")
        self._set_status(result.message)
        self._select_pdf(result.selected, notes)

    def _select_pdf(self, pdf: PdfFile, notes: list[str], auto: bool = False) -> None:
        self.current_pdf = pdf
        self._auto_flow = auto
        self.rejected_draft_ids = set()
        parser = FilenameParser(self.config_obj.filename_parser)
        self.parse_result = parser.parse(pdf.name)
        notes = list(self.parse_result.notes) + notes
        if self.parse_result.ok:
            self.company = self.parse_result.company
        else:
            self.company = None
            self._show_pdf(pdf, notes)
            company = self.ask_company(
                pdf.name,
                "The company name could not be determined from the file name:\n"
                f"{self.parse_result.error}\n\nPlease enter the company name.",
                self.parse_result.suggestion)
            if not company:
                self._set_banner("warn", "No company name - nothing will be attached. Use 'Edit company...' "
                                         "to enter it.")
                self._refresh_buttons()
                return
            self.company = company
            notes.insert(0, "Company name entered manually.")
        self._show_pdf(pdf, notes)
        log.info("PDF %s -> company '%s'", pdf.name, self.company)
        if self.gmail is None:
            self._set_banner("info", f"Company: {self.company}. Connect Gmail to find the matching draft.")
            self._refresh_buttons()
            return
        self.load_drafts_and_match()

    def pick_pdf(self) -> None:
        if self.busy:
            return
        folder = self._ensure_folder()
        if folder is None:
            return
        scanner = self._make_scanner()

        def on_list(candidates: list) -> None:
            if not candidates:
                self.show_info("No PDF files found in the folder.")
                return
            chosen = self.ask_pick_pdf(candidates)
            if chosen is None:
                return
            self._reset_workflow()
            self._run_task(f"Checking {chosen.name} ...", lambda: scanner.check_single(chosen),
                           lambda pdf: self._select_pdf(pdf, ["Chosen manually."]))

        self._run_task("Listing PDF files...", lambda: scanner.list_candidates(folder), on_list)

    def edit_company(self) -> None:
        if self.busy or self.current_pdf is None:
            return
        company = self.ask_company(
            self.current_pdf.name,
            "Correct the company name if it was not extracted correctly. Matching is repeated "
            "with the new name.",
            self.company or (self.parse_result.suggestion if self.parse_result else ""))
        if not company or company == self.company:
            return
        self.company = company
        self.selected_match = None
        self.manual_match = None
        self.pdf_labels["company"].configure(text=company)
        log.info("Company name changed manually to '%s'", company)
        if self.gmail is not None:
            self.load_drafts_and_match()
        else:
            self._set_banner("info", f"Company: {company}. Connect Gmail to find the matching draft.")
            self._refresh_buttons()

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------
    def load_drafts_and_match(self) -> None:
        if self.gmail is None or self.company is None:
            return
        gmail = self.gmail
        max_drafts = int(self.config_obj.matching.get("max_drafts", 500))

        def progress(done: int, total: int) -> None:
            self.worker.post(self._set_status, f"Reading Gmail drafts ({done}/{total})...")

        def task() -> tuple[list[DraftInfo], bool]:
            return gmail.fetch_all_drafts(max_drafts=max_drafts, progress=progress)

        def on_success(result: tuple[list[DraftInfo], bool]) -> None:
            self.drafts, self.drafts_truncated = result
            self._apply_matching()

        self._run_task("Reading Gmail drafts...", task, on_success)

    def _apply_matching(self) -> None:
        if self.drafts is None or self.company is None:
            return
        engine = MatchingEngine(self.config_obj.matching.get("generic_email_domains", []))
        considered = [d for d in self.drafts if d.draft_id not in self.rejected_draft_ids]
        self.outcome = engine.match(self.company, considered)
        self.selected_match = None
        self.manual_match = None
        self._populate_candidates()
        outcome = self.outcome
        suffix = ""
        if self.drafts_truncated:
            suffix = f" (only the first {len(self.drafts)} drafts were checked)"
        if self.rejected_draft_ids:
            suffix += f" ({len(self.rejected_draft_ids)} rejected draft(s) excluded)"
        kind = {SINGLE_STRONG: "ok", SINGLE_POSSIBLE: "warn", MULTIPLE: "warn",
                WEAK_ONLY: "stop", NO_MATCH: "stop"}[outcome.status]
        self._set_banner(kind, outcome.message + suffix)
        self._set_status(f"{len(self.drafts)} draft(s) checked for '{self.company}'.")
        log.info("Matching '%s': %s", self.company, outcome.status)
        if self._auto_flow:
            self._continue_automatically()

    def _populate_candidates(self) -> None:
        self._clear_candidates()
        outcome = self.outcome
        if outcome is None:
            return
        rows: list[tuple[DraftMatch, str]] = []
        if self.manual_match is not None:
            rows.append((self.manual_match, "manual"))
        for match in outcome.candidates + outcome.weak_candidates:
            if self.manual_match is not None and match.draft.draft_id == self.manual_match.draft.draft_id:
                continue
            rows.append((match, match.strength))
        for match, tag in rows:
            draft = match.draft
            label = "Manually selected" if tag == "manual" else match.strength_label
            item = self.tree.insert("", "end", values=(label, draft.subject or "(no subject)",
                                                       draft.primary_recipient(), draft.date_display), tags=(tag,))
            self._tree_items[item] = match
        preselect = None
        if self.manual_match is not None:
            preselect = self.manual_match
        elif outcome.status in (SINGLE_STRONG, SINGLE_POSSIBLE):
            preselect = outcome.proposed
        if preselect is not None:
            for item, match in self._tree_items.items():
                if match is preselect:
                    self.tree.selection_set(item)
                    self.tree.see(item)
                    break
            self.selected_match = preselect
            self._show_match_details(preselect)
        elif not rows:
            self._show_match_details(None, "No candidate drafts.")
        self._refresh_buttons()

    def _on_candidate_select(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        match = self._tree_items.get(selection[0])
        if match is None:
            return
        if match is self.manual_match or match.strength in (STRONG, POSSIBLE):
            self.selected_match = match
            self._show_match_details(match)
        else:
            self.selected_match = None
            self._show_match_details(match, "Weak matches cannot be confirmed directly. If this really is the "
                                            "right draft, use 'Select Another Draft...' to choose it manually.")
        self._refresh_buttons()

    def reject_match(self) -> None:
        if self.busy or self.selected_match is None:
            return
        rejected = self.selected_match
        self.rejected_draft_ids.add(rejected.draft.draft_id)
        log.info("User rejected draft %s ('%s') for '%s'", rejected.draft.draft_id,
                 rejected.draft.subject, self.company)
        self._apply_matching()
        if self.outcome is not None and self.outcome.status in (SINGLE_STRONG, SINGLE_POSSIBLE):
            self._set_banner("warn", "The rejected draft was removed. " + self.outcome.message)

    def select_another_draft(self) -> None:
        if self.busy or self.drafts is None or self.company is None:
            return
        engine = MatchingEngine(self.config_obj.matching.get("generic_email_domains", []))
        ranked = engine.match(self.company, self.drafts).all_matches
        if not ranked:
            self.show_info("There are no drafts in this Gmail account.")
            return
        chosen = self.ask_pick_draft(ranked)
        if chosen is None:
            return
        rejected = chosen.draft.draft_id in self.rejected_draft_ids
        if chosen.strength not in (STRONG, POSSIBLE) or rejected:
            reason = ("you rejected this draft earlier" if rejected else
                      f"the company name '{self.company}' was not reliably found in it")
            if not self.ask_yes_no(
                    f"This draft does not reliably match: {reason}.\n\n"
                    f"Subject: {chosen.draft.subject or '(no subject)'}\n"
                    f"Recipient: {chosen.draft.recipients_display()}\n\n"
                    "Are you sure this draft belongs to this application?",
                    title="Manual draft selection", warning=True):
                return
            self.rejected_draft_ids.discard(chosen.draft.draft_id)
            self.manual_match = chosen
            log.info("User manually selected non-matching draft %s", chosen.draft.draft_id)
            self._populate_candidates()
            self._set_banner("warn", "Manually selected draft - check recipient and subject carefully before confirming.")
        else:
            self.manual_match = None
            for item, match in self._tree_items.items():
                if match.draft.draft_id == chosen.draft.draft_id:
                    self.tree.selection_set(item)
                    self.tree.see(item)
                    break
            self.selected_match = chosen
            self._show_match_details(chosen)
        self.selected_match = self.manual_match or chosen
        self._refresh_buttons()

    def cancel_operation(self) -> None:
        if self.busy:
            return
        log.info("Operation cancelled by the user - nothing was attached")
        self._reset_workflow()
        self._set_status("Cancelled. Nothing was attached.")

    # ------------------------------------------------------------------
    # Attaching
    # ------------------------------------------------------------------
    def confirm_and_attach(self) -> None:
        if self.busy or self.selected_match is None or self.current_pdf is None or self.gmail is None or not self.company:
            return
        match = self.selected_match
        manual = match is self.manual_match
        pdf = self.current_pdf
        company = self.company
        manager = AttachmentManager(self.gmail, self.db)

        def on_prepared(plan: AttachPlan) -> None:
            if not self.ask_confirm_plan(plan, match, manual):
                log.info("Final confirmation declined - nothing was attached")
                self._set_status("Not confirmed. Nothing was attached.")
                return
            self._execute_plan(manager, plan)

        def on_prepare_error(error: BaseException) -> None:
            if isinstance(error, DuplicateAttachmentError) and error.exact_duplicate:
                if self.ask_yes_no(f"{error}\n\nMark this PDF as processed so it is skipped in future scans?"):
                    manager.mark_already_attached(pdf, company, match.draft)
                    self.refresh_history()
                    self._reset_workflow()
                    self._set_status("PDF marked as already attached.")
                return
            if isinstance(error, DraftChangedError):
                self.show_warning(str(error) + "\n\nThe drafts are read again now.")
                self.load_drafts_and_match()
                return
            if isinstance(error, LocalFileChangedError):
                self.show_error(str(error))
                self._reset_workflow()
                return
            self._handle_error(error)

        self._run_task("Reading the draft and preparing the attachment...",
                       lambda: manager.prepare(pdf, company, match.draft), on_prepared, on_prepare_error)

    def _execute_plan(self, manager: AttachmentManager, plan: AttachPlan) -> None:
        dry_run = self.dry_run_active
        send = self.send_enabled
        if dry_run:
            status = "Dry run: checking (Gmail is not modified)..."
        else:
            status = "Attaching PDF and sending the e-mail..." if send else "Attaching PDF to the draft..."
        self._run_task(status, lambda: manager.execute(plan, dry_run=dry_run, send=send), self._on_attach_result)

    # ------------------------------------------------------------------
    # Automatic checking / sending
    # ------------------------------------------------------------------
    def _auto_tick(self) -> None:
        """Runs every few seconds: look for a new PDF when nothing else is going on."""
        try:
            if (self.auto_check_var.get() and not self.busy and self._modal_depth == 0
                    and self.gmail is not None and self.current_pdf is None):
                self._auto_scan()
        finally:
            self.after(self.config_obj.auto_check_interval_ms, self._auto_tick)

    def _auto_scan(self) -> None:
        folder = Path(self.folder_var.get())
        if not folder.is_dir():
            return
        scanner = self._make_scanner()
        seen = set(self._auto_seen)

        def on_result(result: ScanResult) -> None:
            if result.selected is None or self.current_pdf is not None:
                self._set_status(f"Watching {folder} for new PDFs...")
                return
            self._auto_seen.add(result.selected.sha256)
            log.info("Automatic check found %s", result.selected.name)
            self._select_pdf(result.selected, ["Found automatically."], auto=True)

        def on_error(error: BaseException) -> None:
            log.warning("Automatic check failed: %s", error)

        self._run_task("Checking for new PDFs...", lambda: scanner.scan(folder, exclude_hashes=seen),
                       on_result, on_error)

    def _auto_send_blocker(self) -> str | None:
        """None if the current match may be attached and sent without asking,
        otherwise the reason why the user must decide."""
        outcome = self.outcome
        if outcome is None or outcome.status != SINGLE_STRONG or outcome.proposed is None:
            return "there is no single clear match"
        if len(outcome.candidates) != 1:
            return "other drafts also mention this company"
        if self.parse_result is None or not self.parse_result.ok or self.company != self.parse_result.company:
            return "the company name was not read from the file name"
        if self.rejected_draft_ids or self.manual_match is not None:
            return "a draft was rejected or selected manually"
        if self.selected_match is not outcome.proposed:
            return "a different draft is selected"
        if not outcome.proposed.draft.to:
            return "the draft has no recipient"
        return None

    def _draw_attention(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
            self.bell()
        except tk.TclError:
            pass

    def _continue_automatically(self) -> None:
        """Called after an automatically found PDF was matched."""
        if not self.send_enabled:
            self._draw_attention()
            return
        blocker = self._auto_send_blocker()
        if blocker is not None:
            log.info("Not sending automatically: %s", blocker)
            self._set_banner("warn", (self.outcome.message if self.outcome else "") +
                             f"\n\nNot sent automatically because {blocker}. Please decide yourself.")
            self._draw_attention()
            return
        match = self.selected_match
        pdf, company = self.current_pdf, self.company
        manager = AttachmentManager(self.gmail, self.db)
        log.info("Automatic attach%s: %s -> draft %s", "" if self.dry_run_active else " and send",
                 pdf.name, match.draft.draft_id)

        def on_error(error: BaseException) -> None:
            self._draw_attention()
            if isinstance(error, DuplicateAttachmentError):
                self._set_banner("stop", f"{error}\nNothing was sent.")
                return
            self._handle_error(error)

        self._run_task("Preparing automatic attachment...",
                       lambda: manager.prepare(pdf, company, match.draft),
                       lambda plan: self._execute_plan(manager, plan), on_error)

    def _on_attach_result(self, result: AttachResult) -> None:
        self.last_result = result
        auto = self._auto_flow
        self.refresh_history()
        self._set_status(result.message.splitlines()[0])
        if result.status == STATUS_SUCCESS and result.send_status in (None, SEND_SENT):
            if not auto:
                self.show_info(result.message, title="Sent" if result.sent else "Attached")
            self._reset_workflow(keep_status=True)
            next_step = ("The next new PDF is picked up automatically." if self.auto_check_var.get()
                         else "Click 'Scan for newest PDF' for the next application.")
            self._set_banner("ok", result.message + "\n\n" + next_step)
        elif result.status == STATUS_SUCCESS:
            # Attached, but sending failed or its outcome is unclear.
            self._draw_attention()
            self.show_warning(result.message, title="E-mail not sent" if result.send_status == SEND_FAILED
                              else "Please check your Sent folder")
            self._reset_workflow(keep_status=True)
            self._set_banner("warn", result.message)
        elif result.status == STATUS_DRY_RUN:
            if auto:
                self._reset_workflow(keep_status=True)  # keep watching for the next PDF
            else:
                self.show_info(result.message + "\n\nTo attach for real, switch off dry-run mode and "
                               "confirm again.", title="Dry run")
            self._set_banner("info", result.message)
        elif result.status == STATUS_UNVERIFIED:
            self.show_warning(result.message, title="Please check the draft")
            self._reset_workflow(keep_status=True)
            self._set_banner("warn", result.message)
        else:
            self.show_error(result.message, title="Not attached")
            self._set_banner("stop", result.message)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def refresh_history(self) -> None:
        try:
            records = self.db.list_history(include_dry_runs=bool(self.show_dry_runs_var.get()))
        except Exception as exc:
            log.error("Could not read history: %s", exc)
            return
        previous = self._selected_history_record()
        self.history_tree.delete(*self.history_tree.get_children())
        self._history_items = {}
        reselect = None
        for record in records:
            item = self.history_tree.insert("", "end", values=(
                record.local_time(fmt="%Y-%m-%d %H:%M"), record.status_label, record.pdf_filename,
                record.company_name or "",
                record.draft_subject or "", record.draft_recipients or "", record.draft_id or "",
            ), tags=(record.status,))
            self._history_items[item] = record
            if previous is not None and record.id == previous.id:
                reselect = item
        children = self.history_tree.get_children()
        if reselect is None and children:
            reselect = children[0]  # show the details of the newest entry
        if reselect is not None:
            self.history_tree.selection_set(reselect)
        self._on_history_select()

    def _selected_history_record(self) -> HistoryRecord | None:
        selection = self.history_tree.selection()
        return self._history_items.get(selection[0]) if selection else None

    def _on_history_select(self, _event: Any = None) -> None:
        record = self._selected_history_record()
        resolvable = record is not None and record.status in INCOMPLETE_STATUSES
        self.mark_success_button.state(["!disabled"] if resolvable else ["disabled"])
        self.mark_failed_button.state(["!disabled"] if resolvable else ["disabled"])
        text = ""
        if record is not None:
            text = (f"Status: {record.status_label}\nPDF: {record.pdf_filename}\nPath: {record.pdf_path}\n"
                    f"SHA-256: {record.sha256}\nCompany: {record.company_name or ''}\n"
                    f"Draft: {record.draft_id or ''}  Subject: {record.draft_subject or ''}\n"
                    f"Recipient: {record.draft_recipients or ''}\n"
                    f"Started: {record.local_time('started_at')}  Finished: {record.local_time('finished_at')}\n"
                    f"Details: {record.error_message or ''}")
        self.history_details.configure(state="normal")
        self.history_details.delete("1.0", "end")
        self.history_details.insert("1.0", text)
        self.history_details.configure(state="disabled")

    def _resolve_selected(self, status: str) -> None:
        record = self._selected_history_record()
        if record is None or record.status not in INCOMPLETE_STATUSES:
            return
        if status == STATUS_SUCCESS:
            question = (f"Did you check in Gmail that '{record.pdf_filename}' IS attached to the draft "
                        f"'{record.draft_subject}'?\n\nThe PDF will then be treated as processed.")
            note = "Marked as attached by the user after checking Gmail."
        else:
            question = (f"Did you check in Gmail that '{record.pdf_filename}' is NOT attached to the draft "
                        f"'{record.draft_subject}'?\n\nThe PDF can then be processed again.")
            note = "Marked as not attached by the user after checking Gmail."
        if not self.ask_yes_no(question):
            return
        self.db.resolve_manually(record.id, status, note)
        self.refresh_history()

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _show_help(self) -> None:
        self.show_info(
            "1. Write your cover letter as a Gmail draft (recipient + subject).\n"
            "2. Save the application PDF in the Bewerbungen folder, named like\n"
            "   Amine_Haouam_Ausbildung_Elektroniker_<Company>.pdf\n"
            "3. Click 'Scan for newest PDF'. The newest unprocessed PDF is selected and the company "
            "name is read from the file name.\n"
            "4. The app searches your drafts for the company (subject, recipients, text).\n"
            "5. Review the proposed draft and confirm.\n\n"
            "Automatic checking: new PDFs are picked up without clicking Scan.\n"
            "Automatic sending (optional): if exactly one draft clearly matches, the PDF is attached "
            "and the e-mail is sent without asking. Unclear matches always wait for you.\n\n"
            "Dry-run mode shows what would happen without changing Gmail or sending anything.",
            title="How it works")

    def _show_about(self) -> None:
        self.show_info(
            f"{APP_NAME} {APP_VERSION}\n\nLocal, free tool that attaches application PDFs to existing "
            "Gmail drafts using the official Gmail API.\n\nIt sends e-mails only if you enable sending, never deletes drafts "
            f"and never modifies your PDF files.\n\nData folder:\n{self.paths.root}",
            title="About")

    def _on_close(self) -> None:
        if self.busy and not self.ask_yes_no(
                "An operation is still running. If you quit now, it will be checked the next time you "
                "connect Gmail.\n\nQuit anyway?", warning=True):
            return
        logging.getLogger().removeHandler(self._log_handler)
        self.worker.shutdown()
        self.destroy()

    # Test helper ---------------------------------------------------------
    def wait_until_idle(self, timeout: float = 10.0) -> None:
        """Pump the Tk event loop until no background task is pending."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.update()
            if not self.busy and self.worker.pending == 0:
                self.update()
                return
            time.sleep(0.01)
        raise TimeoutError("Background task did not finish in time")
