#!/usr/bin/env python3
"""
monitor_termin.py
==================

Prüft die Online-Terminvergabe der Stadt Ulm (Staatsangehörigkeitsbehörde /
Einbürgerung) auf freie Termine und schickt bei Erfolg eine E-Mail.
Es wird NICHTS gebucht - das Skript geht nur bis zur Kalenderansicht und
liest sie aus.

WICHTIG - bitte vor dem Einsatz lesen
--------------------------------------
1. Die Zielseite ist ein mehrstufiger, JavaScript-basierter Assistent
   (Kommunix "Terminvergabe"). Die genauen Texte/Selektoren für den
   Menüpunkt "Einbürgerung" innerhalb von "Staatsangehörigkeitsbehörde"
   kenne ich nicht mit Sicherheit, da ich die Seite nicht mit einem
   echten Browser ausführen konnte. Führe das Skript beim ersten Mal
   UNBEDINGT mit HEADLESS=false aus und schau zu, ob es die richtigen
   Schritte klickt. Passe ggf. die Textsuchen weiter unten an
   (Suche nach "ANPASSEN").
2. Die Erkennung "Termin verfügbar?" ist eine Heuristik (Suche nach
   Text wie "keine Termine" vs. Vorhandensein von anklickbaren
   Kalendertagen). Verifiziere die ersten paar Treffer manuell über
   den Screenshot, bevor du dich darauf verlässt.
3. Das Skript bucht nichts und füllt keine persönlichen Daten aus.
4. Zu häufiges/aggressives Abrufen kann als Bot-Traffic auffallen.
   Alle 15 Minuten ist ein vernünftiger, unauffälliger Rhythmus -
   nicht kürzer wählen.

Einrichtung
-----------
    pip install -r requirements.txt
    playwright install chromium

    Umgebungsvariablen setzen (z.B. in einer .env-Datei oder im Cron-Job):
        SMTP_HOST=smtp.gmail.com
        SMTP_PORT=587
        SMTP_USER=deine_adresse@gmail.com
        SMTP_PASSWORD=app-passwort         # kein normales Passwort, s.u.
        EMAIL_TO=deine_adresse@gmail.com
        HEADLESS=true                       # beim ersten Testlauf: false

Bei Gmail brauchst du ein "App-Passwort" (Google-Konto -> Sicherheit ->
2-Faktor-Auth aktivieren -> App-Passwörter), normale Passwörter
funktionieren per SMTP nicht mehr.

Automatisch alle 15 Minuten ausführen
--------------------------------------
Linux/Mac (crontab -e):
    */15 * * * * cd /pfad/zum/skript && /usr/bin/python3 monitor_termin.py >> monitor.log 2>&1

Windows: Taskplaner -> Aufgabe erstellen -> Trigger "alle 15 Minuten" ->
Aktion: python.exe monitor_termin.py

Alternativ: GitHub Actions mit einem "schedule"-Cron, falls das Skript
nicht dauerhaft auf einem eigenen Rechner laufen soll.
"""

import os
import sys
import smtplib
import logging
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://ssc.wilkencloud.de/ulm/"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# Textmuster, die auf "kein Termin frei" hindeuten (ANPASSEN falls nötig)
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


def send_email(subject: str, body: str, attachment: Path | None = None) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to_addr = os.environ["EMAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(body)

    if attachment and attachment.exists():
        msg.add_attachment(
            attachment.read_bytes(),
            maintype="image",
            subtype="png",
            filename=attachment.name,
        )

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    log.info("E-Mail verschickt an %s", to_addr)


def check_ulm_termine() -> tuple[bool, Path]:
    """Navigiert bis zur Kalenderansicht und prüft auf freie Termine.
    Gibt (verfuegbar, screenshot_pfad) zurueck. Bucht nichts."""
    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = SCREENSHOT_DIR / f"termin_{timestamp}.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=30000)

        # Schritt 1: Behörde auswählen -- ANPASSEN falls der Linktext abweicht
        page.get_by_text("Staatsangehörigkeitsbehörde", exact=False).first.click()
        page.wait_for_load_state("networkidle")

        # Schritt 2: Anliegen "Einbürgerung" auswählen -- ANPASSEN
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
        log.error("Fehler beim Prüfen der Seite: %s", e)
        return 1

    if available:
        log.info("Möglicherweise ein Termin frei! Screenshot: %s", screenshot)
        send_email(
            subject="Ulm Einbürgerung: möglicher freier Termin!",
            body=(
                "Die Terminseite zeigt keinen 'keine Termine'-Hinweis mehr.\n"
                f"Bitte SOFORT selbst pruefen und buchen: {URL}\n"
                f"Screenshot liegt bei: {screenshot}\n\n"
                "Hinweis: automatische Erkennung, bitte verifizieren."
            ),
            attachment=screenshot,
        )
    else:
        log.info("Kein Termin frei (Stand jetzt).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
