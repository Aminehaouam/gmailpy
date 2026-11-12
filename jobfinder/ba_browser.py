"""
ba_browser.py - Drive a real browser through the Jobsuche der BA.

* Uses Microsoft Edge (installed on every Windows PC) or Google Chrome in a
  visible window with its own profile folder, so cookie choices and the
  site's session survive between runs.  Playwright's own Chromium is the
  last fallback.
* Opens search result pages and job pages at a calm pace (random pause
  between page loads) and reads the data embedded in each page.
* When the employer's contact details are protected by the
  "Sicherheitsabfrage" (captcha), it notifies the user (callback), brings
  the browser window to the front and WAITS until the user has solved it.
  The captcha is never solved or bypassed automatically.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from email_finder import extract_emails
from ba_parser import (
    Job,
    SearchPage,
    apply_detail,
    apply_kontaktdaten,
    build_search_url,
    with_page,
    extract_state,
    find_kontaktdaten,
    parse_contact_text,
    parse_search,
    specialty_selected,
)

log = logging.getLogger(__name__)

_CONTACT_STATE_JS = """
() => {
  const box = document.querySelector('#jobdetails-kontaktdaten-container');
  if (!box) return {state: 'absent', text: ''};
  const captcha = box.querySelector('#captchaForm, #kontaktdaten-captcha-image, #kontaktdaten-captcha-input');
  if (captcha && captcha.offsetParent !== null) return {state: 'captcha', text: ''};
  const text = (box.innerText || '').trim();
  if (!text || box.classList.contains('ba-loading') || box.querySelector('.ba-loading'))
    return {state: 'loading', text: text};
  return {state: 'content', text: text};
}
"""


_PAGE_DATA_JS = """
() => ({
  title: document.title || '',
  text: document.body ? document.body.innerText : '',
  links: Array.from(document.querySelectorAll('a[href]')).slice(0, 1500)
           .map(a => [(a.innerText || a.title || '').trim().slice(0, 80), a.getAttribute('href')]),
  mailtos: Array.from(document.querySelectorAll('a[href^="mailto:" i]')).map(a => a.getAttribute('href')),
})
"""

class StopRequested(Exception):
    pass


class BrowserNotAvailable(Exception):
    pass


class BrowserSession:
    """One browser window, used for all page loads of a run."""

    def __init__(self, profile_dir: Path, *, headless: bool = False, channel: str | None = None,
                 executable_path: str | None = None, delay: tuple[float, float] = (2.0, 4.0),
                 stop_event: threading.Event | None = None,
                 skip_event: threading.Event | None = None):
        self.profile_dir = profile_dir
        self.headless = headless
        self.channel = channel
        self.executable_path = executable_path
        self.delay = delay
        self.stop_event = stop_event or threading.Event()
        # Set by the user to skip the contact details of the current job.
        self.skip_event = skip_event or threading.Event()
        self._pw = None
        self._context = None
        self.page = None
        self.browser_name = ""
        self._last_navigation = 0.0
        self._cookies_done = False
        self._responses: list = []   # data responses of the current job page

    # -- life cycle ---------------------------------------------------------
    def start(self) -> None:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        attempts: list[tuple[str, dict]] = []
        if self.executable_path:
            attempts.append(("custom browser", {"executable_path": self.executable_path}))
        else:
            channels = [self.channel] if self.channel else ["msedge", "chrome"]
            attempts += [({"msedge": "Microsoft Edge", "chrome": "Google Chrome"}.get(c, c), {"channel": c})
                         for c in channels]
            attempts.append(("Chromium", {}))
        errors = []
        for name, options in attempts:
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=self.headless, locale="de-DE",
                    viewport=None if not self.headless else {"width": 1280, "height": 900},
                    args=[] if self.headless else ["--start-maximized"], **options)
                self.browser_name = name
                break
            except PlaywrightError as exc:
                errors.append(f"{name}: {str(exc).splitlines()[0]}")
        if self._context is None:
            self.close()
            raise BrowserNotAvailable("No browser could be started.\n" + "\n".join(errors))
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.page.set_default_timeout(45000)
        # Remember the data responses of each page: the contact data
        # (Ansprechpartner, e-mail, phone) arrive as JSON after the
        # Sicherheitsabfrage and are read from there.
        self.page.on("response", self._remember_response)
        log.info("Browser started: %s", self.browser_name)

    def close(self) -> None:
        for closer in (lambda: self._context and self._context.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._context = None
        self._pw = None

    # -- helpers --------------------------------------------------------------
    def _remember_response(self, response) -> None:
        try:
            if "arbeitsagentur.de" in response.url and response.request.resource_type in ("xhr", "fetch"):
                self._responses.append(response)
        except Exception:
            pass

    def _contact_data_from_responses(self) -> dict | None:
        for response in reversed(self._responses):
            try:
                if "json" not in (response.headers.get("content-type") or ""):
                    continue
                found = find_kontaktdaten(response.json())
            except Exception:
                continue
            if found:
                return found
        return None

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRequested()

    def _pause(self) -> None:
        """Calm pace: random pause between page loads (stoppable)."""
        wait = random.uniform(*self.delay) - (time.monotonic() - self._last_navigation)
        end = time.monotonic() + max(0.0, wait)
        while time.monotonic() < end:
            self._check_stop()
            time.sleep(0.1)

    def goto(self, url: str) -> str:
        """Open *url* and return the server-rendered HTML."""
        self._check_stop()
        self._pause()
        self._responses = []
        response = self.page.goto(url, wait_until="domcontentloaded")
        self._last_navigation = time.monotonic()
        html = response.text() if response is not None else self.page.content()
        # The banner appears a moment after the page: watch for it on the
        # first pages of a session (later it is remembered by the browser).
        self._dismiss_cookie_banner(wait=0.0 if self._cookies_done else 5.0)
        return html

    def _dismiss_cookie_banner(self, wait: float = 0.0) -> bool:
        """Choose 'Alle Cookies ablehnen' (only technically necessary cookies)
        so the banner never hides the Sicherheitsabfrage."""
        end = time.monotonic() + wait
        while True:
            try:
                button = self.page.get_by_role("button", name="Alle Cookies ablehnen")
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=3000)
                    self._cookies_done = True
                    log.info("Cookie banner: rejected optional cookies")
                    return True
            except Exception:
                pass
            if time.monotonic() >= end:
                return False
            self._check_stop()
            time.sleep(0.25)

    # -- pages ----------------------------------------------------------------
    def search(self, was: str, wo: str, umkreis: int | None, page_number: int) -> SearchPage:
        html = self.goto(build_search_url(was, wo, umkreis, page_number))
        return parse_search(extract_state(html))

    def search_url(self, url: str, page_number: int, only_elektroniker: bool = True) -> SearchPage:
        """Result page *page_number* of any Jobsuche search link (filters kept)."""
        html = self.goto(with_page(url, page_number))
        return parse_search(extract_state(html), only_elektroniker=only_elektroniker)

    def contact_state(self) -> dict:
        return self.page.evaluate(_CONTACT_STATE_JS)

    def fetch_detail(
        self,
        job: Job,
        *,
        want_contact: bool,
        on_captcha: Callable[[Job], None] = lambda job: None,
        on_captcha_done: Callable[[Job, bool], None] = lambda job, solved: None,
        captcha_timeout: float = 600.0,
        load_timeout: float = 15.0,
    ) -> Job:
        html = self.goto(job.url)
        apply_detail(job, extract_state(html))   # job ad + employer profile (incl. e-mails in the text)
        if not want_contact:
            self._emails_on_ba_page(job)
            return job

        state = self._wait_for_contact_box(load_timeout)
        if state["state"] == "captcha":
            log.info("Sicherheitsabfrage for %s - waiting for the user", job.company)
            self._focus_captcha()   # captcha visible and the text field focused first
            on_captcha(job)
            state = self._wait_for_user(captcha_timeout)
            solved = state["state"] == "content"
            on_captcha_done(job, solved)
            if not solved:
                self._emails_on_ba_page(job)
                job.contact_status = "captcha-skipped"  # Ansprechpartner missing: retried next run
                return job
        if state["state"] == "content":
            time.sleep(0.5)  # let the contact data response finish
            data = self._contact_data_from_responses()
            if data:
                apply_kontaktdaten(job, data)
            contact = parse_contact_text(state["text"])
            if not job.contact_person and contact["person"]:
                job.contact_person = contact["person"]
            if contact["emails"]:
                job.emails = list(dict.fromkeys(contact["emails"] + job.emails))
                job.email_source = "BA-Kontaktdaten"
            if not data and contact["phones"]:
                job.phones = [f"Telefon: {p}" for p in contact["phones"]]
            job.contact_text = contact["text"]
        self._emails_on_ba_page(job)
        if job.emails or job.phones or job.contact_person:
            job.contact_status = "ok"
        else:
            # No contact data: partner-portal jobs are applied for on the partner site.
            job.contact_status = "external" if job.external_url else "none"
        return job

    def _emails_on_ba_page(self, job: Job) -> None:
        """Every e-mail address anywhere on the BA page (text and mailto links)."""
        try:
            data = self.page.evaluate(_PAGE_DATA_JS)
        except Exception:
            return
        found = extract_emails(data["text"], " ".join(data["mailtos"]))
        new = [e for e in found if e not in job.emails]
        if new:
            if not job.emails:
                job.email_source = "BA-Anzeige"
            job.emails = job.emails + new

    def _wait_for_contact_box(self, timeout: float) -> dict:
        end = time.monotonic() + timeout
        state = self.contact_state()
        while state["state"] == "loading" and time.monotonic() < end:
            self._check_stop()
            self._dismiss_cookie_banner()
            time.sleep(0.3)
            state = self.contact_state()
        return state

    def _focus_captcha(self) -> None:
        self._dismiss_cookie_banner()
        try:
            self.page.bring_to_front()
            self.page.locator("#jobdetails-kontaktdaten-container").scroll_into_view_if_needed(timeout=3000)
            self.page.locator("#kontaktdaten-captcha-input").focus(timeout=3000)
        except Exception:
            pass

    def _wait_for_user(self, timeout: float) -> dict:
        """Poll until the captcha is gone and the contact details are shown."""
        end = time.monotonic() + timeout
        self.skip_event.clear()
        state = self.contact_state()
        while time.monotonic() < end:
            self._check_stop()
            if state["state"] == "content":
                return state
            if self.skip_event.is_set():
                self.skip_event.clear()
                return state
            time.sleep(1.0)
            self._dismiss_cookie_banner()
            try:
                state = self.contact_state()
            except Exception:  # page reloaded meanwhile
                state = {"state": "loading", "text": ""}
        return state


# --------------------------------------------------------------------------
# One complete run
# --------------------------------------------------------------------------

@dataclass
class RunSettings:
    was: str = "Elektroniker/in"
    wo: str = ""
    umkreis: int | None = None
    specialties: set[str] = field(default_factory=set)
    max_jobs: int = 30
    max_pages: int = 20
    want_contact: bool = True
    skip_known: bool = True
    captcha_timeout: float = 600.0
    # The user's own links: search pages (used instead of was/wo/umkreis)
    # and single jobs (reference numbers).
    search_urls: list[str] = field(default_factory=list)
    job_refs: list[str] = field(default_factory=list)
    # Keep only Elektroniker/in in the selected specialties.
    filter_elektroniker: bool = True


@dataclass
class RunEvents:
    status: Callable[[str], None] = lambda text: None
    job: Callable[[Job], None] = lambda job: None
    specialties: Callable[[dict], None] = lambda counts: None
    captcha: Callable[[Job], None] = lambda job: None
    captcha_done: Callable[[Job, bool], None] = lambda job, solved: None


def run(session: BrowserSession, settings: RunSettings, known: Callable[[str], bool],
        save: Callable[[Job], None], events: RunEvents) -> dict:
    """Open the jobs of the user's job links, then go through the search
    results (the user's search links, or the search built from place and
    radius) page by page.  Returns a summary."""
    counter = {"opened": 0, "found": 0, "skipped": 0}
    seen: set[str] = set()

    def process(job: Job, filtered: bool) -> bool:
        """Open one job; returns False when the job limit is reached."""
        if job.refnr in seen:
            return True
        seen.add(job.refnr)
        if filtered and settings.filter_elektroniker and not specialty_selected(job.specialty, settings.specialties):
            return True
        counter["found"] += 1
        if settings.skip_known and known(job.refnr):
            counter["skipped"] += 1
            return True
        if counter["opened"] >= settings.max_jobs:
            return False
        counter["opened"] += 1
        events.status(f"Opening job {counter['opened']}/{settings.max_jobs}: "
                      f"{job.company or job.refnr} {f'({job.ort})' if job.ort else ''}".strip())
        try:
            session.fetch_detail(job, want_contact=settings.want_contact, on_captcha=events.captcha,
                                 on_captcha_done=events.captcha_done, captcha_timeout=settings.captcha_timeout)
        except StopRequested:
            save(job)
            raise
        except Exception as exc:  # one broken page must not stop the run
            log.warning("Could not read job %s: %s", job.refnr, exc)
            job.contact_status = job.contact_status or "error"
        save(job)
        events.job(job)
        return True

    # 1. Single job links: always opened (no specialty filter).
    for refnr in settings.job_refs:
        if not process(Job(refnr, "", "", "", ""), filtered=False):
            break

    # 2. Search links (or the default search), page by page.
    sources = settings.search_urls or [build_search_url(settings.was, settings.wo, settings.umkreis)]
    totals: dict[str, int] = {}
    for number, url in enumerate(sources, 1):
        page_number, last_page = 1, 1
        label = f"link {number}/{len(sources)}, " if len(sources) > 1 else ""
        while (page_number <= min(last_page, settings.max_pages)
               and counter["opened"] < settings.max_jobs):
            events.status(f"Reading search results ({label}page {page_number})...")
            result = session.search_url(url, page_number, only_elektroniker=settings.filter_elektroniker)
            if page_number == 1:
                for name, count in result.specialties.items():
                    totals[name] = totals.get(name, 0) + count
                events.specialties(dict(totals))
                events.status(f"{result.max_results} results for this search...")
            last_page = result.last_page
            for job in result.jobs:
                if not process(job, filtered=True):
                    break
            page_number += 1
        if counter["opened"] >= settings.max_jobs:
            break
    return dict(counter)
