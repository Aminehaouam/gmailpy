"""
jobfinder_db.py - Collected jobs (SQLite) and CSV export.

Every job is stored with its reference number, so the next run can skip jobs
that were already collected.  Jobs whose contact details could not be read
(captcha not solved) are opened again next time.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from ba_parser import Job

_JOB_FIELDS = {f.name for f in fields(Job)}

# Export columns (everything from the BA listing).
CSV_COLUMNS = [
    ("first_seen", "Gefunden am"), ("company", "Firma"), ("specialty", "Fachrichtung"),
    ("title", "Stellentitel"), ("beruf", "Ausbildungsberuf"),
    ("contact_person", "Ansprechpartner"), ("contact_position", "Position"),
    ("emails", "E-Mail"), ("email_source", "E-Mail gefunden in"), ("phones", "Telefon"),
    ("homepage", "Firmenwebsite"), ("contact_address", "Kontaktadresse"),
    ("street", "Straße"), ("plz", "PLZ"), ("ort", "Ort"), ("region", "Bundesland"),
    ("locations", "Alle Arbeitsorte"), ("start", "Beginn"), ("pay", "Ausbildungsvergütung"),
    ("pay_note", "Vergütungsangabe"), ("working_time", "Arbeitszeit"), ("contract", "Vertragsdauer"),
    ("education", "Schulabschluss"), ("application_methods", "Bewerbung"),
    ("application_url", "Online-Bewerbung"), ("required_documents", "Geforderte Unterlagen"),
    ("external_url", "Bewerbung über Partnerseite"), ("partner_name", "Partner"),
    ("other_links", "Weitere Links"), ("social_links", "Social Media"),
    ("employer_contact", "Kontakt im Arbeitgeberprofil"), ("company_size", "Betriebsgröße"),
    ("founded", "Gründungsjahr"), ("headquarters", "Hauptsitz"), ("benefits", "Mitarbeitervorteile"),
    ("employer_description", "Über den Arbeitgeber"), ("published", "Veröffentlicht"),
    ("changed", "Geändert"), ("chiffre", "Chiffre"), ("refnr", "Referenznummer"), ("url", "Link zur Stelle"),
    ("contact_status", "Kontaktstatus"), ("description", "Stellenbeschreibung"),
]


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                refnr TEXT PRIMARY KEY, data TEXT NOT NULL,
                first_seen TEXT NOT NULL, updated TEXT NOT NULL)""")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=15)

    def is_complete(self, refnr: str) -> bool:
        """Known and nothing left to fetch (contact read, none, or partner job)."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE refnr = ?", (refnr,)).fetchone()
        if row is None:
            return False
        status = json.loads(row[0]).get("contact_status", "")
        return status in ("ok", "none", "external")

    def save(self, job: Job) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        data = json.dumps(asdict(job), ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO jobs (refnr, data, first_seen, updated) VALUES (?, ?, ?, ?)
                   ON CONFLICT(refnr) DO UPDATE SET data = excluded.data, updated = excluded.updated""",
                (job.refnr, data, now, now))

    def all(self) -> list[tuple[Job, str]]:
        """All jobs, newest first, with the date they were first found."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT data, first_seen FROM jobs ORDER BY first_seen DESC, rowid DESC").fetchall()
        result = []
        for data, first_seen in rows:
            values = {k: v for k, v in json.loads(data).items() if k in _JOB_FIELDS}
            result.append((Job(**values), first_seen))
        return result

    def delete_all(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM jobs")

    def export_csv(self, target: Path) -> int:
        """Excel-friendly CSV (semicolon separated, UTF-8 with BOM)."""
        jobs = self.all()
        with open(target, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow([title for _, title in CSV_COLUMNS])
            for job, first_seen in jobs:
                row = []
                for key, _ in CSV_COLUMNS:
                    if key == "first_seen":
                        value = first_seen.replace("T", " ")
                    elif key == "pay":
                        value = job.pay_text()
                    else:
                        value = getattr(job, key)
                    if isinstance(value, list):
                        value = " | ".join(str(v) for v in value)
                    row.append(value)
                writer.writerow(row)
        return len(jobs)

    def export_json(self, target: Path) -> int:
        """All data as JSON (convenient for further processing, e.g. by Claude)."""
        jobs = self.all()
        data = []
        for job, first_seen in jobs:
            item = asdict(job)
            item.update(first_seen=first_seen, url=job.url, pay=job.pay_text())
            data.append(item)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(jobs)
