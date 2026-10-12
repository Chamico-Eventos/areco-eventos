"""
Areco Eventos Scraper
=====================
Runs every 3 days via GitHub Actions.
Scrapes events from:
  1. sanantoniodeareco.com/eventos
  2. areco.ar/category/festejos/ + /category/cultura/

Posts batches to a Google Apps Script web app which writes
to the Areco_Eventos tab of the Eventos DB spreadsheet.
No GCP service account needed — Apps Script handles auth.

Required env var:
  APPS_SCRIPT_URL  — the /exec URL of the deployed web app
"""

import os
import re
import json
import time
import logging
import hashlib
import requests
from datetime import datetime, date
from bs4 import BeautifulSoup

# ── CONFIG ────────────────────────────────────────────────────────────────────

APPS_SCRIPT_URL = os.environ.get(
    "APPS_SCRIPT_URL",
    "https://script.google.com/macros/s/AKfycbyq687XCjn2YbtAjLHvrWE0RZ5u-Tyijhoa_5nWHkJorE62_G1xYLEvEmx3dTA9Zs9k/exec"
)

# How many events to POST in one batch (Apps Script has a 6min execution limit)
BATCH_SIZE = 20

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ArecoEventosBot/1.0; "
        "+https://github.com/datamanIAcs/areco-eventos)"
    )
}

# ── SCHEMA ────────────────────────────────────────────────────────────────────
# Single source of truth. Apps Script mirrors this and auto-adds new columns.
# Add new entries here → they appear in the sheet on next run automatically.

SCHEMA = [
    ("id",                   ""),
    ("fecha",                ""),
    ("hora",                 "Todo el día"),
    ("nombre",               ""),
    ("categoria",            "Evento"),
    ("lugar",                ""),
    ("descripcion",          ""),
    ("precio",               "Consultar"),
    ("es_datamanIAcs",       "FALSE"),
    ("preventa_disponible",  "FALSE"),
    ("link_accion",          ""),
    ("fuente_nombre",        ""),
    ("fuente_url",           ""),
    ("imagen_url",           ""),
    ("activo",               "TRUE"),
    ("fecha_scrape",         ""),        # Date row was first added
    ("tipo_fuente",          "Scrape"),  # "Scrape" = scraper owns | "Manual" = never touch
    ("ultima_actualizacion", ""),        # Date of last scraper update
    # ── ADD NEW COLUMNS BELOW ─────────────────────────────────────────────────
    # ("tags",              ""),
    # ("duracion_hs",       ""),
]

COLUMN_NAMES    = [col for col, _ in SCHEMA]
COLUMN_DEFAULTS = {col: default for col, default in SCHEMA}

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("areco_scraper")


# ── HELPERS ───────────────────────────────────────────────────────────────────

def make_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def fetch(url: str, timeout: int = 10) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning(f"fetch failed {url}: {e}")
        return None


MONTH_MAP = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

def parse_spanish_date(text: str) -> str | None:
    """
    Parses dates from multiple formats.
    Always returns MM/DD/YYYY (Excel-friendly) or None.
    Internally converts everything to that format.
    """
    if not text:
        return None
    text = text.strip().lower()

    # DD/MM/YYYY → MM/DD/YYYY
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        day, month, year = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        return f"{month}/{day}/{year}"

    # ISO YYYY-MM-DD → MM/DD/YYYY
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        year, month, day = m.group(1), m.group(2), m.group(3)
        return f"{month}/{day}/{year}"

    # URL slug /YYYY/MM/DD/ → MM/DD/YYYY
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", text)
    if m:
        year, month, day = m.group(1), m.group(2), m.group(3)
        return f"{month}/{day}/{year}"

    # Spanish long form "19 de septiembre de 2026" → MM/DD/YYYY
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", text)
    if m:
        day   = m.group(1).zfill(2)
        month = MONTH_MAP.get(m.group(2))
        year  = m.group(3)
        if month:
            return f"{str(month).zfill(2)}/{day}/{year}"

    return None


def to_iso(fecha_mmddyyyy: str) -> str | None:
    """Convert MM/DD/YYYY back to ISO for internal date comparisons."""
    if not fecha_mmddyyyy:
        return None
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", fecha_mmddyyyy)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    return None


def parse_time(text: str) -> str:
    m = re.search(r"(\d{1,2})[:\.](\d{2})\s*hs?", text, re.IGNORECASE)
    if m:
        return f"{m.group(1).zfill(2)}:{m.group(2)}"
    return "Todo el día"


CATEGORY_KEYWORDS = {
    "Música":      ["música", "banda", "concierto", "recital", "peña", "folklore",
                    "fogón", "jazz", "rock", "cumbia", "tango", "dj"],
    "Tradición":   ["tradición", "gaucho", "tropilla", "jineteada", "desfile",
                    "crioll", "paisano", "pulpería", "carruaje"],
    "Arte":        ["arte", "pintura", "exposición", "muestra", "escultura",
                    "fotografía", "teatro", "danza"],
    "Deportes":    ["kayak", "carrera", "deportes", "fútbol", "tenis", "natación",
                    "maratón", "ciclismo"],
    "Gastronomía": ["alfajor", "gastronom", "sabor", "vino", "cerveza",
                    "asado", "picnic"],
    "Infantil":    ["niños", "infantil", "chicos", "taller", "colonia"],
    "Cultural":    ["cultura", "libro", "charla", "conferencia", "museo",
                    "escritor", "cine", "carnaval"],
    "Feria":       ["feria", "mercado", "artesanía"],
}

def guess_category(text: str) -> str:
    t = text.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return cat
    return "Evento"


def guess_price(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["gratis", "gratuita", "gratuito", "libre y gratuita"]):
        return "Gratis"

    # Argentine format: $20.000 or $1.500 (dot = thousands separator)
    # Priority 1: full thousands format — $20.000 / $1.500 / $15,000
    m = re.search(r"\$\s?(\d{1,3}(?:[.,]\d{3})+)", text)
    if m:
        return f"${m.group(1)}"

    # Priority 2: bare number — only accept if ≥ 500 (Argentine prices)
    # Values like $10 are almost certainly $10.000 with missing separator
    # Flag them as "Consultar" to avoid showing misleading prices
    m = re.search(r"\$\s?(\d+)", text)
    if m:
        value = int(m.group(1))
        if value >= 500:
            return f"${m.group(1)}"
        # Suspicious low value — could be $10.000 written without separator
        return "Consultar"

    return "Consultar"


VENUES = [
    ("Galpón Chamico",               ["chamico"]),
    ("Predio Colombo",               ["colombo"]),
    ("Plaza Ruiz de Arellano",       ["plaza arellano", "plaza ruiz"]),
    ("Parque Criollo R. Güiraldes",  ["parque criollo", "güiraldes"]),
    ("La Matera",                    ["la matera", "matera"]),
    ("Salón Guerrico",               ["guerrico"]),
    ("Pulpería La Blanqueada",       ["blanqueada"]),
    ("Costanera",                    ["costanera"]),
    ("Centro Cultural Viviana Vigil",["viviana vigil"]),
]

def guess_venue(text: str) -> str:
    t = text.lower()
    for name, keywords in VENUES:
        if any(kw in t for kw in keywords):
            return name
    return ""


def is_past(fecha: str, grace_days: int = 7) -> bool:
    """Works with MM/DD/YYYY or ISO YYYY-MM-DD."""
    try:
        # Try MM/DD/YYYY first (our output format)
        iso = to_iso(fecha) if "/" in str(fecha) else fecha
        if iso:
            return (date.today() - date.fromisoformat(iso)).days > grace_days
    except (ValueError, TypeError):
        pass
    return False


def build_event(event: dict) -> dict:
    """Ensure all schema fields present + inject source tag for router."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = {col: COLUMN_DEFAULTS.get(col, "") for col in COLUMN_NAMES}
    out.update(event)
    today = datetime.now().strftime("%m/%d/%Y")
    # fecha_scrape only set on first insert — Apps Script preserves existing value
    out["fecha_scrape"]          = today
    out["tipo_fuente"]           = "Scrape"
    out["ultima_actualizacion"]  = today
    out["es_datamanIAcs"]      = "FALSE"
    out["preventa_disponible"] = "FALSE"
    out["activo"]              = "TRUE"
    out["source"]              = "areco_scraper"   # ← router key for Apps Script
    return out


# ── SOURCE 1: sanantoniodeareco.com ──────────────────────────────────────────

SAC_BASE = "https://www.sanantoniodeareco.com"

# Pattern: "Eventos y actividades Septiembre 2026" — agenda roundup posts
SAC_AGENDA_RE = re.compile(
    r"eventos\s+y\s+actividades\s+\w+\s+\d{4}", re.IGNORECASE
)

def scrape_sanantoniodeareco() -> list[dict]:
    log.info("Scraping sanantoniodeareco.com/eventos …")
    events = []
    index  = fetch(f"{SAC_BASE}/eventos")
    if not index:
        return events

    links = set()
    for a in index.find_all("a", href=True):
        href = a["href"]
        if "/eventos/" in href and href != f"{SAC_BASE}/eventos":
            if href.startswith("/"):
                href = SAC_BASE + href
            if href.startswith(SAC_BASE):
                links.add(href)

    log.info(f"  Found {len(links)} event links")
    # Cap at 10 pages to keep runtime under control
    for url in sorted(links)[:10]:
        soup = fetch(url)
        if not soup:
            continue
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

        # Agenda roundup post → parse sub-events from body
        if SAC_AGENDA_RE.search(title):
            log.info(f"  Parsing agenda post: {title}")
            sub = parse_sac_agenda(url, soup)
            log.info(f"    → {len(sub)} sub-events extracted")
            events.extend(sub)
        else:
            ev = scrape_sac_event(url, soup, title)
            if ev:
                events.append(ev)

    return events


def parse_sac_agenda(url: str, soup: BeautifulSoup) -> list[dict]:
    """
    Parses a monthly agenda post like "Eventos y actividades Septiembre 2026".

    Real page structure (from sanantoniodeareco.com):
      <li><strong>Viernes 04/09</strong>
        – 21:30 hs – Peña en el Bar El Tokio…
        – 11:30 y 15:00 hs – Visita Guiada…
      </li>
      <li><strong>Sábado 05/09</strong>
        – 11:30 hs – …
      </li>

    Dates are short (DD/MM), year inferred from URL or current year.
    Returns one event dict per sub-event line.
    """
    og_img     = soup.find("meta", property="og:image")
    imagen_url = og_img["content"] if og_img else ""

    # Infer year from URL (e.g. "septiembre-2026" → 2026)
    year_m = re.search(r"(\d{4})", url)
    year   = year_m.group(1) if year_m else str(date.today().year)

    # Infer month from URL slug (e.g. "septiembre-2026")
    month_from_url = None
    for month_name, month_num in MONTH_MAP.items():
        if month_name in url.lower():
            month_from_url = month_num
            break

    events       = []
    current_date = None   # MM/DD/YYYY string

    # Walk every <li> in the article
    article = soup.find("article") or soup.find("main") or soup
    list_items = article.find_all("li")

    # If no <li> found, fall back to splitting text by newlines
    if not list_items:
        raw = article.get_text("\n", strip=True)
        list_items = [type("FakeLi", (), {"get_text": lambda self, sep=" ", strip=True: line})()
                      for line in raw.split("\n") if line.strip()]

    for li in list_items:
        text = li.get_text(" ", strip=True)
        if not text:
            continue

        # ── Check for day header ─────────────────────────────
        # Patterns: "Viernes 04/09"  "Sábado 05/09"  "Domingo 06/09"
        # Also: "Viernes 4 de septiembre" etc.
        day_m = re.match(
            r"(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)"
            r"\s+(\d{1,2})[/\.](\d{1,2})(?:[/\.](\d{2,4}))?",
            text, re.IGNORECASE
        )
        if day_m:
            day   = day_m.group(2).zfill(2)
            month = day_m.group(3).zfill(2)
            yr    = day_m.group(4) or year
            if len(yr) == 2:
                yr = "20" + yr
            current_date = f"{month}/{day}/{yr}"
            # After the day header the same <li> may have sub-events on next lines
            # Process the rest of the text below the header
            rest = text[day_m.end():].strip()
            if rest:
                _extract_subevents(rest, current_date, url, imagen_url, year, events)
            continue

        # Also handle bold day headers: text starts with bold <strong>
        strong = li.find("strong")
        if strong:
            strong_text = strong.get_text(strip=True)
            day_m2 = re.match(
                r"(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)"
                r"\s+(\d{1,2})[/\.](\d{1,2})(?:[/\.](\d{2,4}))?",
                strong_text, re.IGNORECASE
            )
            if day_m2:
                day   = day_m2.group(2).zfill(2)
                month = day_m2.group(3).zfill(2)
                yr    = day_m2.group(4) or year
                if len(yr) == 2:
                    yr = "20" + yr
                current_date = f"{month}/{day}/{yr}"
                rest = li.get_text(" ", strip=True)[len(strong_text):].strip()
                if rest:
                    _extract_subevents(rest, current_date, url, imagen_url, year, events)
                continue

        # ── Check for event line under current day ───────────
        if current_date:
            _extract_subevents(text, current_date, url, imagen_url, year, events)

    return events


def _extract_subevents(text: str, fecha: str, url: str,
                       imagen_url: str, year: str, events: list) -> None:
    """
    Splits a block of text into individual event lines and appends to events.
    Lines look like: "– 21:30 hs – Peña en el Bar El Tokio..."
    or just: "11:30 hs – Visita Guiada..."
    """
    if is_past(fecha):
        return

    # Split on em-dash preceded by time, or on newline
    # Pattern: optional time, dash, event name
    EVENT_PAT = re.compile(
        r"[–\-]\s*"                                    # leading dash
        r"(\d{1,2}[:\.]?\d{0,2}(?:\s*(?:y|a)\s*\d{1,2}[:\.]?\d{0,2})?\s*hs?)?"  # optional time
        r"\s*[–\-]?\s*"                                # optional second dash
        r"([^–\-]{10,})",                              # event name (≥10 chars)
        re.IGNORECASE
    )

    for m in EVENT_PAT.finditer(text):
        hora_raw = (m.group(1) or "").strip()
        nombre   = m.group(2).strip()
        # Clean trailing punctuation and noise
        nombre   = re.sub(r"\s+", " ", nombre).strip(" .,")
        if len(nombre) < 8:
            continue
        # Skip pure address/location lines
        if re.match(r"^(ubicaci[oó]n|direcci[oó]n|punto de encuentro)", nombre, re.I):
            continue

        hora = parse_time(hora_raw + " hs") if hora_raw else "Todo el día"
        uid  = make_id(url + fecha + nombre[:40])

        # Avoid exact duplicates within this agenda
        if any(e["id"] == uid for e in events):
            continue

        events.append({
            "id":           uid,
            "fecha":        fecha,
            "hora":         hora,
            "nombre":       nombre[:120],
            "categoria":    guess_category(nombre),
            "lugar":        guess_venue(nombre),
            "descripcion":  nombre[:200],
            "precio":       guess_price(nombre),
            "link_accion":  url,
            "fuente_nombre":"SanAntoniodeAreco.com",
            "fuente_url":   url,
            "imagen_url":   imagen_url,
        })


def scrape_sac_event(url: str, soup: BeautifulSoup, nombre: str) -> dict | None:
    """Scrapes a single structured event post (not an agenda roundup)."""
    og_img    = soup.find("meta", property="og:image")
    imagen_url = og_img["content"] if og_img else ""

    article   = soup.find("article") or soup.find("main") or soup
    full_text = article.get_text(" ", strip=True)

    fecha = None
    hora  = "Todo el día"

    date_span = soup.find(string=re.compile(
        r"(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)"
        r"\s+\d{1,2}\s+de\s+\w+\s+de\s+\d{4}", re.IGNORECASE
    ))
    if date_span:
        fecha = parse_spanish_date(date_span)
        hora  = parse_time(date_span)

    if not fecha:
        pub = soup.find("meta", property="article:published_time")
        if pub:
            fecha = parse_spanish_date(pub["content"][:10])

    if not fecha:
        fecha = parse_spanish_date(url)

    if fecha and is_past(fecha):
        log.debug(f"  Skipping past: {nombre} ({fecha})")
        return None

    if hora == "Todo el día":
        m = re.search(r"(\d{1,2})[:\.](\d{2})\s*hs?", full_text, re.IGNORECASE)
        if m:
            hora = f"{m.group(1).zfill(2)}:{m.group(2)}"

    og_desc     = soup.find("meta", property="og:description")
    descripcion = og_desc["content"].strip() if og_desc else ""
    if not descripcion:
        for p in article.find_all("p"):
            t = p.get_text(strip=True)
            if len(t) > 40:
                descripcion = t[:200]
                break

    return {
        "id":           make_id(url),
        "fecha":        fecha or "Ver fuente",
        "hora":         hora,
        "nombre":       nombre,
        "categoria":    guess_category(nombre + " " + full_text[:500]),
        "lugar":        guess_venue(full_text),
        "descripcion":  descripcion[:300],
        "precio":       guess_price(full_text),
        "link_accion":  url,
        "fuente_nombre":"SanAntoniodeAreco.com",
        "fuente_url":   url,
        "imagen_url":   imagen_url,
    }


# ── SOURCE 2: areco.ar ───────────────────────────────────────────────────────

ARECO_BASE = "https://areco.ar"

def scrape_areco_ar() -> list[dict]:
    log.info("Scraping areco.ar …")
    seen_urls = set()

    for feed_url in [
        f"{ARECO_BASE}/category/festejos/",
        f"{ARECO_BASE}/category/cultura/",
    ]:
        index = fetch(feed_url)
        if not index:
            continue
        for a in index.find_all("a", href=True):
            href = a["href"]
            if (
                href.startswith(ARECO_BASE)
                and href not in (f"{ARECO_BASE}/", feed_url)
                and "/category/" not in href
                and "/page/"     not in href
                and "/wp-"       not in href
                and href not in seen_urls
            ):
                seen_urls.add(href)

    log.info(f"  Found {len(seen_urls)} post links")
    events = []
    for url in sorted(seen_urls):
        ev = scrape_areco_post(url)
        if ev:
            events.append(ev)
    return events


def scrape_areco_post(url: str) -> dict | None:
    soup = fetch(url)
    if not soup:
        return None

    h2     = soup.find("h2") or soup.find("h1")
    nombre = h2.get_text(strip=True) if h2 else ""
    if not nombre or len(nombre) < 5:
        return None

    # Skip clearly non-event municipal posts
    SKIP_KEYWORDS = [
        "juzgado", "recursos humanos", "secretaría de salud",
        "transparencia", "boletín oficial", "modalidad de turno",
        "ingresos públicos", "factura electrónica", "c.o.t",
        "delegación", "habilitacion", "licitacion", "concurso de precios",
        "acta", "ordenanza", "decreto", "nota de prensa",
    ]
    nombre_lower = nombre.lower()
    if any(kw in nombre_lower for kw in SKIP_KEYWORDS):
        log.debug(f"  Skipping admin post: {nombre}")
        return None

    article   = soup.find("article") or soup.find("main") or soup
    full_text = article.get_text(" ", strip=True)[:1200]

    # Look for an explicit future date IN THE BODY TEXT
    # areco.ar posts: event flyers have the date in the image but sometimes
    # also in the text as "día DD de MES" or "DD/MM/YYYY"
    fecha_from_body = None

    # Try Spanish long form in body
    m = re.search(
        r"(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto"
        r"|septiembre|octubre|noviembre|diciembre)\s+de\s+(\d{4})",
        full_text, re.IGNORECASE
    )
    if m:
        fecha_from_body = parse_spanish_date(m.group(0))

    # Try DD/MM/YYYY in body
    if not fecha_from_body:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", full_text)
        if m:
            fecha_from_body = parse_spanish_date(m.group(0))

    # Fallback: article:published_time (publish date, not event date)
    # Only use this if body had a real date — otherwise skip the post
    # because it's likely an admin notice, not an event
    pub_meta  = soup.find("meta", property="article:published_time")
    pub_fecha = pub_meta["content"][:10] if pub_meta else None
    if pub_fecha:
        # Convert ISO pub date to MM/DD/YYYY for consistency
        pub_fecha = parse_spanish_date(pub_fecha)  # ISO triggers the ISO branch

    # Use body date if found, else publish date only if body looks event-like
    EVENT_SIGNALS = [
        "evento", "actividad", "festival", "show", "concierto", "peña",
        "fiesta", "carnaval", "tradición", "feria", "exposición", "recital",
        "taller", "charla", "inauguración", "vacaciones", "ciclo",
    ]
    body_is_event = any(kw in full_text.lower() for kw in EVENT_SIGNALS)

    if fecha_from_body:
        fecha = fecha_from_body
    elif body_is_event and pub_fecha:
        fecha = pub_fecha
    else:
        # No date + no event signals → skip (admin notice)
        log.debug(f"  Skipping no-date post: {nombre}")
        return None

    # Skip if the date we found is more than 7 days in the past
    if is_past(fecha):
        log.debug(f"  Skipping past: {nombre} ({fecha})")
        return None

    og_desc     = soup.find("meta", property="og:description")
    descripcion = og_desc["content"].strip() if og_desc else nombre

    og_img     = soup.find("meta", property="og:image")
    imagen_url  = og_img["content"] if og_img else ""

    return {
        "id":           make_id(url),
        "fecha":        fecha or "Ver fuente",
        "hora":         parse_time(full_text),
        "nombre":       nombre,
        "categoria":    guess_category(nombre + " " + full_text),
        "lugar":        guess_venue(full_text),
        "descripcion":  descripcion[:300],
        "precio":       guess_price(full_text),
        "link_accion":  url,
        "fuente_nombre":"Municipalidad Areco",
        "fuente_url":   url,
        "imagen_url":   imagen_url,
    }


# ── APPS SCRIPT POSTER ────────────────────────────────────────────────────────

def post_to_apps_script(events: list[dict]) -> dict:
    """
    POSTs a list of events to the Apps Script web app in batches.
    Returns a summary dict: {total, added, skipped, errors}.
    """
    if not events:
        return {"total": 0, "added": 0, "skipped": 0, "errors": 0}

    summary = {"total": len(events), "added": 0, "skipped": 0, "errors": 0}

    # Split into batches to stay within Apps Script 6-min execution limit
    batches = [
        events[i : i + BATCH_SIZE]
        for i in range(0, len(events), BATCH_SIZE)
    ]

    for i, batch in enumerate(batches):
        log.info(f"  Posting batch {i+1}/{len(batches)} ({len(batch)} events) …")
        try:
            r = requests.post(
                APPS_SCRIPT_URL,
                data=json.dumps(batch),          # Apps Script reads postData.contents
                headers={"Content-Type": "application/json"},
                timeout=60,
                allow_redirects=True,            # Apps Script always redirects once
            )
            r.raise_for_status()

            # Apps Script returns JSON: {status, added, skipped, total}
            resp = r.json()
            if resp.get("status") == "ok":
                summary["added"]   += resp.get("added",   0)
                summary["skipped"] += resp.get("skipped", 0)
                log.info(f"    ✓ added={resp.get('added',0)}  skipped={resp.get('skipped',0)}")
            else:
                log.error(f"    Apps Script error: {resp.get('message', resp)}")
                summary["errors"] += len(batch)

        except Exception as exc:
            log.error(f"    POST failed: {exc}")
            summary["errors"] += len(batch)

        # Small pause between batches — be polite to Apps Script quotas
        if i < len(batches) - 1:
            time.sleep(2)

    return summary


# ── SOURCE 3: quehacemos.com.ar ──────────────────────────────────────────────

QH_BASE        = "https://www.quehacemos.com.ar"
QH_SEARCH_URL  = f"{QH_BASE}/?q=San+Antonio+de+Areco"

def scrape_quehacemos() -> list[dict]:
    """
    Scrapes quehacemos.com.ar search results for San Antonio de Areco.
    Capped at 15 event pages to avoid hanging runs.
    """
    log.info("Scraping quehacemos.com.ar …")
    events = []
    index  = fetch(QH_SEARCH_URL, timeout=10)
    if not index:
        log.warning("  quehacemos.com.ar unreachable — skipping")
        return events

    cards = (
        index.find_all("article") or
        index.find_all("div", class_=re.compile(r"event|card|item", re.I))
    )

    if not cards:
        cards = index.find_all("a", href=re.compile(r"/eventos?/|/agenda/", re.I))

    # Hard cap — never fetch more than 15 detail pages per run
    MAX_CARDS = 15
    log.info(f"  Found {len(cards)} candidate cards (cap: {MAX_CARDS})")

    seen = set()
    for card in cards[:MAX_CARDS]:
        a = card.find("a", href=True) if card.name != "a" else card
        if not a:
            continue
        href = a["href"]
        if href.startswith("/"):
            href = QH_BASE + href
        if not href.startswith(QH_BASE):
            continue
        if href in seen:
            continue
        seen.add(href)

        ev = scrape_qh_event(href)
        if ev:
            events.append(ev)

    return events


def scrape_qh_event(url: str) -> dict | None:
    """Scrapes a single quehacemos event detail page."""
    soup = fetch(url)
    if not soup:
        return None

    # Title
    h1     = soup.find("h1") or soup.find("h2")
    nombre = h1.get_text(strip=True) if h1 else ""
    if not nombre or len(nombre) < 5:
        return None

    full_text = (soup.find("main") or soup).get_text(" ", strip=True)

    # Date — quehacemos uses structured meta or visible date spans
    fecha = None
    hora  = "Todo el día"

    # Try og:article:published or event schema
    for meta_name in ["event:start_time", "article:published_time"]:
        m = soup.find("meta", property=meta_name)
        if m and m.get("content"):
            fecha = parse_spanish_date(m["content"][:10])
            if fecha:
                break

    # Try visible date text patterns in the page
    if not fecha:
        m = re.search(
            r"(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)"
            r"\s+\d{1,2}\s+(?:de\s+)?\w+",
            full_text, re.IGNORECASE
        )
        if m:
            fecha = parse_spanish_date(m.group(0))

    # Try DD/MM or DD/MM/YYYY
    if not fecha:
        m = re.search(r"\b(\d{1,2})[/\.](\d{1,2})(?:[/\.](\d{2,4}))?\b", full_text)
        if m:
            year = m.group(3) or str(date.today().year)
            if len(year) == 2:
                year = "20" + year
            fecha = parse_spanish_date(
                f"{m.group(1).zfill(2)}/{m.group(2).zfill(2)}/{year}"
            )

    if not fecha:
        return None

    if is_past(fecha):
        log.debug(f"  Skipping past QH event: {nombre} ({fecha})")
        return None

    # Time
    hora_m = re.search(r"(\d{1,2})[:\.](\d{2})\s*hs?", full_text, re.IGNORECASE)
    if hora_m:
        hora = f"{hora_m.group(1).zfill(2)}:{hora_m.group(2)}"

    og_img     = soup.find("meta", property="og:image")
    imagen_url  = og_img["content"] if og_img else ""

    og_desc     = soup.find("meta", property="og:description")
    descripcion = og_desc["content"].strip() if og_desc else nombre[:200]

    # Only keep if Areco is mentioned
    if "areco" not in full_text.lower() and "areco" not in url.lower():
        return None

    return {
        "id":           make_id(url),
        "fecha":        fecha,
        "hora":         hora,
        "nombre":       nombre[:120],
        "categoria":    guess_category(nombre + " " + full_text[:400]),
        "lugar":        guess_venue(full_text),
        "descripcion":  descripcion[:300],
        "precio":       guess_price(full_text),
        "link_accion":  url,
        "fuente_nombre":"QuéHacemos.com.ar",
        "fuente_url":   url,
        "imagen_url":   imagen_url,
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────

def run():
    import time as _time
    RUN_DEADLINE = _time.time() + 8 * 60   # hard stop after 8 minutes

    log.info("=" * 60)
    log.info(f"Areco Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info(f"Endpoint: {APPS_SCRIPT_URL[:60]}…")
    log.info("=" * 60)

    # 1. Scrape all sources
    raw_events: list[dict] = []

    log.info("--- Source 1: sanantoniodeareco.com ---")
    raw_events.extend(scrape_sanantoniodeareco())
    if _time.time() > RUN_DEADLINE:
        log.warning("Deadline reached after source 1 — skipping remaining sources")
    else:
        log.info("--- Source 2: areco.ar ---")
        raw_events.extend(scrape_areco_ar())

    if _time.time() > RUN_DEADLINE:
        log.warning("Deadline reached after source 2 — skipping quehacemos")
    else:
        log.info("--- Source 3: quehacemos.com.ar ---")
        raw_events.extend(scrape_quehacemos())

    log.info(f"Total events scraped: {len(raw_events)}")

    if not raw_events:
        log.info("Nothing to send. Exiting.")
        return

    # 2. Enrich with defaults + source tag
    events = [build_event(ev) for ev in raw_events]

    # 3. POST to Apps Script — dedup happens server-side
    log.info(f"Sending {len(events)} events to Apps Script …")
    result = post_to_apps_script(events)

    log.info("=" * 60)
    log.info(
        f"Done. total={result['total']}  "
        f"added={result['added']}  "
        f"skipped={result['skipped']}  "
        f"errors={result['errors']}"
    )


if __name__ == "__main__":
    run()
