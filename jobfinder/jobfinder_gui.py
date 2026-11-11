"""
jobfinder_gui.py - Window of the Ausbildung Job Finder.

The browser work runs on one background thread (Playwright needs all calls
on the same thread); results come back to the Tk thread through a queue.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from alert_sound import play_attention, play_done
from ba_browser import BrowserNotAvailable, BrowserSession, RunEvents, RunSettings, StopRequested, run
from ba_parser import KNOWN_SPECIALTIES, OTHER_SPECIALTY, Job
from jobfinder_db import JobStore

log = logging.getLogger(__name__)

APP_NAME = "Ausbildung Job Finder"
APP_VERSION = "1.0.0"

C = {
    "bg": "#f3f5f8", "card": "#ffffff", "border": "#d8dee8", "text": "#1f2933", "muted": "#5f6b7a",
    "header": "#14532d", "header_sub": "#c9e8d2", "accent": "#2563eb", "accent_active": "#1d4ed8",
    "danger": "#b91c1c", "warn_bg": "#fff3dc", "warn_fg": "#7a4500", "info_bg": "#e8f0fe",
    "info_fg": "#1e3a8a", "disabled_bg": "#e5e9f0", "disabled_fg": "#98a2b3",
}
STATUS_TEXT = {
    "ok": "Kontakt ✓", "none": "kein Kontakt angegeben", "external": "Bewerbung über Partnerseite",
    "captcha-skipped": "Sicherheitsabfrage offen", "error": "Fehler beim Lesen", "": "",
}
RADIUS_CHOICES = ["ganz Deutschland", "10 km", "25 km", "50 km", "100 km", "200 km"]

DEFAULT_SETTINGS: dict[str, Any] = {
    "wo": "", "umkreis": "50 km", "max_jobs": 30, "want_contact": True, "skip_known": True,
    "specialties": KNOWN_SPECIALTIES + [OTHER_SPECIALTY],
}


def data_dir() -> Path:
    override = os.environ.get("JOBFINDER_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BewerbungJobFinder"
    else:
        base = Path.home() / ".local" / "share" / "BewerbungJobFinder"
    base.mkdir(parents=True, exist_ok=True)
    return base


def load_settings(path: Path) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            settings.update({k: v for k, v in loaded.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return settings


def _style(root: tk.Tk) -> None:
    import tkinter.font as tkfont
    family = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
    if family not in tkfont.families(root):
        family = tkfont.nametofont("TkDefaultFont").actual("family")
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
        tkfont.nametofont(name).configure(family=family, size=10)
    s = ttk.Style(root)
    s.theme_use("clam")
    root.configure(background=C["bg"])
    s.configure(".", background=C["bg"], foreground=C["text"], font=(family, 10))
    s.configure("Card.TLabelframe", background=C["card"], bordercolor=C["border"], relief="solid", padding=(12, 8))
    s.configure("Card.TLabelframe.Label", background=C["card"], foreground=C["header"], font=(family, 11, "bold"))
    s.configure("Card.TFrame", background=C["card"])
    s.configure("Card.TLabel", background=C["card"])
    s.configure("Card.TCheckbutton", background=C["card"])
    s.map("Card.TCheckbutton", background=[("active", C["card"])])
    s.configure("Header.TFrame", background=C["header"])
    s.configure("Title.TLabel", background=C["header"], foreground="#ffffff", font=(family, 16, "bold"))
    s.configure("Sub.TLabel", background=C["header"], foreground=C["header_sub"])
    s.configure("TButton", padding=(12, 6), background="#ffffff", bordercolor=C["border"])
    s.map("TButton", background=[("disabled", C["disabled_bg"]), ("active", "#eef2f7")],
          foreground=[("disabled", C["disabled_fg"])])
    for name, base, active in (("Accent", C["accent"], C["accent_active"]), ("Danger", C["danger"], "#991b1b")):
        s.configure(f"{name}.TButton", background=base, foreground="#ffffff", bordercolor=base,
                    font=(family, 10, "bold"))
        s.map(f"{name}.TButton", background=[("disabled", C["disabled_bg"]), ("active", active)],
              foreground=[("disabled", C["disabled_fg"])])
    s.configure("Info.TLabel", background=C["info_bg"], foreground=C["info_fg"], padding=(10, 7))
    s.configure("Warn.TLabel", background=C["warn_bg"], foreground=C["warn_fg"], padding=(10, 7),
                font=(family, 10, "bold"))
    s.configure("Treeview", rowheight=24)
    s.configure("Treeview.Heading", font=(family, 10, "bold"))
    s.map("Treeview", background=[("selected", "#dbe7fd")], foreground=[("selected", C["text"])])


class CaptchaAlert(tk.Toplevel):
    """Always-on-top notice while the user solves the Sicherheitsabfrage."""

    def __init__(self, parent: "JobFinderApp", job: Job):
        super().__init__(parent)
        self.title("Sicherheitsabfrage")
        self.configure(background=C["card"])
        self.attributes("-topmost", True)
        self.resizable(False, False)
        frame = ttk.Frame(self, style="Card.TFrame", padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Bitte Sicherheitsabfrage lösen", style="Card.TLabel",
                  font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(frame, style="Card.TLabel", wraplength=420, justify="left", text=(
            f"Für die Kontaktdaten von „{job.company}“ fragt die Jobsuche nach den Zeichen aus dem Bild.\n\n"
            "Das Browserfenster ist jetzt vorne: Zeichen eingeben und „Absenden“ klicken. "
            "Danach geht es automatisch weiter.")).pack(anchor="w", pady=(8, 12))
        buttons = ttk.Frame(frame, style="Card.TFrame")
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Diese Stelle ohne Kontakt überspringen",
                   command=parent.skip_captcha).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", parent.skip_captcha)
        # Bottom-right corner of the screen, away from the captcha in the browser.
        self.update_idletasks()
        x = self.winfo_screenwidth() - self.winfo_reqwidth() - 30
        y = self.winfo_screenheight() - self.winfo_reqheight() - 90
        self.geometry(f"+{max(0, x)}+{max(0, y)}")


class JobFinderApp(tk.Tk):
    POLL_MS = 80

    def __init__(self, home: Path | None = None, *, browser_options: dict | None = None,
                 alert: Callable[[Callable], None] = play_attention,
                 done_sound: Callable[[Callable], None] = play_done):
        super().__init__()
        self.home = home or data_dir()
        self.settings_path = self.home / "settings.json"
        self.settings = load_settings(self.settings_path)
        self.store = JobStore(self.home / "jobs.sqlite3")
        self.browser_options = browser_options or {}
        self.alert_sound, self.done_sound = alert, done_sound
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.skip_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.captcha_window: CaptchaAlert | None = None
        self._captcha_repeat: str | None = None
        self._items: dict[str, Job] = {}
        self.last_summary: dict | None = None

        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1320x860")
        self.minsize(1000, 680)
        _style(self)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self.reload_results()
        self.after(self.POLL_MS, self._poll)

    # -- layout ---------------------------------------------------------------
    def _build(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(20, 12))
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, style="Sub.TLabel", text="Elektroniker/in – alle Fachrichtungen · "
                  "Jobsuche der Bundesagentur für Arbeit · Ausbildung").pack(anchor="w")

        self.status = ttk.Label(self, text="Bereit.", style="Info.TLabel")
        self.status.pack(fill="x", side="bottom")

        body = ttk.Frame(self, padding=(12, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)

        # Search settings
        search = ttk.Labelframe(body, text="Suche", style="Card.TLabelframe")
        search.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.wo_var = tk.StringVar(value=self.settings["wo"])
        self.radius_var = tk.StringVar(value=self.settings["umkreis"])
        self.max_var = tk.IntVar(value=int(self.settings["max_jobs"]))
        self.contact_var = tk.BooleanVar(value=bool(self.settings["want_contact"]))
        self.skip_var = tk.BooleanVar(value=bool(self.settings["skip_known"]))
        rows = [("Ort / PLZ (leer = ganz Deutschland):", ttk.Entry(search, textvariable=self.wo_var, width=26)),
                ("Umkreis:", ttk.Combobox(search, textvariable=self.radius_var, values=RADIUS_CHOICES,
                                          state="readonly", width=24)),
                ("Max. Stellen pro Suchlauf:", ttk.Spinbox(search, from_=1, to=500, textvariable=self.max_var,
                                                         width=8))]
        for row, (label, widget) in enumerate(rows):
            ttk.Label(search, text=label, style="Card.TLabel").grid(row=row * 2, column=0, sticky="w")
            widget.grid(row=row * 2 + 1, column=0, sticky="w", pady=(0, 8))
        ttk.Checkbutton(search, text="Kontaktdaten abrufen\n(manchmal mit Sicherheitsabfrage)",
                        variable=self.contact_var, style="Card.TCheckbutton").grid(row=6, column=0, sticky="w")
        ttk.Checkbutton(search, text="Bereits gesammelte Stellen überspringen",
                        variable=self.skip_var, style="Card.TCheckbutton").grid(row=7, column=0, sticky="w",
                                                                                  pady=(4, 10))
        self.start_button = ttk.Button(search, text="Suche starten", style="Accent.TButton", command=self.start)
        self.start_button.grid(row=8, column=0, sticky="ew")
        self.stop_button = ttk.Button(search, text="Stopp", style="Danger.TButton", command=self.stop)
        self.stop_button.grid(row=9, column=0, sticky="ew", pady=(6, 0))
        self.stop_button.state(["disabled"])

        # Specialties
        spec = ttk.Labelframe(body, text="Fachrichtungen (Beruf: Elektroniker/in)", style="Card.TLabelframe")
        spec.grid(row=0, column=1, sticky="nsew")
        self.spec_vars: dict[str, tk.BooleanVar] = {}
        self.spec_checks: dict[str, ttk.Checkbutton] = {}
        selected = set(self.settings["specialties"])
        for index, name in enumerate(KNOWN_SPECIALTIES + [OTHER_SPECIALTY]):
            var = tk.BooleanVar(value=name in selected)
            label = "Andere / ohne Fachrichtung" if name == OTHER_SPECIALTY else name
            check = ttk.Checkbutton(spec, text=label, variable=var, style="Card.TCheckbutton")
            check.grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 10), pady=1)
            self.spec_vars[name], self.spec_checks[name] = var, check
        links = ttk.Frame(spec, style="Card.TFrame")
        links.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(links, text="Alle", command=lambda: self._set_specs(True)).pack(side="left")
        ttk.Button(links, text="Keine", command=lambda: self._set_specs(False)).pack(side="left", padx=(6, 0))
        ttk.Label(links, style="Card.TLabel", foreground=C["muted"],
                  text="  Nicht enthalten: IT-System-Elektroniker/in, Industrieelektriker/in u. ä.").pack(side="left")

        # Results
        results = ttk.Labelframe(body, text="Gefundene Stellen", style="Card.TLabelframe")
        results.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        results.columnconfigure(0, weight=3)
        results.columnconfigure(2, weight=2)
        results.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(results, style="Card.TFrame")
        toolbar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="Stelle im Browser öffnen", command=self.open_selected).pack(side="left")
        ttk.Button(toolbar, text="E-Mail kopieren", command=self.copy_email).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Als Excel/CSV speichern", command=self.export).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Liste leeren", command=self.clear).pack(side="right")
        self.count_label = ttk.Label(toolbar, style="Card.TLabel", foreground=C["muted"])
        self.count_label.pack(side="right", padx=10)
        columns = (("found", "Gefunden", 140), ("specialty", "Fachrichtung", 160), ("company", "Firma", 190),
                   ("ort", "Ort", 110), ("email", "E-Mail", 190), ("status", "Kontakt", 150))
        self.tree = ttk.Treeview(results, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for key, title, width in columns:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, anchor="w", stretch=key in ("company", "email"))
        self.tree.tag_configure("captcha", foreground="#9a3412")
        self.tree.tag_configure("new", background="#f0fdf4")
        scroll = ttk.Scrollbar(results, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_details())
        self.tree.bind("<Double-1>", lambda _e: self.open_selected())
        self.details = tk.Text(results, width=40, wrap="word", relief="flat", background="#fbfcfe",
                               padx=10, pady=8)
        self.details.grid(row=1, column=2, sticky="nsew", padx=(10, 0))
        self.details.configure(state="disabled")

    def _set_specs(self, value: bool) -> None:
        for var in self.spec_vars.values():
            var.set(value)

    def _set_status(self, text: str, warn: bool = False) -> None:
        self.status.configure(text=text, style="Warn.TLabel" if warn else "Info.TLabel")

    # -- results --------------------------------------------------------------
    def reload_results(self, highlight: set[str] | None = None) -> None:
        self.tree.delete(*self.tree.get_children())
        self._items = {}
        jobs = self.store.all()
        for job, first_seen in jobs:
            self._insert(job, first_seen, new=bool(highlight and job.refnr in highlight))
        self.count_label.configure(text=f"{len(jobs)} Stellen gespeichert")

    def _insert(self, job: Job, first_seen: str, new: bool = False, index: int | str = "end") -> None:
        tags = []
        if job.contact_status == "captcha-skipped":
            tags.append("captcha")
        if new:
            tags.append("new")
        item = self.tree.insert("", index, values=(
            first_seen.replace("T", " ")[:16], job.specialty or "–", job.company, job.ort,
            ", ".join(job.emails), STATUS_TEXT.get(job.contact_status, job.contact_status),
        ), tags=tags)
        self._items[item] = job

    def selected_job(self) -> Job | None:
        selection = self.tree.selection()
        return self._items.get(selection[0]) if selection else None

    def _show_details(self) -> None:
        job = self.selected_job()
        text = ""
        if job:
            address = ", ".join(x for x in (job.street, f"{job.plz} {job.ort}".strip()) if x)
            lines = [job.title, f"{job.company} – {address}", f"Beruf: {job.beruf}",
                     f"Beginn: {job.start}   Schulabschluss: {job.education or '–'}"]
            if job.emails or job.phones:
                lines.append("E-Mail: " + ", ".join(job.emails) + "   Telefon: " + ", ".join(job.phones))
            if job.homepage:
                lines.append(f"Homepage: {job.homepage}")
            if job.external_url:
                lines.append(f"Bewerbung über Partnerseite: {job.external_url}")
            if job.contact_text:
                lines += ["", "Kontakt:", job.contact_text]
            if job.description:
                lines += ["", job.description[:1500] + ("…" if len(job.description) > 1500 else "")]
            lines += ["", job.url]
            text = "\n".join(lines)
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def open_selected(self) -> None:
        job = self.selected_job()
        if job:
            webbrowser.open(job.url)

    def copy_email(self) -> None:
        job = self.selected_job()
        if job and job.emails:
            self.clipboard_clear()
            self.clipboard_append(job.emails[0])
            self._set_status(f"E-Mail kopiert: {job.emails[0]}")
        else:
            self._set_status("Für diese Stelle ist keine E-Mail-Adresse bekannt.")

    def export(self) -> None:
        name = f"Elektroniker_Ausbildung_{datetime.now():%Y-%m-%d}.csv"
        target = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", initialfile=name,
                                              filetypes=[("CSV (Excel)", "*.csv")])
        if target:
            count = self.store.export_csv(Path(target))
            self._set_status(f"{count} Stellen gespeichert in {target}")

    def clear(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if messagebox.askyesno(APP_NAME, "Alle gespeicherten Stellen löschen?", parent=self):
            self.store.delete_all()
            self.reload_results()

    # -- running a search ---------------------------------------------------
    def current_settings(self) -> RunSettings:
        radius = self.radius_var.get()
        umkreis = None if radius.startswith("ganz") else int(radius.split()[0])
        return RunSettings(
            wo=self.wo_var.get().strip(), umkreis=umkreis if self.wo_var.get().strip() else None,
            specialties={name for name, var in self.spec_vars.items() if var.get()},
            max_jobs=max(1, int(self.max_var.get() or 30)), want_contact=bool(self.contact_var.get()),
            skip_known=bool(self.skip_var.get()))

    def _save_settings(self, settings: RunSettings) -> None:
        data = {"wo": settings.wo, "umkreis": self.radius_var.get(), "max_jobs": settings.max_jobs,
                "want_contact": settings.want_contact, "skip_known": settings.skip_known,
                "specialties": sorted(settings.specialties)}
        try:
            self.settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        settings = self.current_settings()
        if not settings.specialties:
            messagebox.showinfo(APP_NAME, "Bitte mindestens eine Fachrichtung auswählen.", parent=self)
            return
        self._save_settings(settings)
        self.stop_event.clear()
        self.skip_event.clear()
        self.new_refs: set[str] = set()
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self._set_status("Browser wird gestartet...")
        self.worker = threading.Thread(target=self._work, args=(settings,), daemon=True, name="jobfinder")
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.skip_event.set()
        self._set_status("Wird angehalten...")

    def skip_captcha(self) -> None:
        self.skip_event.set()
        self._close_captcha()

    def _post(self, kind: str, *payload: Any) -> None:
        self.events.put((kind, payload))

    def _work(self, settings: RunSettings) -> None:
        """Background thread: the whole browser session."""
        session = BrowserSession(self.home / "browser-profile", stop_event=self.stop_event,
                                 skip_event=self.skip_event, **self.browser_options)
        events = RunEvents(
            status=lambda text: self._post("status", text),
            job=lambda job: self._post("job", job),
            specialties=lambda counts: self._post("specialties", counts),
            captcha=lambda job: self._post("captcha", job),
            captcha_done=lambda job, solved: self._post("captcha_done", job, solved),
        )
        try:
            session.start()
            self._post("status", f"Browser gestartet ({session.browser_name}).")
            summary = run(session, settings, known=self.store.is_complete, save=self.store.save, events=events)
            self._post("done", summary)
        except StopRequested:
            self._post("done", {"stopped": True})
        except BrowserNotAvailable as exc:
            self._post("error", str(exc))
        except Exception as exc:
            log.exception("Search failed")
            self._post("error", f"{type(exc).__name__}: {exc}")
        finally:
            session.close()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                getattr(self, f"_on_{kind}")(*payload)
        except queue.Empty:
            pass
        finally:
            self.after(self.POLL_MS, self._poll)

    def _on_status(self, text: str) -> None:
        self._set_status(text)

    def _on_job(self, job: Job) -> None:
        self.new_refs.add(job.refnr)
        self.reload_results(highlight=self.new_refs)

    def _on_specialties(self, counts: dict) -> None:
        for name, check in self.spec_checks.items():
            base = "Andere / ohne Fachrichtung" if name == OTHER_SPECIALTY else name
            check.configure(text=f"{base} ({counts[name]})" if counts.get(name) else base)

    def _on_captcha(self, job: Job) -> None:
        self._set_status(f"Sicherheitsabfrage für {job.company}: bitte im Browser lösen.", warn=True)
        self._close_captcha()
        self.captcha_window = CaptchaAlert(self, job)
        self._remind()
        self._flash_taskbar()

    def _remind(self) -> None:
        """Sound now and every 30 s while the Sicherheitsabfrage is open."""
        self.alert_sound(self.bell)
        self._captcha_repeat = self.after(30000, self._remind)

    def _on_captcha_done(self, job: Job, solved: bool) -> None:
        self._close_captcha()
        self._set_status("Sicherheitsabfrage gelöst – weiter geht's." if solved
                         else f"Kontaktdaten von {job.company} übersprungen.")

    def _close_captcha(self) -> None:
        if self._captcha_repeat:
            self.after_cancel(self._captcha_repeat)
            self._captcha_repeat = None
        if self.captcha_window is not None:
            try:
                self.captcha_window.destroy()
            except tk.TclError:
                pass
            self.captcha_window = None

    def _flash_taskbar(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.wm_frame(), 16)
            ctypes.windll.user32.FlashWindow(hwnd, True)
        except Exception:
            pass

    def _finish(self) -> None:
        self._close_captcha()
        self.start_button.state(["!disabled"])
        self.stop_button.state(["disabled"])

    def _on_done(self, summary: dict) -> None:
        self._finish()
        self.last_summary = summary
        if summary.get("stopped"):
            self._set_status("Angehalten.")
            return
        self._set_status(f"Fertig: {summary['opened']} Stellen geöffnet, {summary['found']} passende gefunden, "
                         f"{summary['skipped']} bereits bekannt übersprungen.")
        self.done_sound(None)

    def _on_error(self, message: str) -> None:
        self._finish()
        self._set_status("Fehler – siehe Meldung.", warn=True)
        messagebox.showerror(APP_NAME, message, parent=self)

    def on_close(self) -> None:
        self.stop()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5)
        self.destroy()
