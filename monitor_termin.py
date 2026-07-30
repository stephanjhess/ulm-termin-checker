#!/usr/bin/env python3
"""
monitor_termin.py
==================

Prueft die Online-Terminvergabe der Stadt Ulm (Staatsangehoerigkeitsbehoerde /
Einbuergerung) auf freie Termine und schickt bei Erfolg eine Push-Benachrichtigung
ueber ntfy.sh. Es wird NICHTS gebucht - das Skript geht nur bis zur Ergebnisseite
und liest sie aus.

Bekannter Ablauf (vom Nutzer per Screenshots bestaetigt)
----------------------------------------------------------
1. https://ssc.wilkencloud.de/ulm/            -> Cookie-Banner ablehnen,
                                                 dann "Staatsangehörigkeitsbehörde" klicken
2. .../select2?md=4                            -> Checkbox "Anliegen rund um die Einbürgerung"
                                                  anklicken. Es erscheinen ZWEI Hinweis-Modals,
                                                  aber nicht zwingend beide sofort: eines direkt
                                                  nach dem Ankreuzen, das zweite erst NACH dem
                                                  Klick auf "Weiter".
3. .../location?mdt=19&select_cnc=1&cnc-600=1  -> "Weiter" klicken
4. .../suggest                                 -> Zielseite. Zeigt "Kein freier Termin
                                                  verfügbar", wenn nichts frei ist.

WICHTIG - bitte vor dem Einsatz lesen
--------------------------------------
1. Die Hinweis-Popups sind KEINE nativen Browser-Dialoge (alert/confirm), sondern
   In-Page-Modals der Buchungssoftware Tevis:
       <div role="dialog" id="TevisDialog" aria-label="Hinweis" class="modal fade in"
            data-backdrop="static">
   Dazu gehoert ein Overlay <div class="modal-backdrop fade in">, das ALLE Klicks
   abfaengt, solange das Modal offen ist. Ein page.on("dialog", ...)-Handler greift
   hier NICHT.
2. Weil ein Modal auch NACH einem Klick erscheinen kann, wird jeder Seitenwechsel
   ueber advance_to() gemacht: klicken -> warten -> auftauchende Modals schliessen
   -> ggf. nochmal klicken, bis die erwartete URL erreicht ist.
3. Es wird bei JEDEM Schritt ein Screenshot gespeichert (auch im Fehlerfall),
   damit im GitHub-Artefakt immer sichtbar ist, wo es haengen blieb.
4. Das Skript bucht nichts und fuellt keine persoenlichen Daten aus - es
   stoppt auf der Ergebnisseite (Schritt 4).
5. Alle 15 Minuten ist ein vernuenftiger, unauffaelliger Abfrage-Rhythmus.

Einrichtung
-----------
    pip install -r requirements.txt
    playwright install chromium

    Umgebungsvariablen:
        NTFY_TOPIC=dein-eindeutiges-thema      # erforderlich
        NTFY_SERVER=https://ntfy.sh            # optional
        HEADLESS=true                          # beim lokalen Debuggen: false
"""

import os
import re
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_URL = "https://ssc.wilkencloud.de/ulm/"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# Selektoren fuer die Tevis-Modals -- ANPASSEN falls die Software aktualisiert wird
MODAL_SELECTOR = "#TevisDialog, div[role='dialog'].modal.in, div.modal.in"
BACKDROP_SELECTOR = "div.modal-backdrop"
WEITER_SELECTOR = (
    "#WeiterButton, input[type=submit][value='Weiter'], button:has-text('Weiter')"
)

# Vom Nutzer bestaetigte Original-Phrase zuerst, Rest als Fallback
NO_SLOT_PHRASES = [
    "kein freier termin verfügbar",
    "keine freien termine",
    "keine termine verfügbar",
    "kein freier termin",
    "aktuell keine termine",
    "leider keine",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("monitor_termin")

RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
_shot_counter = 0


def shot(page, label: str) -> Path | None:
    """Speichert einen numerierten Screenshot. Wird bei jedem Schritt und im
    Fehlerfall aufgerufen, damit das GitHub-Artefakt nie leer ist."""
    global _shot_counter
    _shot_counter += 1
    path = SCREENSHOT_DIR / f"{RUN_STAMP}_{_shot_counter:02d}_{label}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot: %s", path.name)
        return path
    except Exception as e:
        log.warning("Screenshot '%s' fehlgeschlagen: %s", label, e)
        return None


def send_ntfy(subject: str, body: str, attachment: Path | None = None) -> None:
    """Schickt eine Push-Benachrichtigung ueber ntfy.sh, optional mit Screenshot."""
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    endpoint = f"{server}/{topic}"

    headers = {
        "Title": subject,
        "Priority": "urgent",
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
    """Klickt den Cookie-Banner weg, falls er auftaucht."""
    try:
        page.get_by_role("button", name="Ablehnen").click(timeout=5000)
        log.info("Cookie-Banner abgelehnt.")
    except Exception:
        log.info("Kein Cookie-Banner gefunden (oder schon weg) - weiter.")


def close_one_modal(page, timeout: int = 2500) -> bool:
    """Wartet kurz auf ein Tevis-Hinweis-Modal und schliesst es per OK.
    Gibt True zurueck, wenn eines geschlossen wurde."""
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
    log.info("Hinweis-Modal erkannt%s - bestaetige mit OK.", f" ('{label}')" if label else "")

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
            clicked = True
        except Exception as e:
            log.error("Modal liess sich nicht schliessen: %s", e)
            return False

    # Warten, bis das blockierende Overlay wirklich weg ist
    try:
        page.locator(BACKDROP_SELECTOR).first.wait_for(state="hidden", timeout=5000)
    except Exception:
        log.info("Overlay noch sichtbar - evtl. folgt direkt das naechste Modal.")

    page.wait_for_timeout(300)
    return True


def dismiss_notice_dialogs(page, max_modals: int = 5, timeout: int = 2500) -> int:
    """Schliesst alle gerade offenen bzw. kurz darauf erscheinenden Hinweis-Modals."""
    count = 0
    for _ in range(max_modals):
        if not close_one_modal(page, timeout=timeout):
            break
        count += 1
    return count


def advance_to(page, url_fragment: str, label: str, total_timeout: float = 45.0) -> None:
    """Klickt 'Weiter' und wartet auf die Ziel-URL.

    Der Knackpunkt: ein Hinweis-Modal kann AUCH ERST NACH dem Klick erscheinen
    und die Navigation blockieren. Deshalb wird in einer Schleife abwechselnd
    geprueft, ob die Ziel-URL erreicht ist, ob ein Modal offen ist (-> schliessen)
    und ob erneut geklickt werden muss.
    """
    deadline = time.monotonic() + total_timeout
    clicks = 0

    while time.monotonic() < deadline:
        # Ziel erreicht?
        if url_fragment in page.url:
            log.info("Seite '%s' erreicht (%s).", label, page.url)
            return

        # Steht ein Modal im Weg? Dann weg damit und Schleife neu bewerten.
        if dismiss_notice_dialogs(page, timeout=1200) > 0:
            continue

        # Kein Modal, Ziel nicht erreicht -> (erneut) auf Weiter klicken
        try:
            page.locator(WEITER_SELECTOR).first.click(timeout=5000)
            clicks += 1
            log.info("'Weiter' geklickt (Klick %d, Ziel: %s).", clicks, label)
        except Exception as e:
            log.info("'Weiter' gerade nicht klickbar (%s) - warte kurz.", type(e).__name__)

        # Kurz auf Navigation ODER auftauchendes Modal warten
        try:
            page.wait_for_url(f"**{url_fragment}*", timeout=4000)
        except Exception:
            pass

    shot(page, f"timeout_{label}")
    raise RuntimeError(
        f"Seite '{label}' nach {total_timeout:.0f}s nicht erreicht "
        f"(aktuelle URL: {page.url}, Klicks: {clicks})"
    )


def check_ulm_termine() -> tuple[bool, Path | None]:
    """Klickt sich bis zur Ergebnisseite durch und prueft auf freie Termine.
    Gibt (verfuegbar, screenshot_pfad) zurueck. Bucht nichts."""
    headless = os.environ.get("HEADLESS", "true").lower() != "false"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.on("dialog", lambda dialog: dialog.accept())  # Sicherheitsnetz

        try:
            # Schritt 0/1: Startseite -> Staatsangehörigkeitsbehörde
            page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
            dismiss_cookie_banner(page)
            shot(page, "start")

            page.get_by_text("Staatsangehörigkeitsbehörde", exact=False).first.click()  # ANPASSEN
            page.wait_for_url("**/select2*", timeout=15000)
            dismiss_cookie_banner(page)
            dismiss_notice_dialogs(page)
            shot(page, "select2")

            # Schritt 2: Checkbox "Anliegen rund um die Einbürgerung"
            try:
                page.get_by_role("checkbox", name=re.compile("Einbürgerung")).first.click(timeout=8000)
            except Exception:
                log.info("Checkbox nicht ueber Label gefunden - nutze Fallback-Selektor.")
                page.locator("input[type=checkbox]:visible").first.click(timeout=8000)
            log.info("Checkbox angeklickt.")

            dismiss_notice_dialogs(page)
            shot(page, "after_checkbox")

            # Schritt 2 -> 3 (hier kann ein zweites Modal nach dem Klick kommen)
            advance_to(page, "/location", "location")
            shot(page, "location")

            # Schritt 3 -> 4
            advance_to(page, "/suggest", "suggest")
            dismiss_notice_dialogs(page)

            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            result_shot = shot(page, "ergebnis")

            page_text = page.inner_text("body").lower()
            no_slot_found = any(phrase in page_text for phrase in NO_SLOT_PHRASES)
            log.info(
                "Ergebnisseite ausgewertet - 'kein Termin'-Hinweis %s.",
                "gefunden" if no_slot_found else "NICHT gefunden",
            )
            return (not no_slot_found), result_shot

        except Exception:
            # Immer einen Screenshot vom Fehlerzustand sichern
            shot(page, "fehler")
            log.error("Abbruch auf URL: %s", page.url)
            raise
        finally:
            browser.close()


def main() -> int:
    # Test-Modus: schickt sofort eine Probe-Benachrichtigung und beendet sich.
    # Aufruf: TEST_NTFY=true python monitor_termin.py   oder   python monitor_termin.py --test
    if os.environ.get("TEST_NTFY", "").lower() == "true" or "--test" in sys.argv:
        log.info("TEST-MODUS: sende Probe-Benachrichtigung (keine Seitenpruefung).")
        try:
            send_ntfy(
                subject="Testnachricht - Ulm Termin-Monitor",
                body=(
                    "Das ist nur ein Test. Wenn du das auf dem Handy siehst, "
                    "funktioniert die Benachrichtigung im Erfolgsfall auch."
                ),
            )
            log.info("Test erfolgreich - Benachrichtigung wurde abgeschickt.")
            return 0
        except Exception as e:
            log.error("TEST FEHLGESCHLAGEN: %s", e)
            return 1

    try:
        available, screenshot = check_ulm_termine()
    except Exception as e:
        log.error("Fehler beim Pruefen der Seite: %s", e)
        return 1

    if available:
        log.info("Moeglicherweise ein Termin frei!")
        try:
            send_ntfy(
                subject="Ulm Einbürgerung: möglicher freier Termin!",
                body=(
                    "Die Ergebnisseite zeigt 'Kein freier Termin verfügbar' nicht "
                    "mehr an. Bitte sofort selbst prüfen und buchen."
                ),
                attachment=screenshot,
            )
        except Exception as e:
            log.error("Benachrichtigung fehlgeschlagen: %s", e)
            return 1
    else:
        log.info("Kein Termin frei (Stand jetzt).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
