"""
PPB Stockmarket-Signale — Signal-Check & Dashboard-Datenversorgung

Macht zwei Dinge in einem Lauf:
1. Prueft das GDELT-Nachrichtenvolumen pro Thema, vergleicht es mit dem letzten
   Stand und schickt bei Auffaelligkeiten eine Telegram-Nachricht.
2. Holt fuer eine feste Watchlist echte Insider-Signale (SEC Form 4) und
   Makro-Signale (GDELT, firmenbezogen) und schreibt sie nach docs/data.json,
   von wo das Dashboard sie laedt.

Politiker-Trades und Prediction Markets sind hier bewusst NICHT enthalten,
weil dafuer (noch) keine kostenlosen, offiziellen APIs angebunden sind.
Nutzt nur die Python-Standardbibliothek — kein pip install noetig.
"""

import os
import json
import time
import datetime
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
STATE_PATH = "data/last_state.json"
DASHBOARD_DATA_PATH = "docs/data.json"

# SEC verlangt einen aussagekraeftigen User-Agent mit Kontakt — bei Bedarf anpassen.
SEC_HEADERS = {"User-Agent": "PPB Stockmarket-Signale contact@example.com"}

# ---------------------------------------------------------------------------
# Themenfelder fuer den Makro-Puls (News-Panel)
# ---------------------------------------------------------------------------
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
THRESHOLD_COUNT = 200
THRESHOLD_JUMP_PCT = 50

# ---------------------------------------------------------------------------
# Watchlist fuer die echten Ticker-Signale (Insider + Makro)
# ---------------------------------------------------------------------------
WATCHLIST = [
    {"tk": "NEM", "name": "Newmont Corp.", "gdelt_query": "Newmont Corporation",
     "topic": "Edelmetalle / Goldminen", "market": "us", "broker": True, "ethics": []},
    {"tk": "LMT", "name": "Lockheed Martin", "gdelt_query": "Lockheed Martin",
     "topic": "Verteidigung / Rüstung", "market": "us", "broker": True, "ethics": ["Rüstung"]},
    {"tk": "DELL", "name": "Dell Technologies", "gdelt_query": "Dell Technologies",
     "topic": "KI-Infrastruktur", "market": "us", "broker": True, "ethics": []},
    {"tk": "CVX", "name": "Chevron", "gdelt_query": "Chevron Corporation",
     "topic": "Energie / Öl & Gas", "market": "us", "broker": True, "ethics": ["Fossile Energie"]},
    {"tk": "RTX", "name": "RTX Corporation", "gdelt_query": "RTX Corporation OR Raytheon",
     "topic": "Verteidigung / Rüstung", "market": "us", "broker": True, "ethics": ["Rüstung"]},
    {"tk": "NVDA", "name": "Nvidia Corp.", "gdelt_query": "Nvidia Corporation",
     "topic": "Halbleiter / Chips", "market": "us", "broker": True, "ethics": []},
    {"tk": "UNH", "name": "UnitedHealth Group", "gdelt_query": "UnitedHealth Group",
     "topic": "Gesundheit / FDA-Zulassungen", "market": "us", "broker": True, "ethics": []},
    {"tk": "COIN", "name": "Coinbase Global", "gdelt_query": "Coinbase Global",
     "topic": "Krypto-Regulierung", "market": "us", "broker": True, "ethics": []},
]

MACRO_ARTICLE_THRESHOLD = 15   # ab dieser Artikelzahl (48h) gilt ein Ticker als "Makro aktiv"
INSIDER_LOOKBACK_DAYS = 14     # Form-4-Filings, die aelter sind, zaehlen nicht mehr als "aktiv"


# ---------------------------------------------------------------------------
# Hilfsfunktionen: HTTP
# ---------------------------------------------------------------------------

def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ppb-stockmarket-signale/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_text(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ppb-stockmarket-signale/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# GDELT — Themen-Puls (fuer Telegram-Alarm + News-Panel)
# ---------------------------------------------------------------------------

def gdelt_search(query, timespan="2d", maxrecords=250):
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": maxrecords,
        "timespan": timespan,
        "format": "json",
    }
    url = GDELT_URL + "?" + urllib.parse.urlencode(params)
    data = http_get_json(url, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
    return data.get("articles", [])


def gdelt_count(query, timespan="2d"):
    return len(gdelt_search(query, timespan=timespan))


def gdelt_freshest_hours(articles):
    """Schaetzt das Alter des juengsten Artikels in Stunden (grob, aus 'seendate')."""
    if not articles:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    newest = None
    for a in articles:
        raw = a.get("seendate")
        if not raw:
            continue
        try:
            dt = datetime.datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if newest is None or dt > newest:
            newest = dt
    if newest is None:
        return None
    return round((now - newest).total_seconds() / 3600)


# ---------------------------------------------------------------------------
# SEC EDGAR — Insider-Signale (Form 4)
# ---------------------------------------------------------------------------

def load_sec_ticker_map():
    data = http_get_json(SEC_TICKERS_URL, headers=SEC_HEADERS)
    mapping = {}
    for entry in data.values():
        mapping[entry["ticker"].upper()] = str(entry["cik_str"]).zfill(10)
    return mapping


def fetch_recent_form4(cik10):
    url = SEC_SUBMISSIONS_URL.format(cik=cik10)
    data = http_get_json(url, headers=SEC_HEADERS)
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    cutoff = datetime.date.today() - datetime.timedelta(days=INSIDER_LOOKBACK_DAYS)

    for form, date_str, accession, primary_doc in zip(forms, dates, accessions, docs):
        if form != "4":
            continue
        filing_date = datetime.date.fromisoformat(date_str)
        if filing_date < cutoff:
            continue
        age_hours = (datetime.date.today() - filing_date).days * 24
        return {
            "filing_date": date_str,
            "age_hours": max(age_hours, 1),
            "accession": accession,
            "primary_doc": primary_doc,
        }
    return None


def fetch_form4_direction(cik10, accession, primary_doc):
    """Versucht Kauf/Verkauf aus dem eigentlichen Form-4-Dokument zu lesen."""
    accession_no_dashes = accession.replace("-", "")
    cik_numeric = str(int(cik10))
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_numeric}/{accession_no_dashes}/{primary_doc}"
    try:
        xml_text = http_get_text(url, headers=SEC_HEADERS)
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"  (Form-4-Detail nicht lesbar: {e})")
        return None

    for tag in ("nonDerivativeTransaction", "derivativeTransaction"):
        for elem in root.iter(tag):
            code_elem = elem.find(".//transactionCode")
            if code_elem is not None and code_elem.text:
                code = code_elem.text.strip()
                if code == "P":
                    return "Kauf"
                if code == "S":
                    return "Verkauf"
    return None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Zustand laden/speichern (fuer den Telegram-Vergleich)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def save_dashboard_data(payload):
    os.makedirs(os.path.dirname(DASHBOARD_DATA_PATH), exist_ok=True)
    with open(DASHBOARD_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------

def check_topics():
    """Themen-Puls pruefen, Telegram-Alarm ausloesen, Werte fuers News-Panel zurueckgeben."""
    old_state = load_state()
    new_state = {}
    alerts = []
    news_topics = []

    for topic, query in TOPICS.items():
        try:
            count = gdelt_count(query)
        except Exception as e:
            print(f"Fehler bei Thema '{topic}': {e}")
            count = old_state.get(topic, 0)

        old_count = old_state.get(topic, 0)
        new_state[topic] = count
        news_topics.append({"topic": topic, "vol": count})

        crossed = count >= THRESHOLD_COUNT and old_count < THRESHOLD_COUNT
        jumped = old_count > 0 and ((count - old_count) / old_count * 100) >= THRESHOLD_JUMP_PCT
        if crossed or jumped:
            alerts.append(f"🟢 <b>{topic}</b>: {count} Artikel (zuletzt {old_count})")

        time.sleep(1)

    save_state(new_state)

    if alerts:
        message = (
            "📊 <b>PPB Stockmarket-Signale</b>\n"
            "Auffällige Nachrichtenlage entdeckt:\n\n"
            + "\n".join(alerts)
            + "\n\nDashboard: https://ppbraun.github.io/ppb-stockmarket-signale/"
        )
        try:
            send_telegram(message)
            print("Telegram-Benachrichtigung gesendet.")
        except Exception as e:
            print(f"Telegram-Versand fehlgeschlagen: {e}")
    else:
        print("Keine Auffaelligkeiten in diesem Durchlauf.")

    news_topics.sort(key=lambda x: x["vol"], reverse=True)
    max_vol = max((n["vol"] for n in news_topics), default=1) or 1
    for n in news_topics:
        n["pct"] = round(n["vol"] / max_vol * 100)
    return news_topics


def build_ticker_signals():
    """Fuer jede Watchlist-Aktie echte Insider- und Makro-Signale ermitteln."""
    print("Lade SEC-Ticker-Zuordnung ...")
    try:
        ticker_map = load_sec_ticker_map()
    except Exception as e:
        print(f"SEC-Ticker-Zuordnung konnte nicht geladen werden: {e}")
        ticker_map = {}

    results = []

    for entry in WATCHLIST:
        tk = entry["tk"]
        print(f"Pruefe {tk} ...")

        # --- Insider-Signal (SEC Form 4) ---
        insider = {"active": False}
        cik10 = ticker_map.get(tk)
        if cik10:
            try:
                filing = fetch_recent_form4(cik10)
                if filing:
                    direction = fetch_form4_direction(cik10, filing["accession"], filing["primary_doc"])
                    insider = {
                        "active": True,
                        "age": f"{filing['age_hours']}Std" if filing["age_hours"] < 24
                               else f"{filing['age_hours'] // 24}T",
                        "src": "SEC",
                    }
                    if direction:
                        insider["dir"] = direction
            except Exception as e:
                print(f"  Insider-Check fehlgeschlagen: {e}")
        else:
            print(f"  Keine CIK fuer {tk} gefunden.")

        time.sleep(1)

        # --- Makro-Signal (GDELT, firmenbezogen) ---
        macro = {"active": False}
        try:
            articles = gdelt_search(entry["gdelt_query"])
            count = len(articles)
            if count >= MACRO_ARTICLE_THRESHOLD:
                fresh_hours = gdelt_freshest_hours(articles)
                if fresh_hours is not None and fresh_hours < 24:
                    age_label = f"{fresh_hours}Std"
                elif fresh_hours is not None:
                    age_label = f"{fresh_hours // 24}T"
                else:
                    age_label = "48Std"
                macro = {"active": True, "age": age_label}
        except Exception as e:
            print(f"  Makro-Check fehlgeschlagen: {e}")

        time.sleep(1)

        active_count = sum([insider["active"], macro["active"]])
        why_parts = []
        if insider["active"]:
            why_parts.append("aktuelles SEC-Form-4-Insider-Filing")
        if macro["active"]:
            why_parts.append("erhöhte Nachrichtenaufmerksamkeit")
        why = ("Live-Signal: " + " + ".join(why_parts) + ".") if why_parts else \
              "Aktuell kein aktives Live-Signal (Politiker-Trades und Prediction Markets noch nicht angebunden)."

        results.append({
            "tk": tk,
            "name": entry["name"],
            "broker": entry["broker"],
            "market": entry["market"],
            "score": active_count,
            "reaction": None,
            "politician": None,
            "track": None,
            "sig": {
                "congress": {"active": False},
                "insider": insider,
                "macro": macro,
                "predict": {"active": False},
            },
            "topic": entry["topic"],
            "ethics": entry["ethics"],
            "why": why,
        })

    return results


def main():
    news_topics = check_topics()
    tickers = build_ticker_signals()

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "newsTopics": news_topics,
        "tickers": tickers,
    }
    save_dashboard_data(payload)
    print(f"docs/data.json geschrieben ({len(tickers)} Ticker, {len(news_topics)} Themen).")


if __name__ == "__main__":
    main()
