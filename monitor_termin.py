#!/usr/bin/env python3
"""
monitor_termin.py
==================

Prueft die Online-Terminvergabe der Stadt Ulm (Staatsangehoerigkeitsbehoerde /
Einbuergerung) auf freie Termine und schickt bei Erfolg eine Push-Benachrichtigung
ueber ntfy.sh. Es wird NICHTS gebucht - das Skript geht nur bis zur Ergebnisseite
und liest sie aus.

Zwei Betriebsarten
------------------
* EINZELLAUF (Standard): einmal pruefen, Ergebnis loggen, beenden.
* SCHLEIFENMODUS (LOOP_INTERVAL_MIN gesetzt): prueft im festen Takt selbst
  weiter, bis das Zeitbudget (MAX_RUNTIME_MIN) aufgebraucht ist. Das ist der
  zuverlaessigere Weg auf GitHub Actions, weil der Cron-Trigger dort oft
  20-30 Minuten zu spaet feuert - das Timing INNERHALB des Jobs ist dagegen
  exakt, weil einfach Python schlaeft.

Bekannter Ablauf (per Screenshots verifiziert)
----------------------------------------------
1. https://ssc.wilkencloud.de/ulm/            -> Cookie-Banner ablehnen,
                                                 dann "Staatsangehörigkeitsbehörde"
2. .../select2?md=4                            -> Checkbox "Anliegen rund um die
                                                  Einbürgerung", Hinweis-Modal(s) mit OK
                                                  bestaetigen, dann "Weiter"
3. .../location?...                            -> "Weiter" (Standort Bürgerdienste,
                                                  Olgastr. 66)
4. .../suggest                                 -> Ergebnisseite. Zeigt "Kein freier
                                                  Termin verfügbar", wenn nichts frei ist.

WICHTIG
-------
1. Die Hinweis-Popups sind KEINE nativen Browser-Dialoge, sondern In-Page-Modals
   der Buchungssoftware Tevis (<div role="dialog" id="TevisDialog" class="modal fade in">)
   mit einem Overlay <div class="modal-backdrop">, das alle Klicks abfaengt.
   Ein page.on("dialog", ...)-Handler greift hier NICHT.
2. Ein Modal kann auch ERST NACH einem Klick erscheinen. Deshalb macht advance_to()
   abwechselnd: URL pruefen -> Modal schliessen -> ggf. erneut klicken.
3. Das Skript bucht nichts und fuellt keine persoenlichen Daten aus.
4. Die Erkennung ist "Phrase fehlt = Termin frei". Bei einer unerwarteten
   Fehlerseite gaebe es also einen Fehlalarm - das ist die harmlosere Richtung.
5. Bitte nicht unter ~5 Minuten Takt gehen, um die Behoerdenseite nicht zu belasten.

Umgebungsvariablen
------------------
    NTFY_TOPIC=dein-eindeutiges-thema      # erforderlich
    NTFY_SERVER=https://ntfy.sh            # optional
    HEADLESS=true                          # lokal zum Zuschauen: false
    LOOP_INTERVAL_MIN=10                   # gesetzt = Schleifenmodus
    MAX_RUNTIME_MIN=330                    # Zeitbudget im Schleifenmodus (5,5 h)
    SHOT_STEPS=false                       # Screenshots bei jedem Schritt
    TEST_NTFY=true                         # nur Probe-Push senden, dann beenden
"""

import os
import re
import sys
import time
import random
import logging
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_URL = "https://ssc.wilkencloud.de/ulm/"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# Selektoren fuer die Tevis-Oberflaeche -- ANPASSEN falls die Software aktualisiert wird
MODAL_SELECTOR = "#TevisDialog, div[role='dialog'].modal.in, div.modal.in"
BACKDROP_SELECTOR = "div.modal-backdrop"
WEITER_SELECTOR = (
    "#WeiterButton, input[type=submit][value='Weiter'], button:has-text('Weiter')"
)

# Verifizierte Original-Phrase zuerst, Rest als Fallback
NO_SLOT_PHRASES = [
    "kein freier termin verfügbar",
    "keine zeiten verfügbar",
    "keine freien termine",
    "keine termine verfügbar",
    "kein freier termin",
    "aktuell keine termine",
]

# ---------------------------------------------------------------- Konfiguration
LOOP_INTERVAL_MIN = float(os.environ.get("LOOP_INTERVAL_MIN", "0") or 0)
MAX_RUNTIME_MIN = float(os.environ.get("MAX_RUNTIME_MIN", "330"))
# Im Schleifenmodus wuerden Schritt-Screenshots das Artefakt zumuellen ->
# dort standardmaessig nur Screenshots bei Fehler und bei einem Treffer.
SHOT_STEPS = os.environ.get(
    "SHOT_STEPS", "false" if LOOP_INTERVAL_MIN else "true"
).lower() == "true"
# Nach so vielen Fehlschlaegen in Folge einmal per Push warnen
FAILURE_ALERT_THRESHOLD = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("monitor_termin")

_shot_counter = 0


def shot(page, label: str, step: bool = False) -> Path | None:
    """Speichert einen numerierten Screenshot. Schritt-Screenshots werden im
    Schleifenmodus uebersprungen (step=True), Fehler/Treffer immer gesichert."""
    if step and not SHOT_STEPS:
        return None
    global _shot_counter
    _shot_counter += 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOT_DIR / f"{stamp}_{_shot_counter:03d}_{label}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot: %s", path.name)
        return path
    except Exception as e:
        log.warning("Screenshot '%s' fehlgeschlagen: %s", label, e)
        return None


def send_ntfy(
    subject: str,
    body: str,
    attachment: Path | None = None,
    priority: str = "urgent",
) -> None:
    """Schickt eine Push-Benachrichtigung ueber ntfy.sh, optional mit Screenshot."""
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    endpoint = f"{server}/{topic}"

    headers = {
        "Title": subject,
        "Priority": priority,
        "Click": BASE_URL,
        "Tags": "rotating_light",
    }

    if attachment and attachment.exists():
        headers["Filename"] = attachment.name
        headers["Message"] = body
        with open(attachment, "rb") as f:
            resp = requests.post(endpoint, data=f, headers=headers, timeout=30)
    else:
        resp = requests.post(
            endpoint, data=body.encode("utf-8"), headers=headers, timeout=30
        )

    resp.raise_for_status()
    log.info("ntfy-Benachrichtigung gesendet (Status %s)", resp.status_code)


def dismiss_cookie_banner(page) -> None:
    try:
        page.get_by_role("button", name="Ablehnen").click(timeout=5000)
        log.info("Cookie-Banner abgelehnt.")
    except Exception:
        pass  # nicht vorhanden oder schon weg - unkritisch


def close_one_modal(page, timeout: int = 2500) -> bool:
    """Wartet kurz auf ein Tevis-Hinweis-Modal und schliesst es per OK."""
    modal = page.locator(MODAL_SELECTOR).first
    try:
        # wait_for() wartet wirklich - is_visible() liefert waehrend der
        # Bootstrap-Einblendeanimation False und wuerde das Modal verpassen.
        modal.wait_for(state="visible", timeout=timeout)
    except Exception:
        return False

    label = ""
    try:
        label = (modal.get_attribute("aria-label") or "").strip()
    except Exception:
        pass
    log.info("Hinweis-Modal%s - bestaetige mit OK.", f" ('{label}')" if label else "")

    clicked = False
    for name in ("OK", "Ok", "Ja", "Weiter", "Schließen", "Verstanden"):
        try:
            modal.get_by_role("button", name=name, exact=False).first.click(timeout=1500)
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        try:
            modal.locator(
                "button:visible, input[type=button]:visible, input[type=submit]:visible"
            ).first.click(timeout=2000)
            clicked = True
        except Exception:
            pass

    if not clicked:
        log.warning("Kein OK-Button klickbar - schliesse Modal per JavaScript.")
        try:
            page.evaluate(
                """() => {
                    document.querySelectorAll('.modal.in, #TevisDialog').forEach(m => {
                        m.classList.remove('in');
                        m.style.display = 'none';
                    });
                    document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
                    document.body.classList.remove('modal-open');
                }"""
            )
        except Exception as e:
            log.error("Modal liess sich nicht schliessen: %s", e)
            return False

    # Entscheidend: warten, bis das klick-blockierende Overlay wirklich weg ist
    try:
        page.locator(BACKDROP_SELECTOR).first.wait_for(state="hidden", timeout=5000)
    except Exception:
        pass  # evtl. folgt direkt das naechste Modal

    page.wait_for_timeout(300)
    return True


def dismiss_notice_dialogs(page, max_modals: int = 5, timeout: int = 2500) -> int:
    count = 0
    for _ in range(max_modals):
        if not close_one_modal(page, timeout=timeout):
            break
        count += 1
    return count


def advance_to(page, url_fragment: str, label: str, total_timeout: float = 45.0) -> None:
    """Klickt 'Weiter' und wartet auf die Ziel-URL. Weil ein Hinweis-Modal auch
    ERST NACH dem Klick erscheinen und die Navigation blockieren kann, wird in
    einer Schleife abwechselnd geprueft: URL erreicht? Modal offen? Nochmal klicken?"""
    deadline = time.monotonic() + total_timeout
    clicks = 0

    while time.monotonic() < deadline:
        if url_fragment in page.url:
            log.info("Seite '%s' erreicht.", label)
            return
        if dismiss_notice_dialogs(page, timeout=1200) > 0:
            continue
        try:
            page.locator(WEITER_SELECTOR).first.click(timeout=5000)
            clicks += 1
            log.info("'Weiter' geklickt (Klick %d, Ziel: %s).", clicks, label)
        except Exception as e:
            log.info("'Weiter' gerade nicht klickbar (%s).", type(e).__name__)
        try:
            page.wait_for_url(f"**{url_fragment}*", timeout=4000)
        except Exception:
            pass

    shot(page, f"timeout_{label}")
    raise RuntimeError(
        f"Seite '{label}' nach {total_timeout:.0f}s nicht erreicht "
        f"(URL: {page.url}, Klicks: {clicks})"
    )


def check_ulm_termine() -> tuple[bool, Path | None]:
    """Ein vollstaendiger Pruefdurchlauf mit frischem Browser (wichtig: die
    Tevis-Sitzung laeuft nach ~25 Minuten ab, deshalb pro Durchlauf neu).
    Gibt (verfuegbar, screenshot_pfad) zurueck. Bucht nichts."""
    headless = os.environ.get("HEADLESS", "true").lower() != "false"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.on("dialog", lambda dialog: dialog.accept())  # Sicherheitsnetz

        try:
            page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
            dismiss_cookie_banner(page)
            shot(page, "start", step=True)

            page.get_by_text("Staatsangehörigkeitsbehörde", exact=False).first.click()  # ANPASSEN
            page.wait_for_url("**/select2*", timeout=15000)
            dismiss_cookie_banner(page)
            dismiss_notice_dialogs(page)
            shot(page, "select2", step=True)

            try:
                page.get_by_role("checkbox", name=re.compile("Einbürgerung")).first.click(timeout=8000)
            except Exception:
                log.info("Checkbox nicht ueber Label gefunden - Fallback-Selektor.")
                page.locator("input[type=checkbox]:visible").first.click(timeout=8000)
            log.info("Checkbox angeklickt.")
            dismiss_notice_dialogs(page)
            shot(page, "after_checkbox", step=True)

            advance_to(page, "/location", "location")
            shot(page, "location", step=True)

            advance_to(page, "/suggest", "suggest")
            dismiss_notice_dialogs(page)

            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)

            page_text = page.inner_text("body").lower()
            no_slot_found = any(phrase in page_text for phrase in NO_SLOT_PHRASES)
            available = not no_slot_found
            log.info(
                "Ergebnis: %s",
                "TERMIN MOEGLICH FREI" if available else "kein Termin frei",
            )

            # Bei einem Treffer immer einen Screenshot als Beweis mitschicken
            result_shot = shot(page, "treffer" if available else "ergebnis", step=not available)
            return available, result_shot

        except Exception:
            shot(page, "fehler")
            log.error("Abbruch auf URL: %s", page.url)
            raise
        finally:
            browser.close()


def notify_success(screenshot: Path | None) -> None:
    send_ntfy(
        subject="Ulm Einbürgerung: möglicher freier Termin!",
        body=(
            "Die Ergebnisseite zeigt 'Kein freier Termin verfügbar' nicht mehr an. "
            "Bitte sofort selbst prüfen und buchen."
        ),
        attachment=screenshot,
    )


def run_once() -> int:
    """Einzellauf: einmal pruefen, ggf. benachrichtigen, Exit-Code zurueckgeben."""
    try:
        available, screenshot = check_ulm_termine()
    except Exception as e:
        log.error("Fehler beim Pruefen der Seite: %s", e)
        return 1

    if available:
        try:
            notify_success(screenshot)
        except Exception as e:
            log.error("Benachrichtigung fehlgeschlagen: %s", e)
            return 1
    return 0


def run_loop() -> int:
    """Schleifenmodus: prueft im festen Takt weiter, bis das Zeitbudget endet.

    - Ein einzelner Fehlschlag beendet den Job NICHT, es wird weitergeprueft.
    - Bei einem Treffer wird einmal benachrichtigt, nicht bei jedem Durchlauf
      erneut (erst wieder, wenn der Termin zwischenzeitlich weg war).
    - Nach mehreren Fehlschlaegen in Folge kommt eine leise Warn-Push, damit
      ein stiller Dauerausfall nicht unbemerkt bleibt.
    """
    interval = LOOP_INTERVAL_MIN * 60
    end_time = time.monotonic() + MAX_RUNTIME_MIN * 60
    log.info(
        "Schleifenmodus: alle %.0f Minuten, Zeitbudget %.0f Minuten.",
        LOOP_INTERVAL_MIN,
        MAX_RUNTIME_MIN,
    )

    already_notified = False
    consecutive_failures = 0
    failure_alert_sent = False
    checks = 0

    while True:
        checks += 1
        log.info("--- Durchlauf %d ---", checks)
        try:
            available, screenshot = check_ulm_termine()
            consecutive_failures = 0
            failure_alert_sent = False

            if available and not already_notified:
                try:
                    notify_success(screenshot)
                    already_notified = True
                except Exception as e:
                    log.error("Benachrichtigung fehlgeschlagen: %s", e)
            elif available:
                log.info("Termin weiterhin frei - bereits benachrichtigt, keine erneute Push.")
            else:
                already_notified = False  # Zustand zurueckgesetzt

        except Exception as e:
            consecutive_failures += 1
            log.error(
                "Durchlauf %d fehlgeschlagen (%d in Folge): %s",
                checks,
                consecutive_failures,
                e,
            )
            if consecutive_failures >= FAILURE_ALERT_THRESHOLD and not failure_alert_sent:
                try:
                    send_ntfy(
                        subject="Termin-Monitor: wiederholte Fehler",
                        body=(
                            f"{consecutive_failures} Pruefungen in Folge fehlgeschlagen. "
                            "Evtl. hat sich die Behoerdenseite geaendert - bitte die "
                            "Logs/Screenshots im GitHub-Artefakt ansehen."
                        ),
                        priority="low",
                    )
                    failure_alert_sent = True
                except Exception as inner:
                    log.error("Warn-Push fehlgeschlagen: %s", inner)

        # Zeitbudget pruefen: nur weiterschlafen, wenn danach noch ein
        # vollstaendiger Durchlauf ins Budget passt
        remaining = end_time - time.monotonic()
        if remaining <= interval:
            log.info(
                "Zeitbudget erreicht - beende nach %d Durchlaeufen. "
                "Der naechste Job startet per Cron.",
                checks,
            )
            return 0

        # Kleiner Zufalls-Offset, damit die Abfragen nicht sekundengenau
        # im Takt kommen (wirkt weniger nach Bot)
        sleep_for = interval + random.uniform(-30, 30)
        log.info("Warte %.1f Minuten bis zum naechsten Durchlauf.", sleep_for / 60)
        time.sleep(max(60.0, sleep_for))


def main() -> int:
    # Test-Modus: nur eine Probe-Push senden und beenden
    if os.environ.get("TEST_NTFY", "").lower() == "true" or "--test" in sys.argv:
        log.info("TEST-MODUS: sende Probe-Benachrichtigung (keine Seitenpruefung).")
        try:
            send_ntfy(
                subject="Testnachricht - Ulm Termin-Monitor",
                body=(
                    "Das ist nur ein Test. Wenn du das auf dem Handy siehst, "
                    "funktioniert die Benachrichtigung im Erfolgsfall auch."
                ),
                priority="default",
            )
            log.info("Test erfolgreich - Benachrichtigung wurde abgeschickt.")
            return 0
        except Exception as e:
            log.error("TEST FEHLGESCHLAGEN: %s", e)
            return 1

    if LOOP_INTERVAL_MIN > 0:
        return run_loop()
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
