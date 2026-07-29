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
2. .../select2?md=4                            -> Checkbox "...Einbürgerung" anklicken,
                                                   dann "Weiter" -> 2 Popups mit OK bestaetigen
3. .../location?mdt=19&select_cnc=1&cnc-600=1  -> "Weiter" klicken
4. .../suggest                                 -> Zielseite. Zeigt "Kein freier Termin
                                                   verfügbar", wenn nichts frei ist.

WICHTIG - bitte vor dem Einsatz lesen
--------------------------------------
1. Wenn sich die Seite mal aendert: im Screenshot-Artefakt nachsehen, wo es
   haengen bleibt, und die mit "ANPASSEN" markierten Stellen korrigieren.
2. Die zwei Popups werden generisch abgefangen (egal was genau drinsteht)
   und automatisch bestaetigt.
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
    """Klickt den Cookie-Banner weg, falls er auftaucht. Ignoriert es,
    wenn er nicht da ist."""
    try:
        page.get_by_role("button", name="Ablehnen").click(timeout=5000)
        log.info("Cookie-Banner abgelehnt.")
    except Exception:
        log.info("Kein Cookie-Banner gefunden (oder schon weg) - weiter.")


def click_weiter(page) -> None:
    """Klickt den 'Weiter'-Button auf der aktuellen Seite (wartet automatisch,
    bis er aktiviert ist)."""
    page.get_by_role("button", name="Weiter").first.click()


def check_ulm_termine() -> tuple[bool, Path]:
    """Klickt sich bis zur Ergebnisseite durch und prueft auf freie Termine.
    Gibt (verfuegbar, screenshot_pfad) zurueck. Bucht nichts."""
    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = SCREENSHOT_DIR / f"termin_{timestamp}.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        # Alle Popups/Dialoge (window.confirm/alert) automatisch mit "OK" bestaetigen
        page.on("dialog", lambda dialog: dialog.accept())

        # Schritt 0: Startseite laden, Cookie-Banner weg
        page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        dismiss_cookie_banner(page)

        # Schritt 1: Staatsangehörigkeitsbehörde
        page.get_by_text("Staatsangehörigkeitsbehörde", exact=False).first.click()  # ANPASSEN falls Text abweicht
        page.wait_for_url("**/select2*", timeout=15000)
        dismiss_cookie_banner(page)  # falls er hier erneut auftaucht

        # Schritt 2: Checkbox "Anliegen rund um die Einbürgerung" anklicken
        try:
            page.get_by_role("checkbox", name=re.compile("Einbürgerung")).click(timeout=10000)
        except Exception:
            # Fallback: naeheste Checkbox zum Text "Einbürgerung" suchen  -- ANPASSEN falls das auch fehlschlaegt
            page.locator("text=Einbürgerung").locator(
                "xpath=ancestor::*[self::div or self::tr][1]//input[@type='checkbox']"
            ).first.click(timeout=10000)

        # "Weiter" auf dieser Seite -> danach kommen die 2 Popups (oben automatisch bestaetigt)
        click_weiter(page)
        page.wait_for_url("**/location*", timeout=15000)

        # Schritt 3: Weiter auf der Location-Seite
        click_weiter(page)
        page.wait_for_url("**/suggest*", timeout=15000)

        # Schritt 4: Ergebnis auswerten
        page.wait_for_timeout(1500)
        page.screenshot(path=str(screenshot_path), full_page=True)

        page_text = page.inner_text("body").lower()
        no_slot_found = any(phrase in page_text for phrase in NO_SLOT_PHRASES)

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
        send_ntfy(
            subject="Ulm Einbürgerung: möglicher freier Termin!",
            body=(
                "Die Ergebnisseite zeigt 'Kein freier Termin verfügbar' nicht "
                "mehr an. Bitte sofort selbst prüfen und buchen."
            ),
            attachment=screenshot,
        )
    else:
        log.info("Kein Termin frei (Stand jetzt).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
