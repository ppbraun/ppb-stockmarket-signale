"""
PPB Stockmarket-Signale — Signal-Check & Dashboard-Datenversorgung (automatisch)

Macht zwei Dinge in einem Lauf:
1. Prueft das Finnhub-Nachrichtenvolumen pro Thema, vergleicht es mit dem letzten
   Stand und schickt bei Auffaelligkeiten eine Telegram-Nachricht.
2. Ermittelt VOLLAUTOMATISCH relevante Ticker: Schnittmenge aus S&P-500-Mitgliedern
   (offen gepflegte Liste) und allen SEC-Form-4-Filings des letzten Handelstags
   (offizieller SEC-Bulk-Index). Fuer jeden Treffer werden vier Signale live
   ermittelt: Politiker-Trades (House/Senate Stock Watcher), Insider-Kaeufe (SEC
   Form 4), Makro-Aufmerksamkeit (Finnhub-Firmennews) und Prediction Markets
   (Polymarket) — dazu Kurs (Yahoo/Stooq + FX), Kursreaktion seit dem ersten
   Auftauchen und eine Wikipedia-Kurzbeschreibung. Alles wird nach docs/data.json
   geschrieben.

Hinweis zur Quellenwahl: GDELT (vormals fuer den Makro-Puls genutzt) blockt seit
Sommer 2026 automatisierten Zugriff aus Cloud-/CI-Umgebungen zuverlaessig (auch
mit Retries/Browser-Headern nicht loesbar) — deshalb Umstieg auf Finnhub, das
dafuer einen kostenlosen, funktionierenden API-Key-Zugang bietet.

Nutzt nur die Python-Standardbibliothek — kein pip install noetig.
"""

import os
import re
import csv
import io
import json
import time
import base64
import random
import datetime
import urllib.parse
import urllib.request
import urllib.error

FINNHUB_BASE = "https://finnhub.io/api/v1"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
STOOQ_URL = "https://stooq.com/q/l/?s={ticker}.us&f=sd2t2ohlcv&h&e=csv"
FX_URL = "https://api.frankfurter.app/latest?from=USD&to=EUR"
POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
HOUSE_TRADES_URL = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
SENATE_TRADES_URL = "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_SEARCH_URL = "https://oauth.reddit.com/r/mauerstrassenwetten/search"
STATE_PATH = "data/last_state.json"
DASHBOARD_DATA_PATH = "docs/data.json"

# SEC verlangt einen aussagekraeftigen User-Agent mit Kontakt — bei Bedarf anpassen.
SEC_HEADERS = {"User-Agent": "PPB Stockmarket-Signale contact@example.com"}
REDDIT_USER_AGENT = "ppb-stockmarket-signale/1.0 (personal dashboard)"

WATCHLIST_SIZE = 15            # aktiv verfolgte US-Titel (persistent, nicht jeden Tag neu gewürfelt)
ROTATION_PER_RUN = 3           # max. Plätze pro Lauf, die an frische Kandidaten abgegeben werden
MACRO_ARTICLE_THRESHOLD = 3    # Firmennews (Finnhub) in den letzten 2 Tagen, ab der "Makro aktiv" gilt
INSIDER_LOOKBACK_DAYS = 14
CONGRESS_LOOKBACK_DAYS = 60     # grosszuegig, weil PTR-Meldungen bis zu 45 Tage verspaetet ankommen koennen
PREDICT_VOLUME_THRESHOLD = 10000   # USD Handelsvolumen, ab dem ein Polymarket-Treffer zaehlt
BUZZ_LOOKBACK = "week"          # Reddit-Suchfenster fuer r/mauerstrassenwetten

# ---------------------------------------------------------------------------
# Themenfelder fuer den Makro-Puls (News-Panel).
# Statt einer Volltextsuche pro Thema (wie bei GDELT) werden hier lokal die
# Schlagzeilen/Zusammenfassungen der allgemeinen Finnhub-Marktnachrichten nach
# Stichwoertern durchsucht — kostet nur EINE API-Anfrage fuer alle 11 Themen.
# ---------------------------------------------------------------------------
TOPICS = {
    "Zölle / Handelskonflikt": ["tariff", "trade war"],
    "Fed-Zinspolitik": ["federal reserve", "fed rate", "interest rate"],
    "Edelmetalle / Goldminen": ["gold price", "gold mining", "precious metal"],
    "Verteidigung / Rüstung": ["defense contractor", "weapons manufacturer", "defense stock"],
    "KI-Infrastruktur": ["ai data center", "ai infrastructure", "artificial intelligence chip"],
    "Halbleiter / Chips": ["semiconductor", "chipmaker", "chip stock"],
    "Energie / Öl & Gas": ["oil price", "energy market", "crude oil"],
    "Gesundheit / FDA-Zulassungen": ["fda approval", "drug approval"],
    "Schifffahrt / Lieferketten": ["shipping", "supply chain"],
    "Krypto-Regulierung": ["crypto regulation", "cryptocurrency regulation"],
    "Cybersecurity": ["cybersecurity", "cyberattack", "data breach"],
}
THRESHOLD_COUNT = 5           # Erwaehnungen unter den Marktnachrichten, ab der ein Thema "auffaellig" ist
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
# Finnhub — Makro-Nachrichten (Themen-Puls + pro Ticker)
# ---------------------------------------------------------------------------

def fetch_finnhub_general_news(api_key):
    if not api_key:
        return []
    url = f"{FINNHUB_BASE}/news?category=general&token={api_key}"
    try:
        data = http_get_json(url, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Finnhub-Marktnachrichten konnten nicht geladen werden: {e}")
        return []


def count_topic_mentions(articles, keywords):
    count = 0
    newest_ts = 0
    for a in articles:
        text = f"{a.get('headline', '')} {a.get('summary', '')}".lower()
        if any(kw in text for kw in keywords):
            count += 1
            newest_ts = max(newest_ts, a.get("datetime", 0) or 0)
    return count, newest_ts


def fetch_finnhub_company_news(api_key, ticker, days=2):
    if not api_key:
        return []
    today = datetime.date.today()
    frm = (today - datetime.timedelta(days=days)).isoformat()
    to = today.isoformat()
    url = f"{FINNHUB_BASE}/company-news?symbol={ticker}&from={frm}&to={to}&token={api_key}"
    try:
        data = http_get_json(url, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  Finnhub-Firmennews fehlgeschlagen: {e}")
        return []


def age_label_from_timestamp(ts):
    if not ts:
        return "48Std"
    age_hours = max(round((time.time() - ts) / 3600), 0)
    return f"{age_hours}Std" if age_hours < 24 else f"{age_hours // 24}T"


# ---------------------------------------------------------------------------
# Kursdaten (Yahoo/Stooq) + Wechselkurs (Frankfurter/EZB)
# ---------------------------------------------------------------------------

def fetch_usd_eur_rate():
    try:
        data = http_get_json(FX_URL, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
        return data.get("rates", {}).get("EUR")
    except Exception as e:
        print(f"Wechselkurs konnte nicht geladen werden: {e}")
        return None


def fetch_price_usd(ticker):
    # Primär: Yahoo Finance (öffentlicher Chart-Endpunkt, kein Key, in der Praxis zuverlässiger als Stooq)
    yahoo_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}"
    try:
        data = http_get_json(yahoo_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ppb-stockmarket-signale/1.0)"
        })
        price = data.get("chart", {}).get("result", [{}])[0].get("meta", {}).get("regularMarketPrice")
        if price:
            return float(price)
    except Exception as e:
        print(f"  Yahoo-Kursabfrage fehlgeschlagen ({e}), versuche Stooq ...")

    # Fallback: Stooq
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
        print(f"  Stooq-Kursabfrage ebenfalls fehlgeschlagen: {e}")
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
# Politiker-Trades (House Stock Watcher + Senate Stock Watcher)
# Beides offen gepflegte Datensaetze, die die offiziellen STOCK-Act-
# Offenlegungen (House Clerk / Senate eFD) aufbereiten. Wir scrapen damit
# nicht selbst — wir konsumieren fertige, oeffentliche JSON-Dateien.
# Hinweis: House blockt seit Sommer 2026 haeufig automatisierten Zugriff
# (403) — Senate laeuft zuverlaessig, dadurch bleibt Politik nicht komplett
# leer, auch wenn House gerade ausfaellt.
# ---------------------------------------------------------------------------

def parse_us_date(date_str):
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def load_congress_trades():
    """Laedt House- und Senate-Trades und indiziert sie nach Ticker."""
    trades_by_ticker = {}

    def add_trade(ticker, chamber, member, tx_type, tx_date_str, disclosure_date_str):
        if not ticker or ticker in ("--", "", "N/A"):
            return
        trades_by_ticker.setdefault(ticker.upper().strip(), []).append({
            "chamber": chamber,
            "member": member,
            "type": (tx_type or ""),
            "tx_date": tx_date_str,
            "disclosure_date": disclosure_date_str,
        })

    try:
        house_data = None
        last_house_error = None
        for attempt in range(2):
            try:
                house_data = http_get_json(HOUSE_TRADES_URL, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; ppb-stockmarket-signale/1.0)",
                    "Accept": "application/json",
                })
                break
            except Exception as e:
                last_house_error = e
                print(f"  House-Trades Versuch {attempt + 1} fehlgeschlagen ({e}), warte 5s ...")
                time.sleep(5)
        if house_data is None:
            raise last_house_error

        for t in house_data:
            add_trade(
                t.get("ticker"), "House", t.get("representative"),
                t.get("type"), t.get("transaction_date"), t.get("disclosure_date"),
            )
        print(f"House-Trades geladen: {len(house_data)} Einträge.")
    except Exception as e:
        print(f"House-Trades konnten nicht geladen werden: {e}")

    try:
        senate_data = http_get_json(SENATE_TRADES_URL, headers={"User-Agent": "ppb-stockmarket-signale/1.0"})
        for t in senate_data:
            member = t.get("senator") or f"{t.get('first_name', '')} {t.get('last_name', '')}".strip()
            add_trade(
                t.get("ticker"), "Senate", member,
                t.get("type"), t.get("transaction_date"), t.get("date_recieved"),
            )
        print(f"Senate-Trades geladen: {len(senate_data)} Einträge.")
    except Exception as e:
        print(f"Senate-Trades konnten nicht geladen werden: {e}")

    return trades_by_ticker


def most_recent_congress_trade(trades_by_ticker, ticker):
    entries = trades_by_ticker.get(ticker.upper())
    if not entries:
        return None

    cutoff = datetime.date.today() - datetime.timedelta(days=CONGRESS_LOOKBACK_DAYS)
    best, best_date = None, None
    for e in entries:
        d = parse_us_date(e.get("disclosure_date")) or parse_us_date(e.get("tx_date"))
        if not d or d < cutoff:
            continue
        if best_date is None or d > best_date:
            best_date, best = d, e

    if not best:
        return None

    age_days = max((datetime.date.today() - best_date).days, 0)
    tx_type = best["type"].lower()
    if "purchase" in tx_type or "buy" in tx_type:
        direction = "Kauf"
    elif "sale" in tx_type or "sell" in tx_type:
        direction = "Verkauf"
    else:
        direction = None

    return {
        "direction": direction,
        "age_hours": age_days * 24,
        "member": best["member"],
        "chamber": best["chamber"],
    }


# ---------------------------------------------------------------------------
# Reddit-Sentiment (r/mauerstrassenwetten) — deutsches Retail-Buzz-Signal
# Hinweis: Reddit hat seinen kostenlosen API-Zugang seit Ende Mai 2026
# faktisch eingestellt. Bleibt hier defensiv im Code (falls sich das aendert
# oder Secrets gesetzt werden), degradiert aber sauber ohne Absturz.
# ---------------------------------------------------------------------------

def fetch_reddit_token():
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Kein REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET gesetzt — Buzz-Signal wird übersprungen.")
        return None

    credentials = f"{client_id}:{client_secret}".encode("utf-8")
    auth_header = "Basic " + base64.b64encode(credentials).decode("ascii")
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(REDDIT_TOKEN_URL, data=data, headers={
        "Authorization": auth_header,
        "User-Agent": REDDIT_USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload.get("access_token")
    except Exception as e:
        print(f"Reddit-Token konnte nicht geholt werden: {e}")
        return None


def fetch_reddit_buzz(token, ticker):
    if not token:
        return {"active": False}

    params = {"q": ticker, "restrict_sr": "1", "sort": "new", "t": BUZZ_LOOKBACK, "limit": "25"}
    url = REDDIT_SEARCH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": REDDIT_USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  Reddit-Suche fehlgeschlagen: {e}")
        return {"active": False}

    posts = data.get("data", {}).get("children", [])
    if not posts:
        return {"active": False}

    newest_ts = max((p.get("data", {}).get("created_utc", 0) for p in posts), default=0)
    if not newest_ts:
        return {"active": False}

    return {
        "active": True,
        "age": age_label_from_timestamp(newest_ts),
        "count": len(posts),
    }


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
    """Ermittelt Kauf/Verkauf aus dem Form-4-Dokument.
    Nutzt eine tolerante Text-/Regex-Suche statt striktem XML-Parsing, weil
    reale SEC-Filings gelegentlich minimal fehlerhaftes XML enthalten
    (z. B. unescapte Zeichen in Freitext-Fussnoten), an dem ein strenger
    Parser (ElementTree) zuverlässig scheitert."""
    accession_no_dashes = accession.replace("-", "")
    cik_numeric = str(int(cik10))
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_numeric}/{accession_no_dashes}/{primary_doc}"
    try:
        xml_text = http_get_text(url, headers=SEC_HEADERS)
    except Exception as e:
        print(f"  (Form-4-Dokument nicht abrufbar: {e})")
        return None

    codes = re.findall(r"<transactionCode>\s*([A-Z])\s*</transactionCode>", xml_text)
    for code in codes:
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
# Themen-Puls (Telegram-Alarm + News-Panel)
# ---------------------------------------------------------------------------

def check_topics(finnhub_key):
    old_state = load_state()
    new_state = {}
    alerts = []
    news_topics = []

    articles = fetch_finnhub_general_news(finnhub_key)
    print(f"Finnhub-Marktnachrichten geladen: {len(articles)} Artikel.")

    for topic, keywords in TOPICS.items():
        count, _ = count_topic_mentions(articles, keywords)
        old_count = old_state.get(topic, 0)
        new_state[topic] = count
        news_topics.append({"topic": topic, "vol": count})

        crossed = count >= THRESHOLD_COUNT and old_count < THRESHOLD_COUNT
        jumped = old_count > 0 and ((count - old_count) / old_count * 100) >= THRESHOLD_JUMP_PCT
        if crossed or jumped:
            alerts.append(f"🟢 <b>{topic}</b>: {count} Erwähnungen (zuletzt {old_count})")

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

CHINA_ADR_WATCHLIST = [
    {"tk": "BABA", "name": "Alibaba Group Holding", "sector": "Consumer Discretionary"},
    {"tk": "JD", "name": "JD.com", "sector": "Consumer Discretionary"},
    {"tk": "PDD", "name": "PDD Holdings (Pinduoduo)", "sector": "Consumer Discretionary"},
    {"tk": "BIDU", "name": "Baidu", "sector": "Communication Services"},
    {"tk": "NIO", "name": "NIO Inc.", "sector": "Consumer Discretionary"},
    {"tk": "LI", "name": "Li Auto", "sector": "Consumer Discretionary"},
    {"tk": "TCOM", "name": "Trip.com Group", "sector": "Consumer Discretionary"},
    {"tk": "NTES", "name": "NetEase", "sector": "Communication Services"},
]


def load_sec_ticker_cik_map():
    """Offizielle SEC-Zuordnung Ticker -> CIK (fuer Firmen ausserhalb der S&P-500-Liste)."""
    data = http_get_json(SEC_TICKERS_URL, headers=SEC_HEADERS)
    mapping = {}
    for entry in data.values():
        mapping[entry["ticker"].upper()] = str(entry["cik_str"]).zfill(10)
    return mapping


def build_signal_for_ticker(tk, name, sector, cik10, market, finnhub_key, fx_rate,
                             congress_trades, reddit_token, old_tickers_state, today_str):
    """Ermittelt alle fuenf Signale + Kurs/Beschreibung fuer EINEN Ticker.
    Gemeinsam genutzt von der automatischen S&P-500-Auswahl und der festen
    China-ADR-Watchlist, damit die Logik nicht doppelt gepflegt werden muss."""
    print(f"Pruefe {tk} ({name}) ...")

    insider = {"active": False}
    ethics = []
    description = None
    if cik10:
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
    else:
        print("  Keine CIK bekannt — Insider-Check übersprungen.")

    time.sleep(1)

    try:
        description = fetch_company_description(name)
    except Exception as e:
        print(f"  Firmenbeschreibung fehlgeschlagen: {e}")

    time.sleep(1)

    macro = {"active": False}
    try:
        company_articles = fetch_finnhub_company_news(finnhub_key, tk)
        count = len(company_articles)
        if count >= MACRO_ARTICLE_THRESHOLD:
            newest_ts = max((a.get("datetime", 0) or 0 for a in company_articles), default=0)
            macro = {"active": True, "age": age_label_from_timestamp(newest_ts)}
    except Exception as e:
        print(f"  Makro-Check fehlgeschlagen: {e}")

    time.sleep(1)

    predict = {"active": False}
    try:
        predict = fetch_polymarket_signal(name)
    except Exception as e:
        print(f"  Predict-Check fehlgeschlagen: {e}")

    time.sleep(1)

    congress = {"active": False}
    politician = None
    try:
        match = most_recent_congress_trade(congress_trades, tk)
        if match:
            congress = {
                "active": True,
                "age": f"{match['age_hours']}Std" if match["age_hours"] < 24
                       else f"{match['age_hours'] // 24}T",
                "src": match["chamber"],
            }
            if match["direction"]:
                congress["dir"] = match["direction"]
            politician = f"{match['member']} ({match['chamber']})" if match["member"] else match["chamber"]
    except Exception as e:
        print(f"  Politiker-Check fehlgeschlagen: {e}")

    time.sleep(1)

    buzz = {"active": False}
    try:
        buzz = fetch_reddit_buzz(reddit_token, tk)
    except Exception as e:
        print(f"  Buzz-Check fehlgeschlagen: {e}")

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

    active_count = sum([congress["active"], insider["active"], macro["active"], predict["active"], buzz["active"]])
    why_parts = []
    if congress["active"]:
        why_parts.append("aktueller Politiker-Trade (House/Senate)")
    if insider["active"]:
        why_parts.append("aktuelles SEC-Form-4-Insider-Filing")
    if macro["active"]:
        why_parts.append("erhöhte Nachrichtenaufmerksamkeit")
    if predict["active"]:
        why_parts.append("aktiver Prediction-Market-Bezug")
    if buzz["active"]:
        why_parts.append(f"Diskussion in r/mauerstrassenwetten ({buzz.get('count', 0)} Beiträge)")
    if why_parts:
        why = "Live-Signal: " + " + ".join(why_parts) + "."
    elif market == "cn" and not cik10:
        why = "Als Foreign Private Issuer meist von SEC-Insider-Meldepflicht befreit — aktuell keine weiteren Signale."
    else:
        why = "Insider-Filing vorhanden, aber (noch) keine weiteren Signale."

    result = {
        "tk": tk,
        "name": name,
        "broker": True,
        "market": market,
        "score": active_count,
        "reaction": reaction,
        "priceEur": price_eur,
        "description": description,
        "politician": politician,
        "track": None,
        "sig": {
            "congress": congress,
            "insider": insider,
            "macro": macro,
            "predict": predict,
            "buzz": buzz,
        },
        "topic": sector or "Unternehmensspezifisch",
        "ethics": ethics,
        "why": why,
    }
    state_entry = {
        "score": active_count,
        "baseline_price_usd": baseline_price_usd,
        "first_seen": first_seen,
    }
    return result, state_entry


def build_ticker_signals_auto(old_tickers_state, finnhub_key):
    print("Lade S&P-500-Liste ...")
    try:
        sp500 = load_sp500()
    except Exception as e:
        print(f"S&P-500-Liste konnte nicht geladen werden: {e}")
        return [], {}

    sp500_by_tk = {info["tk"]: info for info in sp500.values()}
    sp500_cik_by_tk = {info["tk"]: cik for cik, info in sp500.items()}

    form4_ciks, filing_date = fetch_recent_form4_ciks()
    if not form4_ciks:
        return [], {}

    all_candidates = [(cik, info) for cik, info in sp500.items() if cik in form4_ciks]
    random.shuffle(all_candidates)

    # --- Persistente Watchlist statt taeglicher Zufallsstichprobe ---
    # Bereits verfolgte Titel bleiben auf der Liste und werden JEDEN Lauf neu
    # geprueft (damit Score-Entwicklungen ueberhaupt sichtbar werden). Nur wenn
    # Plaetze frei sind oder die schwaechsten Eintraege rotiert werden, kommen
    # neue Kandidaten aus dem heutigen Form-4-Pool dazu.
    china_tks = {e["tk"] for e in CHINA_ADR_WATCHLIST}
    watchlist_tks = [tk for tk in old_tickers_state.keys() if tk not in china_tks and tk in sp500_by_tk]
    fresh_candidate_tks = [info["tk"] for cik, info in all_candidates if info["tk"] not in watchlist_tks]

    if len(watchlist_tks) < WATCHLIST_SIZE:
        need = WATCHLIST_SIZE - len(watchlist_tks)
        added = fresh_candidate_tks[:need]
        watchlist_tks.extend(added)
        print(f"Watchlist wird aufgefüllt: +{len(added)} neue Titel ({len(watchlist_tks)}/{WATCHLIST_SIZE}).")
    elif fresh_candidate_tks:
        scored = sorted(watchlist_tks, key=lambda tk: old_tickers_state.get(tk, {}).get("score", 0))
        n_replace = min(ROTATION_PER_RUN, len(fresh_candidate_tks))
        to_evict = set(scored[:n_replace])
        added = fresh_candidate_tks[:n_replace]
        watchlist_tks = [tk for tk in watchlist_tks if tk not in to_evict] + added
        print(f"Watchlist-Rotation: {sorted(to_evict)} raus, {added} rein (Score-schwächste ersetzt).")
    else:
        print("Watchlist voll, keine frischen Kandidaten heute — unverändert weiterverfolgt.")

    print(f"Aktiv verfolgte US-Watchlist ({len(watchlist_tks)}): {', '.join(sorted(watchlist_tks))}")

    print("Lade USD/EUR-Wechselkurs ...")
    fx_rate = fetch_usd_eur_rate()
    if fx_rate:
        print(f"Wechselkurs USD->EUR: {fx_rate}")
    else:
        print("Kein Wechselkurs verfügbar — Kurs (€) bleibt leer.")

    print("Lade Politiker-Trades (House + Senate) ...")
    congress_trades = load_congress_trades()

    print("Hole Reddit-Zugriffstoken ...")
    reddit_token = fetch_reddit_token()

    results = []
    new_tickers_state = {}
    today_str = datetime.date.today().isoformat()

    for tk in watchlist_tks:
        info = sp500_by_tk.get(tk)
        if not info:
            continue
        cik = sp500_cik_by_tk.get(tk)
        cik10 = str(cik).zfill(10) if cik else None
        result, state_entry = build_signal_for_ticker(
            tk, info["name"], info["sector"], cik10, "us",
            finnhub_key, fx_rate, congress_trades, reddit_token, old_tickers_state, today_str,
        )
        results.append(result)
        new_tickers_state[tk] = state_entry

    # --- Feste China-ADR-Watchlist zusaetzlich pruefen ---
    # S&P 500 enthaelt keine chinesischen Firmen, und Auslaendische Emittenten
    # (Foreign Private Issuers) sind meist von der Form-4-Meldepflicht befreit
    # — automatische Entdeckung wie bei den US-Titeln funktioniert hier nicht,
    # deshalb eine kleine, feste Liste bekannter, liquider China-ADRs.
    print("Prüfe feste China-ADR-Watchlist ...")
    try:
        sec_cik_map = load_sec_ticker_cik_map()
    except Exception as e:
        print(f"SEC-Ticker-Zuordnung für China-ADRs konnte nicht geladen werden: {e}")
        sec_cik_map = {}

    for entry in CHINA_ADR_WATCHLIST:
        cik10 = sec_cik_map.get(entry["tk"])
        result, state_entry = build_signal_for_ticker(
            entry["tk"], entry["name"], entry["sector"], cik10, "cn",
            finnhub_key, fx_rate, congress_trades, reddit_token, old_tickers_state, today_str,
        )
        results.append(result)
        new_tickers_state[entry["tk"]] = state_entry

    return results, new_tickers_state


def net_direction_label(ticker_result):
    """Fasst beobachtete Kauf-/Verkauf-Richtung aus Politik + Insider zusammen
    (dieselbe Logik wie im Dashboard, JS-Pendant: netDirection/directionHtml).
    Reine Zusammenfassung bereits gemeldeter Transaktionen — keine Prognose."""
    sig = ticker_result.get("sig", {})
    buy = 0
    sell = 0
    for key in ("congress", "insider"):
        s = sig.get(key) or {}
        if s.get("active") and s.get("dir") == "Kauf":
            buy += 1
        if s.get("active") and s.get("dir") == "Verkauf":
            sell += 1

    if buy > 0 and sell == 0:
        return f"▲ {buy}x Kauf" if buy > 1 else "▲ Kauf"
    if sell > 0 and buy == 0:
        return f"▼ {sell}x Verkauf" if sell > 1 else "▼ Verkauf"
    if buy > 0 and sell > 0:
        return "⚡ Gemischt"
    return None


def diff_ticker_alerts(old_tickers_state, new_tickers):
    """Vergleicht die aktuelle Ticker-Liste mit der letzten und meldet
    neue Treffer, gestiegene/gesunkene Scores sowie Watchlist-Abgänge
    fuer Telegram."""
    alerts = []
    seen_tks = set()

    for t in new_tickers:
        tk = t["tk"]
        seen_tks.add(tk)
        new_score = t.get("score", 0)
        prev = old_tickers_state.get(tk)
        direction = net_direction_label(t)
        dir_suffix = f" · {direction}" if direction else ""

        if prev is None:
            alerts.append(f"🆕 <b>{tk}</b> ({t['name']}) neu in der Liste — Score {new_score}/5{dir_suffix}")
        elif new_score > prev.get("score", 0):
            alerts.append(f"📈 <b>{tk}</b> relevanter geworden: Score {prev.get('score', 0)} → {new_score}{dir_suffix}")
        elif new_score < prev.get("score", 0):
            alerts.append(f"📉 <b>{tk}</b> weniger relevant: Score {prev.get('score', 0)} → {new_score}")

    # Titel, die vorher verfolgt wurden, jetzt aber aus der Watchlist rotiert sind
    for tk in old_tickers_state:
        if tk not in seen_tks:
            alerts.append(f"➖ <b>{tk}</b> aus der Watchlist rotiert (Score war zu niedrig)")

    return alerts


def main():
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    if not finnhub_key:
        print("Kein FINNHUB_API_KEY gesetzt — Makro-Signale (Themen + pro Ticker) bleiben leer.")

    old_state = load_state()
    old_tickers_state = old_state.get("_tickers", {})
    if not isinstance(old_tickers_state, dict):
        # Alter Zustand (Vorversion speicherte eine Liste statt eines Dicts) — zuruecksetzen.
        print("Alter Ticker-Zustand hat unerwartetes Format, wird zurückgesetzt.")
        old_tickers_state = {}

    news_topics = check_topics(finnhub_key)
    tickers, new_tickers_state = build_ticker_signals_auto(old_tickers_state, finnhub_key)

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
