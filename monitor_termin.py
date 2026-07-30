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
                                                  anklicken. Dabei erscheinen zwei Hinweis-
                                                  Popups, die per OK bestaetigt werden muessen.
                                                  Danach wird "Weiter" aktiv.
3. .../location?mdt=19&select_cnc=1&cnc-600=1  -> "Weiter" klicken
4. .../suggest                                 -> Zielseite. Zeigt "Kein freier Termin
                                                  verfügbar", wenn nichts frei ist.

WICHTIG - bitte vor dem Einsatz lesen
--------------------------------------
1. Die Hinweis-Popups sind KEINE nativen Browser-Dialoge (alert/confirm), sondern
   In-Page-Modals der Buchungssoftware Tevis:
       <div role="dialog" id="TevisDialog" aria-label="Hinweis" class="modal fade in"
            data-backdrop="static">
   Dazu gehoert ein unsichtbares Overlay <div class="modal-backdrop fade in">, das
   ALLE Klicks abfaengt, solange das Modal offen ist. Ein page.on("dialog", ...)-
   Handler greift hier NICHT. Deshalb werden sie in dismiss_notice_dialogs()
   aktiv erkannt, per OK geschlossen, und es wird gewartet, bis das Overlay
   wirklich verschwunden ist.
2. Wenn sich die Seite mal aendert: im Screenshot-Artefakt nachsehen, wo es
   haengen bleibt, und die mit "ANPASSEN" markierten Stellen korrigieren.
3. Das Skript bucht nichts und fuellt keine persoenlichen Daten aus - es
   stoppt auf der Ergebnisseite (Schritt 4).
4. Zu haeufiges/aggressives Abrufen kann als Bot-Traffic auffallen.
   Alle 15 Minuten ist ein vernuenftiger, unauffaelliger Rhythmus.

Einrichtung
-----------
    pip install -r requirements.txt
    playwright install chromium

    Umgebungsvariablen setzen (z.B. im GitHub-Actions-Secret oder lokal):
        NTFY_TOPIC=dein-eindeutiges-thema
        NTFY_SERVER=https://ntfy.sh            # optional, Standard ist ntfy.sh
        HEADLESS=true                          # beim ersten Testlauf: false
        DEBUG_SHOTS=true                       # optional: Screenshot nach jedem Schritt
"""

import os
import re
import sys
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

DEBUG_SHOTS = os.environ.get("DEBUG_SHOTS", "false").lower() == "true"


def debug_shot(page, label: str) -> None:
    """Optionaler Zwischen-Screenshot, um Haenger zu lokalisieren."""
    if not DEBUG_SHOTS:
        return
    path = SCREENSHOT_DIR / f"debug_{label}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log.info("Debug-Screenshot: %s", path.name)
    except Exception as e:
        log.warning("Debug-Screenshot fehlgeschlagen: %s", e)


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
    Gibt True zurueck, wenn eines geschlossen wurde, sonst False."""
    modal = page.locator(MODAL_SELECTOR).first
    try:
        # wait_for() wartet wirklich (is_visible() nicht - das prueft nur den
        # Ist-Zustand und liefert bei der Bootstrap-Einblendeanimation False).
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
    # 1. Versuch: Button mit Text OK / Ja / Weiter / Schliessen im Modal
    for name in ("OK", "Ok", "Ja", "Weiter", "Schließen", "Verstanden"):
        try:
            modal.get_by_role("button", name=name, exact=False).first.click(timeout=1500)
            clicked = True
            break
        except Exception:
            continue

    # 2. Versuch: beliebiger Button / Submit-Input im Modal
    if not clicked:
        try:
            modal.locator(
                "button:visible, input[type=button]:visible, input[type=submit]:visible"
            ).first.click(timeout=2000)
            clicked = True
        except Exception:
            pass

    # 3. Versuch: per JavaScript schliessen (Bootstrap-API), falls Klick scheitert
    if not clicked:
        log.warning("Kein OK-Button klickbar - versuche Modal per JS zu schliessen.")
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

    # Warten, bis das blockierende Overlay wirklich weg ist - das ist der
    # entscheidende Schritt, sonst faengt es den naechsten Klick weiter ab.
    try:
        page.locator(BACKDROP_SELECTOR).first.wait_for(state="hidden", timeout=5000)
    except Exception:
        log.info("Overlay noch sichtbar - evtl. folgt direkt das naechste Modal.")

    page.wait_for_timeout(300)
    return True


def dismiss_notice_dialogs(page, max_modals: int = 5) -> int:
    """Schliesst alle nacheinander auftauchenden Hinweis-Modals (auf Schritt 2
    sind es zwei). Gibt die Anzahl der geschlossenen Modals zurueck."""
    count = 0
    for _ in range(max_modals):
        if not close_one_modal(page):
            break
        count += 1
    if count:
        log.info("%d Hinweis-Modal(e) geschlossen.", count)
    return count


def click_weiter(page, retries: int = 4) -> None:
    """Klickt 'Weiter'. Falls ein Modal/Overlay den Klick abfaengt, wird es
    weggeklickt und der Klick wiederholt."""
    button = page.locator(
        "#WeiterButton, input[type=submit][value='Weiter'], button:has-text('Weiter')"
    ).first
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        # Vor jedem Versuch pruefen, ob ein Modal im Weg steht
        dismiss_notice_dialogs(page)
        try:
            button.click(timeout=8000)
            log.info("'Weiter' geklickt (Versuch %d).", attempt)
            return
        except Exception as e:
            last_error = e
            log.info(
                "'Weiter'-Klick blockiert (Versuch %d/%d) - schliesse Overlay "
                "und versuche erneut.",
                attempt,
                retries,
            )
            page.wait_for_timeout(500)
    raise RuntimeError(f"'Weiter' liess sich nicht klicken: {last_error}")


def check_ulm_termine() -> tuple[bool, Path]:
    """Klickt sich bis zur Ergebnisseite durch und prueft auf freie Termine.
    Gibt (verfuegbar, screenshot_pfad) zurueck. Bucht nichts."""
    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = SCREENSHOT_DIR / f"termin_{timestamp}.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 1400})

        # Sicherheitsnetz fuer echte Browser-Dialoge (die Tevis-Hinweise sind
        # aber KEINE solchen - die werden ueber dismiss_notice_dialogs erledigt)
        page.on("dialog", lambda dialog: dialog.accept())

        # Schritt 0: Startseite laden, Cookie-Banner weg
        page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        dismiss_cookie_banner(page)
        debug_shot(page, "1_start")

        # Schritt 1: Staatsangehörigkeitsbehörde
        page.get_by_text("Staatsangehörigkeitsbehörde", exact=False).first.click()  # ANPASSEN falls Text abweicht
        page.wait_for_url("**/select2*", timeout=15000)
        dismiss_cookie_banner(page)
        dismiss_notice_dialogs(page)
        debug_shot(page, "2_select2")

        # Schritt 2: Checkbox "Anliegen rund um die Einbürgerung" anklicken
        try:
            page.get_by_role("checkbox", name=re.compile("Einbürgerung")).first.click(timeout=8000)
        except Exception:
            # Fallback: Checkbox in derselben Box wie der Text "Einbürgerung"  -- ANPASSEN
            log.info("Checkbox nicht ueber Label gefunden - nutze Fallback-Selektor.")
            page.locator("input[type=checkbox]:visible").first.click(timeout=8000)
        log.info("Checkbox angeklickt.")

        # Die beiden Hinweis-Modals erscheinen jetzt - wegklicken, sonst
        # blockiert das Overlay den 'Weiter'-Button
        dismiss_notice_dialogs(page)
        debug_shot(page, "3_after_checkbox")

        click_weiter(page)
        page.wait_for_url("**/location*", timeout=20000)
        dismiss_notice_dialogs(page)
        debug_shot(page, "4_location")

        # Schritt 3: Weiter auf der Standort-Seite
        click_weiter(page)
        page.wait_for_url("**/suggest*", timeout=20000)
        dismiss_notice_dialogs(page)

        # Schritt 4: Ergebnis auswerten
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(screenshot_path), full_page=True)

        page_text = page.inner_text("body").lower()
        no_slot_found = any(phrase in page_text for phrase in NO_SLOT_PHRASES)
        log.info(
            "Ergebnisseite ausgewertet - 'kein Termin'-Hinweis %s.",
            "gefunden" if no_slot_found else "NICHT gefunden",
        )

        browser.close()
        return (not no_slot_found), screenshot_path


def main() -> int:
    try:
        available, screenshot = check_ulm_termine()
    except Exception as e:
        log.error("Fehler beim Pruefen der Seite: %s", e)
        return 1

    if available:
        log.info("Moeglicherweise ein Termin frei! Screenshot: %s", screenshot)
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
