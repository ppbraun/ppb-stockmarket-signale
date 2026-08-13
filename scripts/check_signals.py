"""
PPB Stockmarket-Signale — Signal-Check & Dashboard-Datenversorgung (automatisch)

Macht zwei Dinge in einem Lauf:
1. Prueft das GDELT-Nachrichtenvolumen pro Thema, vergleicht es mit dem letzten
   Stand und schickt bei Auffaelligkeiten eine Telegram-Nachricht.
2. Ermittelt VOLLAUTOMATISCH relevante Ticker: Schnittmenge aus S&P-500-Mitgliedern
   (offen gepflegte Liste) und allen SEC-Form-4-Filings des letzten Handelstags
   (offizieller SEC-Bulk-Index). Fuer jeden Treffer werden echte Insider- und
   Makro-Signale ermittelt und nach docs/data.json geschrieben.

Politiker-Trades und Prediction Markets sind bewusst NICHT enthalten,
weil dafuer (noch) keine kostenlosen, offiziellen APIs angebunden sind.
Nutzt nur die Python-Standardbibliothek — kein pip install noetig.
"""

import os
import re
import csv
import io
import json
import time
import datetime
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
STATE_PATH = "data/last_state.json"
DASHBOARD_DATA_PATH = "docs/data.json"

# SEC verlangt einen aussagekraeftigen User-Agent mit Kontakt — bei Bedarf anpassen.
SEC_HEADERS = {"User-Agent": "PPB Stockmarket-Signale contact@example.com"}

MAX_CANDIDATES = 20            # Obergrenze, damit ein Lauf nicht zu lange dauert
MACRO_ARTICLE_THRESHOLD = 15
INSIDER_LOOKBACK_DAYS = 14

# ---------------------------------------------------------------------------
# Themenfelder fuer den Makro-Puls (News-Panel) — unveraendert
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

# SIC-Code-Praefixe -> ethische Kennzeichnung (heuristisch, nicht abschliessend)
SIC_ETHICS_MAP = {
    "348": "Rüstung",       # Ordnance & Accessories
    "376": "Rüstung",       # Guided Missiles, Space Vehicles
    "131": "Fossile Energie",  # Crude Petroleum & Natural Gas
    "291": "Fossile Energie",  # Petroleum Refining
    "138": "Fossile Energie",  # Oil & Gas Field Services
    "211": "Tabak",         # Cigarettes
    "799": "Glücksspiel",   # Services-Amusement/Gambling (grob)
}


# ---------------------------------------------------------------------------
# HTTP-Hilfsfunktionen
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
# GDELT
# ---------------------------------------------------------------------------

def gdelt_search(query, timespan="2d", maxrecords=250, retries=3):
    params = {"query": query, "mode": "ArtList", "maxrecords": maxrecords,
              "timespan": timespan, "format": "json"}
    url = GDELT_URL + "?" + urllib.parse.urlencode(params)

    last_error = None
    for attempt in range(retries):
        try:
            data = http_get_json(url, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
            return data.get("articles", [])
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429:
                wait = 8 * (attempt + 1)
                print(f"  GDELT-Rate-Limit (429), warte {wait}s und versuche erneut ...")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_error = e
            print(f"  GDELT-Anfrage fehlgeschlagen ({e}), warte 5s ...")
            time.sleep(5)
    raise last_error


def gdelt_count(query, timespan="2d"):
    return len(gdelt_search(query, timespan=timespan))


def gdelt_freshest_hours(articles):
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
# S&P 500 — Liste relevanter Ticker (automatisch, nicht kuratiert)
# ---------------------------------------------------------------------------

def load_sp500():
    text = http_get_text(SP500_CSV_URL, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
    reader = csv.DictReader(io.StringIO(text))

    fieldnames = reader.fieldnames or []
    if "CIK" not in fieldnames or "Symbol" not in fieldnames:
        print(f"Unerwartete Spalten in der S&P-500-Datei: {fieldnames}")
        print(f"Erste 300 Zeichen der Antwort: {text[:300]!r}")
        return {}

    companies = {}
    skipped = 0
    for row in reader:
        cik_raw = (row.get("CIK") or "")
        cik_digits = re.sub(r"[^0-9]", "", cik_raw)
        if not cik_digits:
            skipped += 1
            continue
        companies[int(cik_digits)] = {
            "tk": (row.get("Symbol") or "").strip(),
            "name": (row.get("Security") or "").strip(),
            "sector": (row.get("GICS Sector") or "").strip(),
        }

    if skipped:
        print(f"{skipped} S&P-500-Zeilen ohne verwertbare CIK übersprungen.")
    print(f"S&P-500-Liste geladen: {len(companies)} Firmen.")
    return companies


# ---------------------------------------------------------------------------
# SEC — Tages-Index aller Filings (fuer die automatische Auswahl)
# ---------------------------------------------------------------------------

def fetch_recent_form4_ciks(max_lookback_days=10):
    """Sucht rueckwaerts den letzten Handelstag mit einem verfuegbaren master.idx
    und liefert die Menge aller CIKs, die an diesem Tag ein Form-4 eingereicht haben."""
    today = datetime.date.today()
    for offset in range(max_lookback_days):
        d = today - datetime.timedelta(days=offset)
        quarter = (d.month - 1) // 3 + 1
        date_str = d.strftime("%Y%m%d")
        url = f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{quarter}/master.{date_str}.idx"
        try:
            text = http_get_text(url, headers=SEC_HEADERS)
        except Exception:
            continue

        lines = text.splitlines()
        try:
            start = next(i for i, l in enumerate(lines) if set(l.strip()) == {"-"}) + 1
        except StopIteration:
            continue

        ciks = set()
        for line in lines[start:]:
            parts = line.split("|")
            if len(parts) != 5:
                continue
            cik_str, _name, form_type, _date, _fname = parts
            if form_type.strip() == "4":
                try:
                    ciks.add(int(cik_str.strip()))
                except ValueError:
                    pass

        if ciks:
            print(f"Form-4-Index gefunden fuer {d.isoformat()}: {len(ciks)} CIKs mit Insider-Filing.")
            return ciks, d

    print("Kein Form-4-Index in den letzten Tagen gefunden.")
    return set(), None


# ---------------------------------------------------------------------------
# SEC — Details je Kandidat (Insider-Filing, SIC)
# ---------------------------------------------------------------------------

def fetch_submission_details(cik10):
    url = SEC_SUBMISSIONS_URL.format(cik=cik10)
    return http_get_json(url, headers=SEC_HEADERS)


def extract_recent_form4(submission_data):
    recent = submission_data.get("filings", {}).get("recent", {})
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
        age_hours = max((datetime.date.today() - filing_date).days * 24, 1)
        return {"filing_date": date_str, "age_hours": age_hours,
                "accession": accession, "primary_doc": primary_doc}
    return None


def fetch_form4_direction(cik10, accession, primary_doc):
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


def classify_ethics(sic_code):
    if not sic_code:
        return []
    prefix3 = sic_code[:3]
    tag = SIC_ETHICS_MAP.get(prefix3)
    return [tag] if tag else []


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=data)
    urllib.request.urlopen(req, timeout=15)


# ---------------------------------------------------------------------------
# Zustand laden/speichern
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
# Themen-Puls (Telegram-Alarm + News-Panel) — unveraendert zur Vorversion
# ---------------------------------------------------------------------------

def check_topics():
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

        time.sleep(3)

    save_state(new_state)

    if alerts:
        message = ("📊 <b>PPB Stockmarket-Signale</b>\nAuffällige Nachrichtenlage entdeckt:\n\n"
                    + "\n".join(alerts)
                    + "\n\nDashboard: https://ppbraun.github.io/ppb-stockmarket-signale/")
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


# ---------------------------------------------------------------------------
# Automatische Ticker-Auswahl + Signale
# ---------------------------------------------------------------------------

def build_ticker_signals_auto():
    print("Lade S&P-500-Liste ...")
    try:
        sp500 = load_sp500()
    except Exception as e:
        print(f"S&P-500-Liste konnte nicht geladen werden: {e}")
        return []

    form4_ciks, filing_date = fetch_recent_form4_ciks()
    if not form4_ciks:
        return []

    candidates = [(cik, info) for cik, info in sp500.items() if cik in form4_ciks]
    candidates = candidates[:MAX_CANDIDATES]
    print(f"{len(candidates)} S&P-500-Titel mit Insider-Filing am {filing_date} (nach Obergrenze {MAX_CANDIDATES}).")

    results = []

    for cik, info in candidates:
        tk = info["tk"]
        cik10 = str(cik).zfill(10)
        print(f"Pruefe {tk} ({info['name']}) ...")

        insider = {"active": False}
        ethics = []
        try:
            submission = fetch_submission_details(cik10)
            sic = submission.get("sic", "")
            ethics = classify_ethics(sic)

            filing = extract_recent_form4(submission)
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

        time.sleep(3)

        macro = {"active": False}
        try:
            articles = gdelt_search(info["name"])
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

        time.sleep(3)

        active_count = sum([insider["active"], macro["active"]])
        why_parts = []
        if insider["active"]:
            why_parts.append("aktuelles SEC-Form-4-Insider-Filing")
        if macro["active"]:
            why_parts.append("erhöhte Nachrichtenaufmerksamkeit")
        why = ("Live-Signal: " + " + ".join(why_parts) + ".") if why_parts else \
              "Insider-Filing vorhanden, aber (noch) keine erhöhte Nachrichtenaufmerksamkeit."

        results.append({
            "tk": tk,
            "name": info["name"],
            "broker": True,
            "market": "us",
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
            "topic": info["sector"] or "Unternehmensspezifisch",
            "ethics": ethics,
            "why": why,
        })

    return results


def main():
    news_topics = check_topics()
    tickers = build_ticker_signals_auto()

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "newsTopics": news_topics,
        "tickers": tickers,
    }
    save_dashboard_data(payload)
    print(f"docs/data.json geschrieben ({len(tickers)} Ticker, {len(news_topics)} Themen).")


if __name__ == "__main__":
    main()
