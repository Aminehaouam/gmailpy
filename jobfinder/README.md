# Ausbildung Job Finder

Separate small Windows app that collects **Ausbildungsstellen Elektroniker/in
(all specialties)** from the **Jobsuche der Bundesagentur für Arbeit** using
your browser, and tells you when the site asks for a **Sicherheitsabfrage**
(captcha) so you can solve it.

## What it does

1. Opens Microsoft Edge (or Chrome) in a visible window with its own profile.
2. Runs the search *Ausbildung · Elektroniker/in* (optionally around a place
   with a radius) and reads the result pages.
3. Keeps only the Beruf **Elektroniker/in** – in every specialty you tick
   (Betriebstechnik, Energie- und Gebäudetechnik, Automatisierungstechnik,
   Geräte und Systeme, Informations- und Systemtechnik, Maschinen und
   Antriebstechnik, Gebäudesystemintegration, … and new/other ones).
   IT-System-Elektroniker/in, Industrieelektriker/in, Elektroanlagenmonteur/in
   etc. are left out.
4. Opens each job at a calm pace and collects: title, specialty, company,
   address, start date, required school leaving certificate, employer
   homepage, description and – if you want – the **contact details**
   (contact person, e-mail, phone). Jobs from partner portals get the link
   to apply there.
5. **Sicherheitsabfrage:** when the contact details are protected, the app
   plays an alert sound (repeated every 30 s), shows a notice in the corner of
   the screen and brings the browser to the front with the text field ready.
   You type the characters and click *Absenden*; the app notices and
   continues automatically. You can also skip that job's contact details.
   The captcha is never solved automatically.
6. **Finding the e-mail address**, in this order:
   * the BA ad itself – job text, employer info and every e-mail link on the
     page (many ads write "Bewerbung an bewerbung@firma.de" in the text);
   * the contact box behind the Sicherheitsabfrage (skipped automatically
     when the ad already contains an e-mail address – fewer captchas);
   * the **company website**: the homepage linked in the BA ad, a web
     address in the job text, or a **Google search** for company + place
     (Bing as fallback). A search result is only used if the company name
     appears in its web address or page title, so job portals and unrelated
     sites are never taken. On the website the app reads the start page,
     the Karriere/Ausbildung/Bewerbung, Kontakt and Impressum pages.
     Application addresses (bewerbung@, ausbildung@, karriere@, personal@,
     recruiting@ …) come first, then info@/kontakt@; datenschutz@, noreply@,
     vertrieb@ and similar are ignored. If Google asks "Ich bin kein
     Roboter", you get the same alert as for the Sicherheitsabfrage (or let
     the app continue with Bing).
   Each company is searched once (the result is remembered; "nothing found"
   is tried again after 7 days). **Fehlende E-Mails suchen** fills in
   addresses for jobs collected earlier.
7. Everything is saved; the next run skips jobs already collected (jobs whose
   contact details are still missing are opened again). **Als Excel/CSV
   speichern** exports the list.

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

## Data

`%LOCALAPPDATA%\BewerbungJobFinder`: `jobs.sqlite3` (collected jobs),
`settings.json`, `browser-profile\` (the app's own browser profile) and
`jobfinder.log`.

## Tests

```bat
.venv\Scripts\python.exe -m pytest tests
```

The parser tests use real pages saved from the Jobsuche (`tests/pages`).
