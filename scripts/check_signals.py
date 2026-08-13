"""
PPB Stockmarket-Signale — Signal-Check
Holt aktuelles Nachrichtenvolumen pro Thema von GDELT, vergleicht es mit dem
letzten bekannten Stand und sendet bei Auffälligkeiten eine Telegram-Nachricht.

Läuft periodisch über GitHub Actions (.github/workflows/check.yml).
Nutzt nur die Python-Standardbibliothek — kein pip install nötig.
"""

import os
import json
import time
import urllib.parse
import urllib.request

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
STATE_PATH = "data/last_state.json"

# Dieselben 11 Themenfelder wie im Dashboard, mit einer Suchanfrage je Thema.
TOPICS = {
    "Zölle / Handelskonflikt": "tariffs OR \"trade war\"",
    "Fed-Zinspolitik": "\"Federal Reserve\" interest rate",
    "Edelmetalle / Goldminen": "gold mining OR \"gold price\"",
    "Verteidigung / Rüstung": "defense contractor OR weapons manufacturer",
    "KI-Infrastruktur": "\"AI data center\" OR \"AI infrastructure\"",
    "Halbleiter / Chips": "semiconductor chips",
    "Energie / Öl & Gas": "\"oil price\" OR \"energy market\"",
    "Gesundheit / FDA-Zulassungen": "FDA approval drug",
    "Schifffahrt / Lieferketten": "shipping \"supply chain\"",
    "Krypto-Regulierung": "crypto regulation",
    "Cybersecurity": "cybersecurity breach",
}

# Ab wann ein Thema als "auffällig" gilt:
THRESHOLD_COUNT = 200      # absolute Artikelzahl (GDELT liefert max. 250 pro Anfrage)
THRESHOLD_JUMP_PCT = 50    # oder: prozentualer Anstieg seit dem letzten Check


def fetch_count(query):
    """Fragt GDELT nach der Artikelanzahl der letzten 48h für ein Thema."""
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": 250,
        "timespan": "2d",
        "format": "json",
    }
    url = GDELT_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return len(data.get("articles", []))


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=data)
    urllib.request.urlopen(req, timeout=15)


def main():
    old_state = load_state()
    new_state = {}
    alerts = []

    for topic, query in TOPICS.items():
        try:
            count = fetch_count(query)
        except Exception as e:
            print(f"Fehler bei '{topic}': {e}")
            continue

        old_count = old_state.get(topic, 0)
        new_state[topic] = count

        crossed_threshold = count >= THRESHOLD_COUNT and old_count < THRESHOLD_COUNT
        jumped = old_count > 0 and ((count - old_count) / old_count * 100) >= THRESHOLD_JUMP_PCT

        if crossed_threshold or jumped:
            alerts.append(f"🟢 <b>{topic}</b>: {count} Artikel (zuletzt {old_count})")

        time.sleep(1)  # GDELT nicht mit Anfragen überlasten

    if alerts:
        message = (
            "📊 <b>PPB Stockmarket-Signale</b>\n"
            "Auffällige Nachrichtenlage entdeckt:\n\n"
            + "\n".join(alerts)
            + "\n\nDashboard: https://ppbraun.github.io/ppb-stockmarket-signale/"
        )
        send_telegram(message)
        print("Benachrichtigung gesendet.")
    else:
        print("Keine Auffälligkeiten in diesem Durchlauf.")

    save_state(new_state)


if __name__ == "__main__":
    main()
