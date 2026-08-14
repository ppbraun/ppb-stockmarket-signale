"""
PPB Stockmarket-Signale — Signal-Check & Dashboard-Datenversorgung (automatisch)

Macht zwei Dinge in einem Lauf:
1. Prueft das GDELT-Nachrichtenvolumen pro Thema, vergleicht es mit dem letzten
   Stand und schickt bei Auffaelligkeiten eine Telegram-Nachricht.
2. Ermittelt VOLLAUTOMATISCH relevante Ticker: Schnittmenge aus S&P-500-Mitgliedern
   (offen gepflegte Liste) und allen SEC-Form-4-Filings des letzten Handelstags
   (offizieller SEC-Bulk-Index). Fuer jeden Treffer werden echte Insider-, Makro-
   und Predict-Signale (Polymarket) sowie Kurs (Stooq+FX), Kursreaktion seit dem
   ersten Auftauchen und eine Wikipedia-Kurzbeschreibung ermittelt und nach
   docs/data.json geschrieben.

Politiker-Trades sind bewusst NICHT enthalten, weil es dafuer kein kostenloses,
offizielles API gibt (nur Scraping moeglich waere).
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
STOOQ_URL = "https://stooq.com/q/l/?s={ticker}.us&f=sd2t2ohlcv&h&e=csv"
FX_URL = "https://api.frankfurter.app/latest?from=USD&to=EUR"
POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
STATE_PATH = "data/last_state.json"
DASHBOARD_DATA_PATH = "docs/data.json"

# SEC verlangt einen aussagekraeftigen User-Agent mit Kontakt — bei Bedarf anpassen.
SEC_HEADERS = {"User-Agent": "PPB Stockmarket-Signale contact@example.com"}

MAX_CANDIDATES = 8             # Obergrenze, damit ein Lauf nicht zu lange dauert
MACRO_ARTICLE_THRESHOLD = 15
INSIDER_LOOKBACK_DAYS = 14
PREDICT_VOLUME_THRESHOLD = 10000   # USD Handelsvolumen, ab dem ein Polymarket-Treffer zaehlt

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

def gdelt_search(query, timespan="2d", maxrecords=250, retries=2):
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
                wait = 10 * (attempt + 1)
                print(f"  GDELT-Rate-Limit (429), warte {wait}s und versuche erneut ...", flush=True)
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last_error = e
            print(f"  GDELT-Anfrage fehlgeschlagen ({e}), warte 4s ...", flush=True)
            time.sleep(4)
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
# Kursdaten (Stooq) + Wechselkurs (Frankfurter/EZB)
# ---------------------------------------------------------------------------

def fetch_usd_eur_rate():
    try:
        data = http_get_json(FX_URL, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
        return data.get("rates", {}).get("EUR")
    except Exception as e:
        print(f"Wechselkurs konnte nicht geladen werden: {e}")
        return None


def fetch_price_usd(ticker):
    url = STOOQ_URL.format(ticker=ticker.lower())
    try:
        text = http_get_text(url, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
        reader = csv.DictReader(io.StringIO(text))
        row = next(reader, None)
        if not row:
            return None
        close = row.get("Close")
        if not close or close in ("N/D", "N/A"):
            return None
        return float(close)
    except Exception as e:
        print(f"  Kursabfrage fehlgeschlagen: {e}")
        return None


# ---------------------------------------------------------------------------
# Firmenbeschreibung (Wikipedia — "was macht das Unternehmen")
# ---------------------------------------------------------------------------

WIKI_HEADERS = {"User-Agent": "PPB Stockmarket-Signale (contact@example.com)"}


def fetch_company_description(name, max_len=180):
    """Holt eine kurze Wikipedia-Zusammenfassung; deutsch zuerst, sonst englisch."""
    for lang in ("de", "en"):
        url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(name)}"
        try:
            data = http_get_json(url, headers=WIKI_HEADERS)
        except Exception:
            continue
        extract = (data.get("extract") or "").strip()
        if not extract:
            continue
        extract = extract.replace("\n", " ")
        if len(extract) > max_len:
            cut = extract[:max_len].rsplit(" ", 1)[0]
            extract = cut + "…"
        return extract
    return None


# ---------------------------------------------------------------------------
# Prediction Markets (Polymarket) — echtes Predict-Signal je Ticker
# ---------------------------------------------------------------------------

def fetch_polymarket_signal(query):
    """Sucht per Volltextsuche nach zur Firma passenden, offenen Maerkten.
    Aktiv, wenn ein Markt mit nennenswertem Handelsvolumen gefunden wird."""
    params = {"q": query}
    url = POLYMARKET_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    try:
        data = http_get_json(url, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
    except Exception as e:
        print(f"  Polymarket-Suche fehlgeschlagen: {e}")
        return {"active": False}

    markets = list(data.get("markets") or [])
    for ev in (data.get("events") or []):
        markets.extend(ev.get("markets") or [])

    best_volume = 0.0
    for m in markets:
        if m.get("closed"):
            continue
        try:
            vol = float(m.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        best_volume = max(best_volume, vol)

    if best_volume >= PREDICT_VOLUME_THRESHOLD:
        return {"active": True}
    return {"active": False}


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

def build_ticker_signals_auto(old_tickers_state):
    print("Lade S&P-500-Liste ...")
    try:
        sp500 = load_sp500()
    except Exception as e:
        print(f"S&P-500-Liste konnte nicht geladen werden: {e}")
        return [], {}

    form4_ciks, filing_date = fetch_recent_form4_ciks()
    if not form4_ciks:
        return [], {}

    candidates = [(cik, info) for cik, info in sp500.items() if cik in form4_ciks]
    candidates = candidates[:MAX_CANDIDATES]
    print(f"{len(candidates)} S&P-500-Titel mit Insider-Filing am {filing_date} (nach Obergrenze {MAX_CANDIDATES}).")

    print("Lade USD/EUR-Wechselkurs ...")
    fx_rate = fetch_usd_eur_rate()
    if fx_rate:
        print(f"Wechselkurs USD->EUR: {fx_rate}")
    else:
        print("Kein Wechselkurs verfügbar — Kurs (€) bleibt leer.")

    results = []
    new_tickers_state = {}
    today_str = datetime.date.today().isoformat()

    for cik, info in candidates:
        tk = info["tk"]
        cik10 = str(cik).zfill(10)
        print(f"Pruefe {tk} ({info['name']}) ...")

        insider = {"active": False}
        ethics = []
        description = None
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

        time.sleep(1)

        try:
            description = fetch_company_description(info["name"])
        except Exception as e:
            print(f"  Firmenbeschreibung fehlgeschlagen: {e}")

        time.sleep(1)

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

        time.sleep(1)

        predict = {"active": False}
        try:
            predict = fetch_polymarket_signal(info["name"])
        except Exception as e:
            print(f"  Predict-Check fehlgeschlagen: {e}")

        time.sleep(1)

        price_usd = fetch_price_usd(tk)
        price_eur = round(price_usd * fx_rate, 2) if (price_usd is not None and fx_rate) else None

        time.sleep(1)

        # Kursreaktion seit dem ersten Auftauchen dieses Tickers berechnen
        prev = old_tickers_state.get(tk)
        reaction = None
        baseline_price_usd = price_usd
        first_seen = today_str
        if prev:
            first_seen = prev.get("first_seen", today_str)
            baseline = prev.get("baseline_price_usd")
            if baseline and price_usd:
                reaction = round((price_usd - baseline) / baseline * 100, 1)
                baseline_price_usd = baseline  # Anker bleibt beim ersten bekannten Kurs

        active_count = sum([insider["active"], macro["active"], predict["active"]])
        why_parts = []
        if insider["active"]:
            why_parts.append("aktuelles SEC-Form-4-Insider-Filing")
        if macro["active"]:
            why_parts.append("erhöhte Nachrichtenaufmerksamkeit")
        if predict["active"]:
            why_parts.append("aktiver Prediction-Market-Bezug")
        why = ("Live-Signal: " + " + ".join(why_parts) + ".") if why_parts else \
              "Insider-Filing vorhanden, aber (noch) keine weiteren Signale."

        results.append({
            "tk": tk,
            "name": info["name"],
            "broker": True,
            "market": "us",
            "score": active_count,
            "reaction": reaction,
            "priceEur": price_eur,
            "description": description,
            "politician": None,
            "track": None,
            "sig": {
                "congress": {"active": False},
                "insider": insider,
                "macro": macro,
                "predict": predict,
            },
            "topic": info["sector"] or "Unternehmensspezifisch",
            "ethics": ethics,
            "why": why,
        })

        new_tickers_state[tk] = {
            "score": active_count,
            "baseline_price_usd": baseline_price_usd,
            "first_seen": first_seen,
        }

    return results, new_tickers_state


def diff_ticker_alerts(old_tickers_state, new_tickers):
    """Vergleicht die aktuelle Ticker-Liste mit der letzten und meldet
    neue Treffer sowie gestiegene Scores fuer Telegram."""
    alerts = []

    for t in new_tickers:
        tk = t["tk"]
        new_score = t.get("score", 0)
        prev = old_tickers_state.get(tk)
        if prev is None:
            alerts.append(f"🆕 <b>{tk}</b> ({t['name']}) neu in der Liste — Score {new_score}/4")
        elif new_score > prev.get("score", 0):
            alerts.append(f"📈 <b>{tk}</b> relevanter geworden: Score {prev.get('score', 0)} → {new_score}")

    return alerts


def main():
    old_state = load_state()
    old_tickers_state = old_state.get("_tickers", {})
    if not isinstance(old_tickers_state, dict):
        # Alter Zustand (Vorversion speicherte eine Liste statt eines Dicts) — zuruecksetzen.
        print("Alter Ticker-Zustand hat unerwartetes Format, wird zurückgesetzt.")
        old_tickers_state = {}

    news_topics = check_topics()
    tickers, new_tickers_state = build_ticker_signals_auto(old_tickers_state)

    ticker_alerts = diff_ticker_alerts(old_tickers_state, tickers)
    if ticker_alerts:
        message = ("🎯 <b>PPB Stockmarket-Signale</b>\nÄnderungen bei beobachteten Tickern:\n\n"
                    + "\n".join(ticker_alerts)
                    + "\n\nDashboard: https://ppbraun.github.io/ppb-stockmarket-signale/")
        try:
            send_telegram(message)
            print("Telegram-Benachrichtigung (Ticker-Änderungen) gesendet.")
        except Exception as e:
            print(f"Telegram-Versand (Ticker-Änderungen) fehlgeschlagen: {e}")
    else:
        print("Keine Ticker-Änderungen gegenüber dem letzten Lauf.")

    # Ticker-Stand fuer den naechsten Vergleich zusaetzlich im State sichern
    state = load_state()
    state["_tickers"] = new_tickers_state
    save_state(state)

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "newsTopics": news_topics,
        "tickers": tickers,
    }
    save_dashboard_data(payload)
    print(f"docs/data.json geschrieben ({len(tickers)} Ticker, {len(news_topics)} Themen).")


if __name__ == "__main__":
    main()
