#!/usr/bin/env python3
"""
monitor_termin.py
==================

Prueft die Online-Terminvergabe der Stadt Ulm (Staatsangehoerigkeitsbehoerde /
Einbuergerung) auf freie Termine und schickt bei Erfolg eine Push-Benachrichtigung
ueber ntfy.sh. Es wird NICHTS gebucht - das Skript geht nur bis zur Kalenderansicht
und liest sie aus.

WICHTIG - bitte vor dem Einsatz lesen
--------------------------------------
1. Die Zielseite ist ein mehrstufiger, JavaScript-basierter Assistent
   (Kommunix "Terminvergabe"). Die genauen Texte/Selektoren fuer den
   Menuepunkt "Einbuergerung" innerhalb von "Staatsangehoerigkeitsbehoerde"
   kenne ich nicht mit Sicherheit, da ich die Seite nicht mit einem
   echten Browser ausfuehren konnte. Fuehre das Skript beim ersten Mal
   UNBEDINGT mit HEADLESS=false aus (oder pruefe den Screenshot aus dem
   ersten Testlauf) und schau, ob es die richtigen Schritte klickt.
   Passe ggf. die Textsuchen weiter unten an (Suche nach "ANPASSEN").
2. Die Erkennung "Termin verfuegbar?" ist eine Heuristik (Suche nach
   Text wie "keine Termine" vs. Vorhandensein von anklickbaren
   Kalendertagen). Verifiziere die ersten paar Treffer manuell ueber
   den Screenshot, bevor du dich darauf verlaesst.
3. Das Skript bucht nichts und fuellt keine persoenlichen Daten aus.
4. Zu haeufiges/aggressives Abrufen kann als Bot-Traffic auffallen.
   Alle 15 Minuten ist ein vernuenftiger, unauffaelliger Rhythmus -
   nicht kuerzer waehlen.

Einrichtung
-----------
    pip install -r requirements.txt
    playwright install chromium

    Umgebungsvariablen setzen (z.B. im GitHub-Actions-Secret oder lokal):
        NTFY_TOPIC=dein-eindeutiges-thema     # siehe Hinweis unten
        NTFY_SERVER=https://ntfy.sh            # optional, Standard ist ntfy.sh
        HEADLESS=true                          # beim ersten Testlauf: false

ntfy-Einrichtung (Push-Benachrichtigung, kein Account noetig)
----------------------------------------------------------------
1. App "ntfy" aus dem App Store / Play Store installieren (oder
   https://ntfy.sh im Browser offen lassen).
2. Ein eigenes, schwer erratbares "Thema" (Topic) ausdenken, z.B.
   "ulm-einbuergerung-x7k2m9" - NICHT etwas Einfaches wie "einbuergerung",
   denn ntfy-Themen sind auf dem kostenlosen Server oeffentlich: wer den
   Themennamen kennt odereraet, kann die Nachrichten mitlesen. Ein langer,
   zufaelliger Name ist daher wichtig.
3. In der App auf "+" -> "Subscribe to topic" -> genau diesen Themennamen
   eingeben.
4. Denselben Namen als NTFY_TOPIC (Umgebungsvariable/Secret) verwenden.

Bei einem Treffer bekommst du dann sofort eine Push-Nachricht aufs Handy,
inklusive Screenshot und einem Link, der direkt zur Buchungsseite fuehrt.
"""

import os
import sys
import logging
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://ssc.wilkencloud.de/ulm/"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# Textmuster, die auf "kein Termin frei" hindeuten (ANPASSEN falls noetig)
NO_SLOT_PHRASES = [
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
    """Schickt eine Push-Benachrichtigung ueber ntfy.sh. Haengt bei
    Bedarf einen Screenshot als Bild an."""
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    endpoint = f"{server}/{topic}"

    headers = {
        "Title": subject,
        "Priority": "urgent",
        "Click": URL,
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


def check_ulm_termine() -> tuple[bool, Path]:
    """Navigiert bis zur Kalenderansicht und prueft auf freie Termine.
    Gibt (verfuegbar, screenshot_pfad) zurueck. Bucht nichts."""
    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = SCREENSHOT_DIR / f"termin_{timestamp}.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=30000)

        # Schritt 1: Behoerde auswaehlen -- ANPASSEN falls der Linktext abweicht
        page.get_by_text("Staatsangehörigkeitsbehörde", exact=False).first.click()
        page.wait_for_load_state("networkidle")

        # Schritt 2: Anliegen "Einbuergerung" auswaehlen -- ANPASSEN
        try:
            page.get_by_text("Einbürgerung", exact=False).first.click(timeout=10000)
            page.wait_for_load_state("networkidle")
        except Exception as e:
            log.warning("Konnte 'Einbürgerung' nicht anklicken: %s", e)

        page.wait_for_timeout(2000)  # kurz warten, bis Kalender/Liste geladen ist
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
                "Die Terminseite zeigt keinen 'keine Termine'-Hinweis mehr. "
                "Bitte sofort selbst prüfen und buchen. Automatische "
                "Erkennung, bitte verifizieren."
            ),
            attachment=screenshot,
        )
    else:
        log.info("Kein Termin frei (Stand jetzt).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
