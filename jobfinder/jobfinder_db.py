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


# How far a job's contact details got (the better record wins when merging).
_STATUS_RANK = {"ok": 4, "none": 3, "external": 3, "captcha-skipped": 1}
COMPLETE_STATUSES = ("ok", "none", "external")


def _filled(job: Job) -> int:
    return sum(1 for value in asdict(job).values() if value)


def merge_jobs(new: Job, old: Job, *, by_amount: bool = False) -> Job:
    """One record from two records of the same job.

    The record with the better contact status wins (on a tie: *new*, or with
    by_amount the one with more data); its empty fields are filled from the
    other one, and the e-mail addresses and phone numbers of both are kept.
    """
    def key(job: Job) -> tuple:
        return (_STATUS_RANK.get(job.contact_status, 0), _filled(job) if by_amount else 0)
    first, second = (new, old) if key(new) >= key(old) else (old, new)
    data, other = asdict(first), asdict(second)
    for name, value in other.items():
        if name in ("emails", "phones"):
            seen = {str(v).casefold() for v in data[name]}
            data[name] = list(data[name]) + [v for v in value if str(v).casefold() not in seen]
        elif not data[name] and value:
            data[name] = value
    return Job(**data)


def _job_from_json(data: str) -> Job:
    return Job(**{k: v for k, v in json.loads(data).items() if k in _JOB_FIELDS})


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
        return status in COMPLETE_STATUSES

    def save(self, job: Job) -> None:
        """Store a job that was just read (merged with what was known: a
        failed second visit does not wipe contact details read before)."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE refnr = ?", (job.refnr,)).fetchone()
            if row is not None:
                job = merge_jobs(job, _job_from_json(row[0]))
            conn.execute(
                """INSERT INTO jobs (refnr, data, first_seen, updated) VALUES (?, ?, ?, ?)
                   ON CONFLICT(refnr) DO UPDATE SET data = excluded.data, updated = excluded.updated""",
                (job.refnr, json.dumps(asdict(job), ensure_ascii=False), now, now))

    def first_seen(self, refnr: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT first_seen FROM jobs WHERE refnr = ?", (refnr,)).fetchone()
        return row[0] if row else None

    def get(self, refnr: str) -> Job | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE refnr = ?", (refnr,)).fetchone()
        return _job_from_json(row[0]) if row else None

    def import_jobs(self, items: list[tuple[Job, str]]) -> dict[str, int]:
        """Add jobs read from saved files, keeping the date they were first
        found.  Jobs already in the list are merged.  One transaction."""
        counts = {"new": 0, "updated": 0, "unchanged": 0}
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            for job, first_seen in items:
                first_seen = first_seen or now
                row = conn.execute("SELECT data, first_seen FROM jobs WHERE refnr = ?", (job.refnr,)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO jobs (refnr, data, first_seen, updated) VALUES (?, ?, ?, ?)",
                                 (job.refnr, json.dumps(asdict(job), ensure_ascii=False), first_seen, now))
                    counts["new"] += 1
                    continue
                old = _job_from_json(row[0])
                merged = merge_jobs(job, old, by_amount=True)
                earliest = min(first_seen, row[1])
                if merged == old and earliest == row[1]:
                    counts["unchanged"] += 1
                    continue
                conn.execute("UPDATE jobs SET data = ?, first_seen = ?, updated = ? WHERE refnr = ?",
                             (json.dumps(asdict(merged), ensure_ascii=False), earliest, now, job.refnr))
                counts["updated"] += 1
        return counts

    def all(self) -> list[tuple[Job, str]]:
        """All jobs, newest first, with the date they were first found."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT data, first_seen FROM jobs ORDER BY first_seen DESC, rowid DESC").fetchall()
        return [(_job_from_json(data), first_seen) for data, first_seen in rows]

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
