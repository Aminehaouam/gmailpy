"""
jobfinder_import.py - Fill the list again from files saved earlier.

Reads the files made with "Als Excel/CSV speichern":

* the CSV file itself (also after opening it in Excel and saving it again
  as CSV - ANSI or UTF-8, semicolon, comma or tab separated),
* the same list saved by Excel as an .xlsx workbook (read with the standard
  library, no extra package),
* the JSON export.

Files of every earlier version of the app are understood (their column
titles differ slightly).  The same job in several files becomes one entry:
the record with the contact details wins, gaps are filled from the others.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ba_parser import Job, LinkError, classify_link, specialty_of
from jobfinder_db import COMPLETE_STATUSES, CSV_COLUMNS, merge_jobs

FILE_TYPES = (".csv", ".txt", ".xlsx", ".xlsm", ".json")

_JOB_FIELDS = {f.name: f for f in fields(Job)}
_LIST_FIELDS = {name for name, f in _JOB_FIELDS.items()
                if f.default_factory is list and name != "pay_per_year"}  # type: ignore[comparison-overlap]
_DATE_FIELDS = ("published", "changed", "start")
_STATUSES = {"", "ok", "none", "external", "captcha-skipped", "error"}


class ImportFileError(ValueError):
    """A file that cannot be read as a saved job list."""


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


# Column title -> (field, priority).  When two columns fill the same field,
# the lower priority number wins.
_HEADERS: dict[str, tuple[str, int]] = {_norm(title): (key, 0) for key, title in CSV_COLUMNS}
_HEADERS.update({
    _norm("Homepage"): ("homepage", 0),        # first version
    _norm("Homepage (BA)"): ("homepage", 0),   # version with web search
    # In the version with web search "Firmenwebsite" was a site found on the
    # web; there it only fills an empty homepage.
    _norm("Firmenwebsite"): ("homepage", 1),
})
for _name in list(_JOB_FIELDS) + ["first_seen", "url", "pay"]:   # JSON field names as titles
    _HEADERS.setdefault(_norm(_name), (_name, 2))


# --------------------------------------------------------------------------
# Reading the files
# --------------------------------------------------------------------------

def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _csv_rows(path: Path) -> list[list[str]]:
    text = _decode(path.read_bytes())
    header = text.split("\n", 1)[0]
    delimiter = max(";,\t", key=header.count)
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _column_index(ref: str) -> int:
    index = 0
    for char in ref:
        if not char.isalpha():
            break
        index = index * 26 + ord(char.upper()) - 64
    return index - 1


def _first_sheet(zf: zipfile.ZipFile) -> str:
    try:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        sheet = workbook.find(f"{_M}sheets/{_M}sheet")
        rel_id = sheet.get(f"{_REL}id") if sheet is not None else None
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.iter(f"{_PKG_REL}Relationship"):
            if rel.get("Id") == rel_id:
                target = rel.get("Target", "")
                return target.lstrip("/") if target.startswith("/") else "xl/" + target
    except (KeyError, ET.ParseError):
        pass
    return "xl/worksheets/sheet1.xml"


def _xlsx_rows(path: Path) -> list[list[str]]:
    try:
        with zipfile.ZipFile(path) as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                for item in ET.fromstring(zf.read("xl/sharedStrings.xml")).iter(f"{_M}si"):
                    parts = item.findall(f"{_M}t") + item.findall(f"{_M}r/{_M}t")
                    shared.append("".join(t.text or "" for t in parts))
            sheet = ET.fromstring(zf.read(_first_sheet(zf)))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ImportFileError(f"{path.name}: keine lesbare Excel-Datei ({exc}).") from exc
    rows: list[list[str]] = []
    for row in sheet.iter(f"{_M}row"):
        cells: dict[int, str] = {}
        for cell in row.findall(f"{_M}c"):
            ref = cell.get("r")
            column = _column_index(ref) if ref else (max(cells) + 1 if cells else 0)
            kind = cell.get("t", "n")
            value = cell.find(f"{_M}v")
            if kind == "s":
                text = shared[int(value.text)] if value is not None and value.text else ""
            elif kind == "inlineStr":
                inline = cell.find(f"{_M}is")
                text = "".join(t.text or "" for t in inline.iter(f"{_M}t")) if inline is not None else ""
            else:
                text = value.text if value is not None and value.text is not None else ""
            cells[column] = text
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

def _excel_serial(value: str) -> datetime | None:
    """Excel stores dates as day numbers (e.g. 46338.35 = 12.11.2026 08:24)."""
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        return None
    if 20000 <= number <= 80000:
        return datetime(1899, 12, 30) + timedelta(days=number)
    return None


_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                 "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y")


def _timestamp(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    moment = _excel_serial(value)
    if moment is None:
        for fmt in _TIME_FORMATS:
            try:
                moment = datetime.strptime(value[:19], fmt)
                break
            except ValueError:
                continue
    if moment is None:
        return None
    return moment.replace(microsecond=0).isoformat(timespec="seconds")


def _date_text(value: str) -> str:
    """Dates Excel turned into day numbers or German dates -> as exported."""
    value = value.strip()
    moment = _excel_serial(value)
    if moment is not None:
        whole = moment.hour == moment.minute == moment.second == 0
        return moment.date().isoformat() if whole else moment.replace(microsecond=0).isoformat()
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value)
    if match:
        day, month, year = (int(g) for g in match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    return value


def _split(name: str, value: str) -> list[str]:
    if name == "emails":
        parts = re.split(r"[\s,;|]+", value)
    elif " | " in value or name != "phones":
        parts = value.split(" | ")
    else:                       # older versions: separated by ", "
        parts = re.split(r"\s*[,;\n]\s*", value)
    result: list[str] = []
    for part in (p.strip() for p in parts):
        if part and part.casefold() not in {r.casefold() for r in result}:
            result.append(part)
    return result


def _pay(value: str) -> list[int]:
    years = re.findall(r"(\d+)\.\s*Jahr\s*([\d.]+)", value)
    return [int(amount.replace(".", "")) for _year, amount in sorted(years, key=lambda y: int(y[0]))
            if amount.replace(".", "").isdigit()]


def _plz(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d{3,5}(\.0+)?", value):     # Excel dropped the leading zero
        return value.split(".")[0].zfill(5)
    return value


def _refnr(values: dict[str, Any]) -> str:
    for key in ("refnr", "url"):
        text = str(values.get(key) or "").strip()
        if not text:
            continue
        try:
            kind, value = classify_link(text)
        except LinkError:
            continue
        if kind == "job":
            return value
    return ""


def _job(values: dict[str, Any], has_status: bool) -> tuple[Job, str] | None:
    """A job from the values of one row (field name -> value)."""
    refnr = _refnr(values)
    if not refnr:
        return None
    data: dict[str, Any] = {}
    for name, value in values.items():
        if name not in _JOB_FIELDS or name == "refnr":
            continue
        if name == "pay_per_year":
            if isinstance(value, list):
                data[name] = [int(v) for v in value if str(v).isdigit()]
        elif name in _LIST_FIELDS:
            data[name] = _split(name, value) if isinstance(value, str) else [str(v) for v in value or []]
        elif name == "supported_by_ba":
            data[name] = value if isinstance(value, bool) else _norm(value) in ("1", "true", "wahr", "ja", "yes")
        else:
            data[name] = "" if value is None else str(value).strip()
    if "pay_per_year" not in data and values.get("pay"):
        data["pay_per_year"] = _pay(str(values["pay"]))
    if data.get("plz"):
        data["plz"] = _plz(data["plz"])
    for name in _DATE_FIELDS:
        if data.get(name):
            data[name] = _date_text(data[name])
    if not data.get("specialty") and data.get("beruf"):
        data["specialty"] = specialty_of(data["beruf"])
    status = data.get("contact_status", "")
    if not has_status:          # first versions did not save the status
        if data.get("contact_person") or data.get("emails") or data.get("phones"):
            status = "ok"
        elif data.get("external_url"):
            status = "external"
    data["contact_status"] = status if status in _STATUSES else ""
    for name in ("title", "beruf", "specialty", "company"):
        data.setdefault(name, "")
    first_seen = _timestamp(str(values.get("first_seen") or "")) or ""
    return Job(refnr=refnr, **data), first_seen


def _jobs_from_rows(rows: list[list[str]], name: str) -> tuple[list[tuple[Job, str]], int]:
    header, columns = None, []
    for index, row in enumerate(rows[:15]):
        mapped = [_HEADERS.get(_norm(cell)) for cell in row]
        keys = {m[0] for m in mapped if m}
        if len(keys) >= 3 and keys & {"refnr", "url"}:
            header, columns = index, mapped
            break
    if header is None:
        raise ImportFileError(f"{name}: keine gespeicherte Stellenliste (es fehlen Spalten wie "
                              "„Referenznummer“ oder „Link zur Stelle“).")
    has_status = any(m and m[0] == "contact_status" for m in columns)
    jobs, unreadable = [], 0
    for row in rows[header + 1:]:
        best: dict[str, tuple[int, str]] = {}
        for cell, mapped in zip(row, columns):
            text = str(cell).strip()
            if not mapped or not text:
                continue
            key, priority = mapped
            if key not in best or priority < best[key][0]:
                best[key] = (priority, text)
        if not best:
            continue                      # empty row
        job = _job({k: v for k, (_p, v) in best.items()}, has_status)
        if job is None:
            unreadable += 1
        else:
            jobs.append(job)
    return jobs, unreadable


def _jobs_from_json(path: Path) -> tuple[list[tuple[Job, str]], int]:
    try:
        items = json.loads(_decode(path.read_bytes()))
    except ValueError as exc:
        raise ImportFileError(f"{path.name}: keine lesbare JSON-Datei ({exc}).") from exc
    if not isinstance(items, list):
        raise ImportFileError(f"{path.name}: keine gespeicherte Stellenliste.")
    jobs, unreadable = [], 0
    for item in items:
        job = _job(item, has_status="contact_status" in item) if isinstance(item, dict) else None
        if job is None:
            unreadable += 1
        else:
            jobs.append(job)
    return jobs, unreadable


def read_file(path: Path) -> tuple[list[tuple[Job, str]], int]:
    """(jobs with the date first found, number of rows without a job)."""
    suffix = path.suffix.lower()
    if suffix not in FILE_TYPES:
        raise ImportFileError(f"{path.name}: Dateityp {suffix or '?'} wird nicht unterstützt "
                              "(CSV, Excel .xlsx oder JSON).")
    try:
        if suffix == ".json":
            return _jobs_from_json(path)
        rows = _xlsx_rows(path) if suffix in (".xlsx", ".xlsm") else _csv_rows(path)
    except OSError as exc:
        raise ImportFileError(f"{path.name}: kann nicht gelesen werden ({exc}).") from exc
    return _jobs_from_rows(rows, path.name)


# --------------------------------------------------------------------------
# Several files
# --------------------------------------------------------------------------

@dataclass
class ImportResult:
    files: int = 0
    rows: int = 0
    unreadable: int = 0
    errors: list[str] = field(default_factory=list)
    jobs: dict[str, tuple[Job, str]] = field(default_factory=dict)

    @property
    def duplicates(self) -> int:
        return self.rows - len(self.jobs)

    @property
    def complete(self) -> int:
        return sum(1 for job, _ in self.jobs.values() if job.contact_status in COMPLETE_STATUSES)


def read_files(paths: list[Path]) -> ImportResult:
    """All jobs of the files; the same job in several files becomes one."""
    result = ImportResult()
    for path in paths:
        try:
            jobs, unreadable = read_file(Path(path))
        except ImportFileError as exc:
            result.errors.append(str(exc))
            continue
        result.files += 1
        result.rows += len(jobs)
        result.unreadable += unreadable
        for job, first_seen in jobs:
            known = result.jobs.get(job.refnr)
            if known is None:
                result.jobs[job.refnr] = (job, first_seen)
            else:
                dates = [d for d in (first_seen, known[1]) if d]
                result.jobs[job.refnr] = (merge_jobs(job, known[0], by_amount=True), min(dates) if dates else "")
    return result
