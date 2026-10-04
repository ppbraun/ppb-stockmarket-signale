#!/usr/bin/env python3
"""IPO-Radar: meldet neue Börsengang-Signale per Telegram.

USA:          SEC-EDGAR (S-1, F-1, 424B4, 8-A12B) mit Firmenprofil,
              Nasdaq-IPO-Kalender (Preisspanne, Termin, Börse, Endpreis)
Deutschland:  Neuemissionen der Deutschen Börse (Zeichnung, Handelsstart, erster Preis),
              Nachrichten-Feeds mit Stichwortfilter (Intention to Float u. a.)

Aufruf eigenständig:   python ipo_watch.py
Aus check_signals.py:  import ipo_watch; ipo_watch.run(notify=eigene_sendefunktion)

Umgebungsvariablen
  SEC_UA              Pflicht, Format "Name kontakt@mail.de" (SEC verlangt das)
  TELEGRAM_BOT_TOKEN  Bot-Token
  TELEGRAM_CHAT_ID    Chat-ID
  IPO_DRY_RUN         "1": nur ausgeben, nichts senden
  IPO_IGNORE_WINDOW   "1": Zeitfenster 7 bis 21 Uhr (Berlin) ignorieren und Statusbericht senden
  IPO_EXCLUDE_REGEX   Namensfilter für EDGAR und Nasdaq (Standard siehe unten)
  IPO_STATE_FILE      Pfad der Zustandsdatei (Standard ipo_state.json)
"""
import copy
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- Einstellungen

STATE_FILE = Path(os.environ.get("IPO_STATE_FILE", "ipo_state.json"))
PREFIX = os.environ.get("IPO_PREFIX", "[IPO]")
KEEP_DAYS = 120               # so lange werden gesehene Einträge gemerkt
MAX_ITEMS_PER_MESSAGE = 6     # mehr Treffer pro Lauf werden auf Nachrichten verteilt
FAIL_ALERT_AFTER = 3          # Warnung nach so vielen Fehlläufen in Folge
WINDOW = (7, 21)              # Berlin, passend zum Signal-Bot

# USA, EDGAR
EDGAR_FORMS = ["S-1", "F-1", "424B4", "8-A12B"]
EDGAR_FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}"
    "&company=&dateb=&owner=include&start=0&count=100&output=atom"
)
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
DOC_HEAD_BYTES = 1_200_000    # so viel vom Prospekt wird für die Firmenbeschreibung gelesen
# Wer eines dieser Formulare schon eingereicht hat, ist nicht neu an der Börse.
PUBLIC_FORMS = {"10-K", "10-KT", "10-Q", "20-F", "40-F", "8-K", "6-K", "4"}
# SPACs, ETFs, Fonds und Trusts reichen ebenfalls S-1 ein und würden sonst fluten.
EXCLUDE_NAME = re.compile(
    os.environ.get("IPO_EXCLUDE_REGEX", r"\b(acquisition|etf|fund|trust)\b"), re.I
)
INITIAL_FILING = re.compile(r"^(S-1|F-1)(/A)?$")

# USA, Nasdaq-IPO-Kalender (inoffizielle Schnittstelle, hinter einer WAF, daher curl_cffi)
NASDAQ_URL = "https://api.nasdaq.com/api/ipo/calendar?date={month}"
NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
PRICED_MAX_AGE_DAYS = 7       # ältere Preisfestsetzungen werden nicht mehr gemeldet
SEC_SEARCH = "https://www.sec.gov/edgar/search/#/q=%22{q}%22&forms=S-1"

# Deutschland
DB_NEWISSUES = "https://live.deutsche-boerse.com/aktien/neuemissionen"
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=de&gl=DE&ceid=DE:de"
DE_QUERIES = [
    '"Intention to Float" Börsengang',
    "Börsengang Frankfurt Erstnotiz",
    "Börsengang Prime Standard Preisspanne",
    "IPO Frankfurter Wertpapierbörse geplant",
]
# Kontrollsuche: liefert immer Treffer und zeigt, ob der Feed überhaupt ankommt.
DE_CANARY = "DAX Börse Frankfurt"
# Eigene Feeds (z. B. EQS, Börse Frankfurt) hier eintragen, der Filter gilt auch für sie.
DE_EXTRA_FEEDS = []
DE_KEYWORDS = re.compile(
    r"börsengang|intention to float|erstnotiz|erstnotierung|\bipo\b|preisspanne"
    r"|zeichnungsfrist|bookbuilding",
    re.I,
)
PRICE_RANGE_DE = re.compile(
    r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:bis|-|–)\s*(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:Euro|EUR|€)",
    re.I,
)

WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ipo-radar)"}


# ---------------------------------------------------------------- Hilfsfunktionen

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_get(url, headers=None, timeout=30):
    r = requests.get(url, headers=headers or WEB_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def fetch_html(url):
    """Erst mit Chrome-Fingerabdruck, sonst normal. Gibt (Text, Weg) zurück."""
    try:
        from curl_cffi import requests as cffi

        r = cffi.get(url, headers=BROWSER_HEADERS, impersonate="chrome", timeout=30)
        if r.status_code == 200 and r.text:
            return r.text, "curl_cffi"
    except Exception as exc:
        print(f"curl_cffi-Abruf fehlgeschlagen: {exc}", file=sys.stderr)
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    return r.text, "requests"


def describe_page(html, via):
    """Kurzbeschreibung einer Seite für das Fehlerprotokoll."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True)[:60] if soup.title else "ohne Titel"
    tables = soup.find_all("table")
    sample = ""
    for t in tables:
        txt = " ".join(t.get_text(" ", strip=True).split())
        if "handelstag" in txt.lower() or "zeichnung" in txt.lower():
            sample = f", Beispieltabelle '{txt[:140]}'"
            break
    return (
        f"Abruf über {via}, {len(html)} Zeichen, Titel '{title}', "
        f"{len(soup.find_all('script'))} Skripte, {len(tables)} Tabellen, "
        f"'Neuemissionen' {'enthalten' if 'neuemissionen' in html.lower() else 'nicht enthalten'}"
        f"{sample}"
    )


def sec_headers():
    ua = os.environ.get("SEC_UA", "").strip()
    if not ua:
        raise RuntimeError('SEC_UA fehlt (Format: "Name kontakt@mail.de")')
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def load_state():
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("Zustandsdatei unlesbar, starte mit leerem Zustand", file=sys.stderr)
            state = {}
    for key in ("seen", "candidates", "init", "fails", "warned", "deals", "db", "errors"):
        state.setdefault(key, {})
    return state


def save_state(state):
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    state["seen"] = {
        k: v for k, v in state["seen"].items()
        if datetime.fromisoformat(v) >= cutoff
    }
    STATE_FILE.write_text(
        json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def send_telegram(text):
    if os.environ.get("IPO_DRY_RUN"):
        print("--- Nachricht (Testlauf, nicht gesendet) ---\n" + text + "\n")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN oder TELEGRAM_CHAT_ID fehlt")
    for i in range(0, len(text), 4000):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat,
                "text": text[i:i + 4000],
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )
        r.raise_for_status()


def batches(header, lines):
    """Verteilt Treffer auf Nachrichten mit höchstens MAX_ITEMS_PER_MESSAGE Zeilen."""
    out = []
    for i in range(0, len(lines), MAX_ITEMS_PER_MESSAGE):
        out.append(header + "\n\n" + "\n\n".join(lines[i:i + MAX_ITEMS_PER_MESSAGE]))
    return out


# Zahlen und Daten in deutscher Schreibweise

def fmt_num(x, digits=2):
    s = f"{x:,.{digits}f}"
    return s.replace(",", "\0").replace(".", ",").replace("\0", ".")


def fmt_big(x):
    if x >= 1e9:
        return f"{fmt_num(x / 1e9, 1)} Mrd."
    if x >= 1e6:
        return f"{fmt_num(x / 1e6, 1)} Mio."
    return fmt_num(x, 0)


def de_date(d):
    return d.strftime("%d.%m.%Y") if d else None


# ---------------------------------------------------------------- Firmenprofil (EDGAR)

SUFFIX_TOKENS = {
    "inc", "corp", "corporation", "ltd", "limited", "plc", "co", "company", "holdings",
    "holding", "llc", "lp", "sa", "ag", "nv", "se", "class", "a", "b", "common", "stock",
    "ordinary", "shares", "ads", "the",
}


def norm_company(name):
    tokens = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    return " ".join(t for t in tokens if t not in SUFFIX_TOKENS)


def find_candidate(state, name):
    n = norm_company(name)
    if len(n) < 4:
        return None
    for c in state["candidates"].values():
        cn = norm_company(c.get("name", ""))
        if cn and (cn == n or (len(cn) >= 6 and (cn in n or n in cn))):
            return c
    return None


NOISE = re.compile(
    r"forward-looking|this summary|summary (does not|is not)|you should|read the entire"
    r"|this prospectus|the following|risk factors|as used in|unless the context"
    r"|table of contents|incorporated by reference|we have not authorized",
    re.I,
)
BUSINESS = re.compile(
    r"^(We|Our company|The company|[A-Z][\w&.,'’\- ]{2,60}) "
    r"(is|are|operates?|develops?|provides?|designs?|builds?|offers?|operate|develop|provide)\b"
)


HEADING = re.compile(
    r"^((Company|Business) Overview|Overview|Our Company|Our Business|About Us|Summary)\s+(?=[A-Z])"
)


def extract_business(text):
    """Sucht im Prospekttext nach dem ersten Satz, der das Geschäft beschreibt."""
    low = text.lower()
    starts = [m.start() for m in re.finditer(r"prospectus summary", low)]
    # der erste Treffer ist meist das Inhaltsverzeichnis, daher zuletzt probieren
    for s in starts[1:] + starts[:1]:
        window = " ".join(text[s + 18:s + 9000].split())
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z])", window):
            # Zwischenüberschriften wie "Overview" oder "Our Company" kleben oft am Satzanfang
            sent = HEADING.sub("", sent.strip())
            if 50 <= len(sent) <= 500 and BUSINESS.match(sent) and not NOISE.search(sent):
                return sent if len(sent) <= 320 else sent[:317].rsplit(" ", 1)[0] + "..."
    return None


def fetch_head(url, headers, limit=DOC_HEAD_BYTES):
    """Liest nur den Anfang eines großen Dokuments."""
    buf = b""
    with requests.get(url, headers=headers, timeout=30, stream=True) as r:
        r.raise_for_status()
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) >= limit:
                break
    return buf.decode("utf-8", errors="ignore")


def html_to_text(html):
    return " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())


def fetch_submissions(cik, headers):
    try:
        time.sleep(0.2)
        return http_get(EDGAR_SUBMISSIONS.format(cik=cik), headers).json()
    except Exception as exc:
        print(f"Abruf CIK {cik} fehlgeschlagen: {exc}", file=sys.stderr)
        return None


def is_new_registrant(data):
    """True: noch nie als Berichtspflichtiger aufgetreten. None: keine Daten."""
    if data is None:
        return None
    forms = set(data.get("filings", {}).get("recent", {}).get("form", []))
    return not (forms & PUBLIC_FORMS)


def build_profile(data, acc, cik, headers):
    """Branche, Sitz und ein beschreibender Satz. Jedes Feld ist optional."""
    prof = {}
    if not data:
        return prof
    if data.get("sicDescription"):
        prof["sic"] = data["sicDescription"]
    addr = (data.get("addresses") or {}).get("business") or {}
    place = addr.get("stateOrCountryDescription") or addr.get("stateOrCountry")
    loc = ", ".join(x for x in (addr.get("city"), place) if x)
    if loc:
        prof["loc"] = loc
    try:
        recent = data.get("filings", {}).get("recent", {})
        accs = recent.get("accessionNumber", [])
        if acc in accs:
            doc = recent.get("primaryDocument", [])[accs.index(acc)]
            url = EDGAR_DOC.format(cik=int(cik), acc=acc.replace("-", ""), doc=doc)
            desc = extract_business(html_to_text(fetch_head(url, headers)))
            if desc:
                prof["desc"] = desc
    except Exception as exc:
        print(f"Firmenbeschreibung CIK {cik} nicht lesbar: {exc}", file=sys.stderr)
    return prof


def profile_lines(c):
    out = []
    if c.get("desc"):
        out.append(c["desc"])
    if c.get("sic"):
        out.append(f"Branche: {c['sic']}")
    if c.get("loc"):
        out.append(f"Sitz: {c['loc']}")
    return out


# ---------------------------------------------------------------- USA: EDGAR

TITLE_RE = re.compile(
    r"^(?P<form>\S+)\s+-\s+(?P<name>.+?)\s+\((?P<cik>\d{10})\)\s+\((?P<role>[^)]*)\)\s*$"
)
ACC_RE = re.compile(r"accession-number=([\d-]+)")


def parse_edgar_entry(entry):
    m = TITLE_RE.match(entry.get("title", "").strip())
    a = ACC_RE.search(entry.get("id", ""))
    if not m or not a:
        return None
    return {
        "acc": a.group(1),
        "form": m.group("form"),
        "name": m.group("name"),
        "cik": m.group("cik"),
        "link": entry.get("link", ""),
    }


def poll_edgar(state):
    headers = sec_headers()
    first = not state["init"].get("edgar")
    entries, problems = [], []

    for form in EDGAR_FORMS:
        try:
            feed = feedparser.parse(
                http_get(EDGAR_FEED.format(form=quote_plus(form)), headers).content
            )
            for e in feed.entries:
                p = parse_edgar_entry(e)
                if p:
                    entries.append(p)
        except Exception as exc:
            problems.append(f"EDGAR {form}: {exc}")
        time.sleep(0.3)

    if len(problems) == len(EDGAR_FORMS):
        return [], problems

    new = []
    for p in entries:
        key = f"edgar:{p['acc']}:{p['cik']}"
        if key in state["seen"]:
            continue
        state["seen"][key] = now_iso()
        new.append(p)

    if first:
        state["init"]["edgar"] = True
        return [
            f"{PREFIX} EDGAR-Überwachung gestartet. {len(new)} aktuelle Einträge "
            f"aus {len(EDGAR_FORMS) - len(problems)} von {len(EDGAR_FORMS)} Feeds "
            "wurden als Ausgangsbasis übernommen, gemeldet wird ab jetzt."
        ], problems

    lines = []
    for p in new:
        if EXCLUDE_NAME.search(p["name"]):
            continue
        form, cik = p["form"].upper(), p["cik"]
        known = cik in state["candidates"]
        data = None

        if INITIAL_FILING.match(form):
            if known:
                continue
            data = fetch_submissions(cik, headers)
            fresh = is_new_registrant(data)
            if fresh is False:
                continue
            cand = {"name": p["name"], "form": p["form"], "ts": now_iso()}
            cand.update(build_profile(data, p["acc"], cik, headers))
            state["candidates"][cik] = cand
            label = "Neuer IPO-Kandidat, Preis und Termin noch offen"
            if fresh is None:
                label += " (Altfiler-Prüfung fehlgeschlagen)"
            extra = profile_lines(cand)
        elif form in ("424B4", "8-A12B"):
            if not known:
                data = fetch_submissions(cik, headers)
                if is_new_registrant(data) is False:
                    continue
            label = (
                "Preis festgesetzt, Final-Prospekt" if form == "424B4"
                else "Börsenlisting angemeldet"
            )
            extra = profile_lines(state["candidates"].get(cik, {}))
        else:
            continue

        parts = [f"{label}: {p['name']} ({p['form']})"] + extra + [p["link"]]
        lines.append("\n".join(parts))

    return batches(f"{PREFIX} USA", lines), problems


# ---------------------------------------------------------------- USA: Nasdaq-Kalender

NULLS = {"", "n/a", "tbd", "none", "-", "--", "na", "n.a."}


def clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in NULLS else s


def to_float(v):
    s = clean(v)
    if s is None:
        return None
    try:
        return float(re.sub(r"[$,\s]", "", s))
    except ValueError:
        return None


def to_date(v):
    s = clean(v)
    if s is None:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def price_range(v):
    s = clean(v)
    if s is None:
        return None, None
    s = re.sub(r"[$,]", "", s)
    m = re.match(r"\s*([\d.]+)\s*[-–]\s*([\d.]+)", s)
    try:
        if m:
            return float(m.group(1)), float(m.group(2))
        x = float(s)
        return x, x
    except ValueError:
        return None, None


def fetch_nasdaq_month(month):
    from curl_cffi import requests as cffi  # erst hier, damit Tests ohne das Paket laufen

    r = cffi.get(
        NASDAQ_URL.format(month=month),
        headers=NASDAQ_HEADERS,
        impersonate="chrome",
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Nasdaq HTTP {r.status_code}")
    return r.json().get("data") or {}


def nasdaq_deals(data):
    """Wandelt die vier Kalenderabschnitte in eine einheitliche Liste."""
    out = []
    buckets = [
        ("upcoming", ((data.get("upcoming") or {}).get("upcomingTable") or {}).get("rows")),
        ("priced", (data.get("priced") or {}).get("rows")),
        ("withdrawn", (data.get("withdrawn") or {}).get("rows")),
    ]
    for status, rows in buckets:
        for row in rows or []:
            name = clean(row.get("companyName"))
            if not name:
                continue
            low, high = price_range(row.get("proposedSharePrice"))
            out.append({
                "status": status,
                "name": name,
                "ticker": clean(row.get("proposedTickerSymbol")),
                "exchange": clean(row.get("proposedExchange")),
                "low": low,
                "high": high,
                "shares": to_float(row.get("sharesOffered")),
                "amount": to_float(row.get("dollarValueOfSharesOffered")),
                "exp": to_date(row.get("expectedPriceDate")),
                "priced_on": to_date(row.get("pricedDate")),
                "withdrawn_on": to_date(row.get("withdrawDate")),
            })
    return out


def deal_text(label, d, state):
    head = f"{label}: {d['name']}"
    tag = ", ".join(x for x in (d.get("ticker"), d.get("exchange")) if x)
    if tag:
        head += f" ({tag})"
    lines = [head]
    if d["status"] == "priced":
        if d["low"]:
            lines.append(f"Ausgabepreis: {fmt_num(d['low'])} US-Dollar")
        if d.get("priced_on"):
            lines.append(f"Preisfestsetzung: {de_date(d['priced_on'])}, Handelsstart in der Regel am Folgetag")
    elif d["status"] == "upcoming":
        if d["low"] and d["high"] and d["low"] != d["high"]:
            lines.append(f"Preisspanne: {fmt_num(d['low'])} bis {fmt_num(d['high'])} US-Dollar")
        elif d["low"]:
            lines.append(f"Preis: {fmt_num(d['low'])} US-Dollar")
        else:
            lines.append("Preisspanne: noch nicht veröffentlicht")
        lines.append(
            "Preisfestsetzung erwartet: " + (de_date(d["exp"]) if d.get("exp") else "noch offen")
        )
    vol = []
    if d.get("shares"):
        vol.append(f"{fmt_big(d['shares'])} Aktien")
    if d.get("amount"):
        vol.append(f"rund {fmt_big(d['amount'])} US-Dollar")
    if vol:
        lines.append("Volumen: " + ", ".join(vol))
    cand = find_candidate(state, d["name"])
    if cand:
        lines += profile_lines(cand)
    else:
        lines.append("Unternehmen: " + SEC_SEARCH.format(q=quote(d["name"])))
    return "\n".join(lines)


def poll_nasdaq(state):
    first = not state["init"].get("nasdaq")
    today = date.today()
    nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    months = [today.strftime("%Y-%m"), nxt.strftime("%Y-%m")]

    deals, problems = {}, []
    for m in months:
        try:
            for d in nasdaq_deals(fetch_nasdaq_month(m)):
                k = "nasdaq:" + norm_company(d["name"])
                # Priced schlägt Upcoming, Upcoming schlägt Withdrawn
                rank = {"priced": 3, "upcoming": 2, "withdrawn": 1}
                if k not in deals or rank[d["status"]] > rank[deals[k]["status"]]:
                    deals[k] = d
        except Exception as exc:
            problems.append(f"Nasdaq {m}: {exc}")
        time.sleep(1.0)
    if len(problems) == len(months):
        return [], problems
    if not deals:
        return [], problems + ["Nasdaq: Antwort ohne einen einzigen Eintrag"]

    n_by = {k: sum(1 for d in deals.values() if d["status"] == k)
            for k in ("upcoming", "priced", "withdrawn")}
    counts = (f"(gelesen: {n_by['upcoming']} offen, {n_by['priced']} bepreist, "
              f"{n_by['withdrawn']} zurückgezogen)")

    known = state["deals"]
    lines, snapshot = [], []

    for k, d in deals.items():
        if EXCLUDE_NAME.search(d["name"]):
            continue
        old = known.get(k)
        sig = {
            "status": d["status"], "low": d["low"], "high": d["high"],
            "exp": de_date(d["exp"]), "price": d["low"] if d["status"] == "priced" else None,
        }
        if first:
            known[k] = sig
            if d["status"] == "upcoming":
                snapshot.append(deal_text("Aktuell im Kalender", d, state))
            continue

        if d["status"] == "upcoming":
            if old is None:
                lines.append(deal_text("Preisspanne und Termin stehen", d, state))
            elif old.get("status") == "upcoming" and (
                old.get("low") != sig["low"] or old.get("high") != sig["high"]
                or old.get("exp") != sig["exp"]
            ):
                was = []
                if old.get("low") != sig["low"] or old.get("high") != sig["high"]:
                    was.append(
                        f"Spanne vorher {fmt_num(old['low'])} bis {fmt_num(old['high'])}"
                        if old.get("low") and old.get("high") else "Spanne vorher offen"
                    )
                if old.get("exp") != sig["exp"]:
                    was.append(f"Termin vorher {old.get('exp') or 'offen'}")
                lines.append(deal_text("Änderung", d, state) + "\n(" + "; ".join(was) + ")")
            known[k] = sig
        elif d["status"] == "priced":
            fresh = d.get("priced_on") is None or (today - d["priced_on"]).days <= PRICED_MAX_AGE_DAYS
            if (old is None or old.get("status") != "priced") and fresh:
                lines.append(deal_text("Preis festgesetzt", d, state))
            known[k] = sig
        elif d["status"] == "withdrawn":
            if old is not None and old.get("status") == "upcoming":
                lines.append(deal_text("Börsengang zurückgezogen", d, state))
            known[k] = sig

    if first:
        state["init"]["nasdaq"] = True
        head = f"{PREFIX} Nasdaq-Kalender überwacht {counts}. Gemeldet wird ab jetzt; aktuell offene Börsengänge:"
        if not snapshot:
            return [f"{PREFIX} Nasdaq-Kalender überwacht {counts}, derzeit ist kein Börsengang mit Termin eingetragen."], problems
        return batches(head, snapshot), problems

    return batches(f"{PREFIX} USA", lines), problems


# ---------------------------------------------------------------- Deutschland

def norm_title(title):
    """Entfernt die Quellenangabe von Google News und alles außer Buchstaben/Ziffern."""
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", title.strip())
    return re.sub(r"[^0-9a-zäöüß]+", "", t.lower())


def parse_db_tables(html):
    """Liest aus den Neuemissionen der Deutschen Börse die Tabellen 'aktuell' und 'bereits notiert'.

    Die Kopfzeile kann aus th- oder td-Zellen bestehen (die Seite nutzt td mit Fettdruck),
    deshalb wird sie über ihren Inhalt gesucht und nicht über das Tag.
    """
    soup = BeautifulSoup(html, "html.parser")
    current, done = [], []
    for table in soup.find_all("table"):
        rows = [
            [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        rows = [r for r in rows if any(r)]
        head_i = None
        for i, r in enumerate(rows):
            low = " ".join(r).lower()
            if "erster handelstag" in low or "zeichnung" in low:
                head_i = i
                break
        if head_i is None:
            continue
        heads = [h.lower() for h in rows[head_i]]
        data = rows[head_i + 1:]

        def idx(word):
            for i, h in enumerate(heads):
                if word in h:
                    return i
            return None

        def cell(r, i):
            return r[i] if i is not None and i < len(r) else ""

        i_name = idx("name") or 0
        if any("zeichnung" in h for h in heads):
            for r in data:
                current.append({
                    "name": cell(r, i_name),
                    "typ": cell(r, idx("typ")),
                    "zeichnung": cell(r, idx("zeichnung")),
                    "handel": cell(r, idx("handelsstart")),
                    "frankfurt": cell(r, idx("frankfurt")),
                })
        elif any("erster handelstag" in h for h in heads):
            for r in data:
                done.append({
                    "name": cell(r, i_name),
                    "tag": cell(r, idx("erster handelstag")),
                    "preis": cell(r, idx("erster preis")),
                })
    return current, done


def poll_db(state):
    first = not state["init"].get("db")
    html, via = fetch_html(DB_NEWISSUES)
    current, done = parse_db_tables(html)
    if not current and not done:
        raise RuntimeError("Keine Tabellen gefunden (" + describe_page(html, via) + ")")
    counts = f"({len(current)} aktuell, {len(done)} bereits notiert gelesen)"

    store = state["db"]
    store.setdefault("current", {})
    store.setdefault("done", [])
    lines = []

    for c in current:
        if not c["name"]:
            continue
        sig = {"zeichnung": c["zeichnung"], "handel": c["handel"]}
        old = store["current"].get(c["name"])
        text = [f"{c['name']} ({c['typ'] or 'Neuemission'})"]
        if c["zeichnung"]:
            text.append(f"Zeichnung (geplant): {c['zeichnung']}")
        if c["handel"]:
            text.append(f"Handelsstart (geplant): {c['handel']}")
        if c["frankfurt"]:
            text.append(f"Über Frankfurt: {c['frankfurt']}")
        text.append("Preisspanne: siehe Wertpapierprospekt, in dieser Liste nicht enthalten")
        if old is None:
            lines.append(("Neu auf der Liste der Deutschen Börse" if not first
                          else "Aktuell auf der Liste") + ": " + "\n".join(text))
        elif old != sig:
            lines.append("Termin geändert: " + "\n".join(text))
        store["current"][c["name"]] = sig

    for d in done:
        key = f"{d['name']}|{d['tag']}"
        if key in store["done"]:
            continue
        store["done"].append(key)
        if not first:
            price = f", erster Preis {d['preis']} Euro" if d["preis"] else ""
            lines.append(f"Erstnotiz erfolgt: {d['name']} am {d['tag']}{price}")

    if first:
        state["init"]["db"] = True
        head = f"{PREFIX} Neuemissionen der Deutschen Börse überwacht {counts}, gemeldet wird ab jetzt."
        if lines:
            return batches(head + " Aktuell:", lines), []
        return [head + " Derzeit steht keine Neuemission auf der Liste."], []
    return batches(f"{PREFIX} Deutschland", lines), []


def poll_de(state):
    first = not state["init"].get("de")
    urls = [GOOGLE_NEWS.format(q=quote_plus(q + " when:3d")) for q in DE_QUERIES]
    urls.append(GOOGLE_NEWS.format(q=quote_plus(DE_CANARY + " when:1d")))
    urls += DE_EXTRA_FEEDS
    found, problems, probe = {}, [], []
    read = 0

    for url in urls:
        try:
            content = http_get(url, BROWSER_HEADERS).content
            probe.append(f"{len(content)} Bytes, Anfang {content[:70]!r}")
            feed = feedparser.parse(content)
            for e in feed.entries:
                read += 1
                title = " ".join(e.get("title", "").split())
                if not title or not DE_KEYWORDS.search(title):
                    continue
                key = "de:" + hashlib.sha1(norm_title(title).encode()).hexdigest()[:16]
                found.setdefault(key, (title, e.get("link", "")))
        except Exception as exc:
            problems.append(f"{url[:70]}: {exc}")
        time.sleep(0.5)

    if len(problems) == len(urls):
        return [], problems
    if read == 0:
        return [], problems + [
            "Nachrichten-Feeds lieferten keinen einzigen Eintrag, auch die Kontrollsuche nicht ("
            + (probe[0] if probe else "keine Antwort") + ")"
        ]

    new = [(k, t, l) for k, (t, l) in found.items() if k not in state["seen"]]
    for k, _, _ in new:
        state["seen"][k] = now_iso()

    if first:
        state["init"]["de"] = True
        return [
            f"{PREFIX} Deutschland-Nachrichten überwacht. {read} Feedeinträge gelesen, "
            f"{len(new)} davon mit Stichwort als Ausgangsbasis übernommen."
        ], problems

    lines = []
    for _, t, l in new:
        m = PRICE_RANGE_DE.search(t)
        hint = f"\nPreisspanne laut Überschrift: {m.group(1)} bis {m.group(2)} Euro" if m else ""
        lines.append(f"{t}{hint}\n{l}")
    return batches(f"{PREFIX} Deutschland, Meldungen", lines), problems


# ---------------------------------------------------------------- Ablauf

def run_source(name, fn, state, notify):
    snapshot = copy.deepcopy(state)
    try:
        messages, problems = fn(state)
        for m in messages:
            notify(m)
    except Exception as exc:
        # Nichts gesendet oder Senden fehlgeschlagen: Zustand zurück, nächster Lauf holt nach
        state.clear()
        state.update(snapshot)
        problems = [f"{type(exc).__name__}: {exc}"]

    fails, warned = state["fails"], state["warned"]
    errors = state.setdefault("errors", {})
    if problems:
        errors[name] = str(problems[0])[:300]
        fails[name] = fails.get(name, 0) + 1
        print(f"[{name}] Probleme ({fails[name]}. in Folge): {problems}", file=sys.stderr)
        if fails[name] >= FAIL_ALERT_AFTER and not warned.get(name):
            try:
                notify(
                    f"{PREFIX} Quelle '{name}' liefert seit {fails[name]} Läufen "
                    f"keine Daten. Letzter Fehler: {problems[0][:200]}"
                )
                warned[name] = True
            except Exception:
                pass
    else:
        if warned.get(name):
            try:
                notify(f"{PREFIX} Quelle '{name}' läuft wieder.")
            except Exception:
                pass
        fails[name] = 0
        warned[name] = False
        errors.pop(name, None)


def status_message(state):
    """Kurzbericht über alle Quellen, aus dem gespeicherten Zustand."""
    deals = state.get("deals", {})
    by = {k: sum(1 for d in deals.values() if d.get("status") == k)
          for k in ("upcoming", "priced", "withdrawn")}
    db = state.get("db", {})
    seen = state.get("seen", {})
    rows = [
        ("edgar", "EDGAR",
         f"{sum(1 for k in seen if k.startswith('edgar:'))} Einträge gemerkt, "
         f"{len(state.get('candidates', {}))} IPO-Kandidaten"),
        ("nasdaq", "Nasdaq-Kalender",
         f"{len(deals)} Börsengänge gemerkt ({by['upcoming']} offen, "
         f"{by['priced']} bepreist, {by['withdrawn']} zurückgezogen)"),
        ("db", "Deutsche Börse",
         f"{len(db.get('current', {}))} aktuell, {len(db.get('done', []))} bereits notiert gemerkt"),
        ("de", "Nachrichten",
         f"{sum(1 for k in seen if k.startswith('de:'))} Meldungen gemerkt"),
    ]
    lines = [f"{PREFIX} Statusbericht (manueller Start)"]
    for name, label, detail in rows:
        n = state["fails"].get(name, 0)
        if not state["init"].get(name):
            flag = "noch nicht gestartet"
        elif n:
            flag = f"gestört seit {n} Läufen"
        else:
            flag = "läuft"
        line = f"{label}: {flag}, {detail}"
        err = state.get("errors", {}).get(name)
        if err:
            line += f"\n   Letzter Fehler: {err}"
        lines.append(line)
    return "\n".join(lines)


SOURCES = (
    ("edgar", poll_edgar),
    ("nasdaq", poll_nasdaq),
    ("db", poll_db),
    ("de", poll_de),
)


def run(notify=None, status=False):
    notify = notify or send_telegram
    state = load_state()
    try:
        for name, fn in SOURCES:
            run_source(name, fn, state, notify)
        if status:
            try:
                notify(status_message(state))
            except Exception as exc:
                print(f"Statusbericht nicht gesendet: {exc}", file=sys.stderr)
    finally:
        save_state(state)


def main():
    if not os.environ.get("IPO_IGNORE_WINDOW"):
        hour = datetime.now(ZoneInfo("Europe/Berlin")).hour
        if not (WINDOW[0] <= hour < WINDOW[1]):
            print("Außerhalb des Zeitfensters, nichts zu tun.")
            return 0
    # Ein manueller Start (Zeitfenster wird ignoriert) schickt zusätzlich einen Statusbericht.
    run(status=bool(os.environ.get("IPO_IGNORE_WINDOW")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
