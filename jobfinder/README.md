# Ausbildung Job Finder

Separate small Windows app that collects **Ausbildungsstellen Elektroniker/in
(all specialties)** from the **Jobsuche der Bundesagentur für Arbeit** using
your browser, and tells you when the site asks for a **Sicherheitsabfrage**
(captcha) so you can solve it.

## What it does

1. Opens Microsoft Edge (or Chrome) in a visible window with its own profile.
2. Runs the search *Ausbildung · Elektroniker/in* (optionally around a place
   with a radius) – or your own Jobsuche links, see *Own links* below – and
   reads the result pages.
3. Keeps only the Beruf **Elektroniker/in** – in every specialty you tick
   (Betriebstechnik, Energie- und Gebäudetechnik, Automatisierungstechnik,
   Geräte und Systeme, Informations- und Systemtechnik, Maschinen und
   Antriebstechnik, Gebäudesystemintegration, … and new/other ones).
   IT-System-Elektroniker/in, Industrieelektriker/in, Elektroanlagenmonteur/in
   etc. are left out.
4. Opens each job at a calm pace and collects **everything in the BA
   listing** (no other websites are visited):
   * **Kontakt:** Ansprechpartner (with position), e-mail, phone / mobile /
     fax, contact address, how to apply (e-mail, online form, post, phone,
     in person), online application link, required documents;
   * **Firmenwebsite** (the employer's homepage link in the listing), other
     links and social-media links;
   * **Stelle:** title, Beruf and specialty, all places of work, start date,
     training pay per year, working time, contract length, required school
     leaving certificate, publication date, reference and Chiffre number,
     partner portal link, full job description;
   * **Arbeitgeber:** description, size, founding year, headquarters,
     benefits and the contact block of the employer profile.
   E-mail addresses are taken from the contact data, the job text, the
   employer profile and every e-mail link on the page.
5. **Sicherheitsabfrage:** the Ansprechpartner, e-mail and phone of the
   contact box are protected. The app plays an alert sound (repeated every
   30 s), shows a notice in the corner of the screen and brings the browser to
   the front with the text field ready. You type the characters and click
   *Absenden*; the app reads the contact data and continues automatically.
   You can also skip that job's contact data (it is tried again next run).
   The captcha is never solved automatically.
6. Everything is saved; the next run skips jobs already collected. **Als
   Excel/CSV speichern** exports all fields – choose *JSON* in the save
   dialog to get the complete data in one file (e.g. for Claude, which can
   then look up anything else).

The cookie banner of the site is answered with *Alle Cookies ablehnen*.

## Installation

Command Prompt (`Win + R`, `cmd`), paste and press Enter:

```bat
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Aminehaouam/gmailpy/claude/great-brown-eyx5q1/jobfinder/install_jobfinder.ps1 | iex"
```

It installs Python (if needed) and the browser-control library, and creates
an **Ausbildung Job Finder** icon on the Desktop. Run the same line again to
update. (Alternatively double-click `jobfinder\Install_JobFinder.bat` in the
downloaded folder.)

## Use

1. Enter a place or PLZ and a radius (or leave the place empty for all of
   Germany), choose the maximum number of jobs per run and the specialties.
2. Click **Suche starten**. The browser opens and works through the jobs; the
   list fills up. New jobs of this run are highlighted.
3. When the alert sounds: solve the Sicherheitsabfrage in the browser.
4. Double-click a job to open it in your normal browser; **E-Mail kopieren**
   copies the address.

### Own links (e.g. only Energie- und Gebäudetechnik)

Instead of the general Elektroniker/in search you can give the app the exact
search you want:

1. In the Jobsuche, set up the search yourself (e.g. Beruf *Elektroniker/in –
   Energie- und Gebäudetechnik*, place, radius, any other filters) and copy
   the address from the browser's address bar.
2. Paste it into **Eigene Links aus der Jobsuche** (button **Einfügen**
   pastes from the clipboard). One link per line – you can add several
   searches.
3. Click **Suche starten**.

* **Search links** (`…/jobsuche/suche?…`) keep all their filters; the app
  goes through all their result pages.
* **Job links** (`…/jobsuche/jobdetail/…`) or bare reference numbers
  (e.g. `10000-1208539150-S`) open exactly that job – always collected, no
  specialty filter. They are processed first.
* When links are entered, *Ort / PLZ* and *Umkreis* on the left are not used.
  *Max. Stellen pro Suchlauf* and *Bereits gesammelte Stellen überspringen*
  still apply; a job found by several links is opened only once.
* **Nur Elektroniker/in der gewählten Fachrichtungen übernehmen**: on by
  default. Untick it to collect every job of your search links, whatever its
  Beruf (e.g. for a search for a different profession).
* Only links to arbeitsagentur.de are accepted; a wrong line is reported
  before the search starts. The links are remembered for the next start;
  **Leeren** clears them.

## Data

`%LOCALAPPDATA%\BewerbungJobFinder`: `jobs.sqlite3` (collected jobs),
`settings.json`, `browser-profile\` (the app's own browser profile) and
`jobfinder.log`.

## Tests

```bat
.venv\Scripts\python.exe -m pytest tests
```

The parser tests use real pages saved from the Jobsuche (`tests/pages`).
