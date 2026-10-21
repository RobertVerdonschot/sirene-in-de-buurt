"""Sirene in de buurt: kleine Flask-app die laat zien welke spoedmeldingen van
brandweer, ambulance en politie er het afgelopen uur in de buurt waren.

Databronnen (gratis, geen key):
- meldingen: RSS-feeds van 112-nu.nl, per gemeente en per dienst (30 meest
  recente items per feed; dat is ook in grote steden ruim meer dan een uur)
- gemeenten in de buurt + plek van een straat: PDOK Locatieserver (overheid)
112-nu.nl stuurt geen CORS-headers, daarom loopt alles via deze server.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, abort, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

# Zoekstraal rond de gebruiker en hoe ver terug we kijken.
RADIUS_M = 3000
WINDOW_S = 3600
MAX_RESULTS = 40

FEED_URL = "https://112-nu.nl/{service}/gemeente-{slug}/rss/rss"
SERVICES = {"ambulance": "AMB", "brandweer": "BRA", "politie": "POL"}
PDOK = "https://api.pdok.nl/bzk/locatieserver/search/v3_1"
HTTP_TIMEOUT = 8
USER_AGENT = "sirene-in-de-buurt/1.0 (+https://github.com/RobertVerdonschot/sirene-in-de-buurt)"

# 112-nu.nl zet Nederlandse tijd in de feed maar plakt er "Z" (UTC) achter.
AMSTERDAM = ZoneInfo("Europe/Amsterdam")

# CBS-gemeentecode -> slug die 112-nu.nl in de feed-URL gebruikt. Eenmalig
# vastgesteld door elke gemeente bij 112-nu.nl op te vragen (zie README).
GEMEENTE_SLUGS: dict[str, str] = json.loads((BASE_DIR / "gemeenten.json").read_text(encoding="utf-8"))

app = Flask(__name__, static_folder=None)
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_pool = ThreadPoolExecutor(max_workers=8)


class _TTLCache:
    def __init__(self) -> None:
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key, max_age: float | None = None):
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            expires, stored, value = hit
            if expires > time.time() or (max_age is not None and time.time() - stored < max_age):
                return value
            return None

    def set(self, key, value, ttl: float) -> None:
        with self._lock:
            if len(self._data) > 5000:
                self._data.clear()
            now = time.time()
            self._data[key] = (now + ttl, now, value)


_cache = _TTLCache()


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


_COMPASS = ["noord", "noordoost", "oost", "zuidoost", "zuid", "zuidwest", "west", "noordwest"]


def _compass(deg: float) -> str:
    return _COMPASS[int((deg + 22.5) // 45) % 8]


def _point(wkt: str | None) -> tuple[float, float] | None:
    m = re.match(r"POINT\(([-\d.]+) ([-\d.]+)\)", wkt or "")
    return (float(m.group(2)), float(m.group(1))) if m else None


# ---------------------------------------------------------------- PDOK

def _pdok(path: str, params: dict) -> list[dict]:
    resp = _session.get(f"{PDOK}/{path}", params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["response"]["docs"]


def _nearby(lat: float, lon: float) -> dict:
    """Gemeenten en woonplaatsen waarvan de grens binnen RADIUS_M ligt."""
    key = ("near", round(lat, 3), round(lon, 3))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    common = {"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "distance": RADIUS_M, "rows": 100}
    gems = _pdok("reverse", {**common, "type": "gemeente", "fl": "gemeentecode,gemeentenaam"})
    wps = _pdok("reverse", {**common, "type": "woonplaats", "fl": "gemeentecode,woonplaatsnaam"})
    result = {
        "gemeenten": {d["gemeentecode"]: d["gemeentenaam"] for d in gems},
        "woonplaatsen": sorted({f'{d["gemeentecode"]}:{_slug(d["woonplaatsnaam"])}' for d in wps}),
    }
    _cache.set(key, result, ttl=6 * 3600)
    return result


_ROAD = re.compile(r"[ans]\d{1,3}")

# Straatnamen worden verschillend afgekort ("Dr. Molewaterplein" bij PDOK,
# "dokter-molewaterplein" bij 112-nu). Vergelijk daarom de voluit-vorm.
_ABBREV = {"dr": "dokter", "doctor": "dokter", "st": "sint", "burg": "burgemeester", "mr": "meester",
           "prof": "professor", "pr": "prins", "kon": "koning", "gen": "generaal", "ds": "dominee",
           "v": "van", "jhr": "jonkheer", "mgr": "monseigneur", "past": "pastoor"}


def _street_key(slug: str) -> str:
    return "-".join(_ABBREV.get(w, w) for w in slug.split("-"))


def _geocode(gemeentecode: str, place: str, street: str | None, pc4: str | None) -> dict | None:
    """Zoek de plek van een melding op. Liever niks dan een gok:
    alleen een treffer als straat (en plaats) echt overeenkomen."""
    key = ("geo", gemeentecode, place, street, pc4)
    cached = _cache.get(key)
    if cached is not None:
        return cached or None
    result: dict = {}
    if street and _ROAD.fullmatch(street):
        # Snelweg of N-weg: het midden van de hele weg zegt niks over de plek.
        result = {"street": street.upper(), "place": None, "precision": "road"}
    elif street:
        q = " ".join(x for x in (street.replace("-", " "), pc4, place.replace("-", " ")) if x)
        docs = _pdok("free", {"q": q, "fq": [f"gemeentecode:{gemeentecode}", "type:(weg OR postcode)"],
                              "rows": 10, "fl": "type,straatnaam,woonplaatsnaam,centroide_ll"})
        pattern = re.compile(re.escape(_street_key(street)) + r"(-\d+)?")
        hits = [d for d in docs if d.get("straatnaam") and pattern.fullmatch(_street_key(_slug(d["straatnaam"])))]
        if pc4:  # met postcode is het postcode-punt preciezer dan het midden van de straat
            hits.sort(key=lambda d: d.get("type") != "postcode")
        hits.sort(key=lambda d: _slug(d.get("woonplaatsnaam", "")) != place)
        if hits and _point(hits[0].get("centroide_ll")):
            lat, lon = _point(hits[0]["centroide_ll"])
            result = {"street": re.sub(r"\s+\d+$", "", hits[0]["straatnaam"]),
                      "place": hits[0].get("woonplaatsnaam"), "lat": lat, "lon": lon, "precision": "street"}
    if not result:
        docs = _pdok("free", {"q": place.replace("-", " "),
                              "fq": [f"gemeentecode:{gemeentecode}", "type:woonplaats"],
                              "rows": 5, "fl": "woonplaatsnaam"})
        hits = [d for d in docs if _slug(d["woonplaatsnaam"]) == place]
        if hits:
            result = {"street": None, "place": hits[0]["woonplaatsnaam"], "precision": "place"}
    _cache.set(key, result, ttl=24 * 3600 if result else 3600)
    return result or None


# ---------------------------------------------------------------- 112-nu.nl

class FeedError(Exception):
    pass


def _fetch_feed(service: str, slug: str) -> list[dict]:
    key = ("feed", service, slug)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        resp = _session.get(FEED_URL.format(service=service, slug=slug), timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        if "xml" not in resp.headers.get("Content-Type", ""):
            raise FeedError("geen RSS (bot-check?)")  # bv. een "ben je een mens"-pagina
        root = ElementTree.fromstring(resp.content)
    except (requests.RequestException, ElementTree.ParseError, FeedError) as exc:
        # Bron even weg: gebruik dan nog maximaal 10 minuten de vorige versie.
        stale = _cache.get(key, max_age=600)
        if stale is not None:
            return stale
        raise FeedError(str(exc)) from exc
    items = [
        {
            "guid": it.findtext("guid") or it.findtext("link"),
            "link": it.findtext("link") or "",
            "category": it.findtext("category") or "",
            "text": (it.findtext("description") or "").strip(),
            "time": _parse_time(it.findtext("pubDate")),
        }
        for it in root.iter("item")
    ]
    # De feeds worden eens per minuut ververst.
    _cache.set(key, items, ttl=60)
    return items


def _parse_time(value: str | None) -> float | None:
    t = parsedate(value or "")
    if not t:
        return None
    local = datetime(*t[:6], tzinfo=AMSTERDAM).timestamp()
    # Mocht 112-nu.nl ooit echte UTC gaan sturen, dan ligt "lokaal" in de toekomst.
    if local > time.time() + 300:
        return datetime(*t[:6], tzinfo=ZoneInfo("UTC")).timestamp()
    return local


# ---------------------------------------------------------------- P2000-tekst

# Bronnen voor de betekenis van de codes:
# - Ambulance: Ambulancezorg Nederland, Uniform Begrippenkader 4.0 (2020), par. 2.2:
#   A1 = acute bedreiging vitale functies, met zwaailicht en sirene;
#   A2 = geen direct levensgevaar, wel zo snel mogelijk, sirene "onder voorwaarden";
#   B = gepland vervoer (geen spoed). A0 staat er niet in.
# - Brandweer: Brandweer Nederland, nota basisprioriteiten in GMS (2018):
#   prio 1 = zo snel mogelijk, met zwaailicht en sirene; prio 2 = spoed maar
#   zonder zwaailicht en sirene; prio 3 = geen spoed.
# - Politie: geen openbare definitie gevonden; we tonen alleen het nummer.
_URGENCY = {
    ("ambulance", "A0"): ("Hoogste spoed", "onbekend", 0),
    ("ambulance", "A1"): ("Met spoed, levensbedreigend", "ja", 1),
    ("ambulance", "A2"): ("Spoed, niet levensbedreigend", "soms", 2),
    ("brandweer", "1"): ("Met spoed (prio 1)", "ja", 1),
    ("brandweer", "2"): ("Spoed (prio 2)", "nee", 2),
    ("politie", "1"): ("Prio 1 (hoogste)", "onbekend", 1),
    ("politie", "2"): ("Prio 2", "onbekend", 2),
    ("politie", None): ("Prio onbekend", "onbekend", 3),
}

_PRIO_A = re.compile(r"^\s*(A0|A1|A2|B1|B2|B)\b")
_PRIO_P = re.compile(r"^\s*(?:P|PRIO|Prio)\s?([1-5])\b")
_PRIO_POL = re.compile(r"^\s*([1-5])\s+(?=[A-Za-z])")

# Afkortingen uit de Landelijke Meldingsclassificaties, naar gewone taal.
# Volgorde telt: specifiek voor algemeen.
_WORDS = [
    (r"\(Kleine HV\)\s*", ""),
    (r"\(Middel BR\)", "(middelgrote brand)"),
    (r"\(Grote BR\)", "(grote brand)"),
    (r"\bHV weg letsel\b", "Verkeersongeval met letsel"),
    (r"\bOngeval wegvervoer letsel\b", "Verkeersongeval met letsel"),
    (r"\bOngeval wegvervoer materieel\b", "Verkeersongeval met schade"),
    (r"\bOngeval wegvervoer\b", "Verkeersongeval"),
    (r"\bAanrijding letsel\b", "Aanrijding met letsel"),
    (r"\bAanrijding materieel\b", "Aanrijding met schade"),
    (r"\bBR woning\b", "Woningbrand"),
    (r"\bBR wegvervoer\b", "Voertuigbrand"),
    (r"\bBR buiten\b", "Buitenbrand"),
    (r"\bBR gebouw\b", "Brand in gebouw"),
    (r"\bBR\b", "Brand"),
    (r"\bOMS brandmelding\b", "Automatische brandmelding"),
    (r"\bOMS handmelder\b", "Brandalarm ingedrukt"),
    (r"\bOMS\b", "Automatische melding"),
    (r"\bAss\. Ambu\b", "Assistentie ambulance"),
    (r"\bAss\. Politie\b", "Assistentie politie"),
    (r"\bAss\.", "Assistentie"),
    (r"\bHV\b", "Hulpverlening"),
    (r"\bVKO\b", "Verkeersongeval"),
    (r"\bgev\. stof\b", "gevaarlijke stof"),
    (r"\bhind\. lucht\b", "hinderlijke lucht"),
    (r"\bPers\.", "Persoon"),
]
_WORDS = [(re.compile(p, re.I), r) for p, r in _WORDS]

_NOISE = [
    r"\((?:DIA|Directe inzet)\s*:?\s*ja\)", r"\bDIA\b", r"\bDirecte inzet\b",
    r"\(Intrekken Alarm Brw\)", r"\bAMBU\b(?:\s+\d{5})?",
    r"\b(?:Rit|bon|ICnum)\b\s*:?\s*\d+", r"//.*$", r",?\s*\bregio\s*\d+",
    r"\b[A-Z]{3}-\d{2}\b", r"\b\d{2}-\d{2}-\d{3}\b",
    r"\((?:OVD|HOVD|CVD|AGS|KRA)\b[^)]*\)", r"\(ovdg\)", r"\([^)]*\bTHV\b[^)]*\)", r"\([\w ]*:\s*\)",
    r"\b(?:graag\s+)?contact\s+(?:MKB|MKA|MK|ZCC|OC)\b(?:\s+AUB)?", r"\bprio\s?[1-5]\b",
    r"\b\d{4}\s?[A-Z]{2}\b", r"\b\d{4,}\b", r"^\s*\d+\s",
]
_NOISE = [re.compile(p, re.I) for p in _NOISE]

# Een tweede straatnaam aan het eind (kruising) hoort niet bij "wat".
_TRAILING_STREET = re.compile(
    r"(\s+[\w'.-]*(straat|weg|laan|plein|singel|kade|dijk|pad|ring|baan|hof|gracht|dreef|steeg|brug|allee)"
    r"(-(noord|oost|zuid|west))?)+$", re.I)

# Geen melding maar een dienstbericht of test.
_NOT_AN_INCIDENT = re.compile(r"\btest|proefalarm|geplande actie|\bcontact\b", re.I)

# Bij ambulance staat vóór de straat vaak een gebouw of bedrijf in plaats
# van wat er aan de hand is. Daarom alleen tonen als het herkenbaar is.
_AMBU_KNOWN = re.compile(r"reanimatie|ongeval|letsel|aanrijding|te water|brand|steek|schiet|explosie|beknel", re.I)


def _priority(service: str, text: str) -> str | None:
    m = _PRIO_A.match(text)
    if m:
        return m.group(1)
    m = _PRIO_P.match(text)
    if not m and service == "politie":
        # Rotterdam zet alleen een cijfer vooraan, Groningen "prio 1" midden in de tekst.
        m = _PRIO_POL.match(text) or re.search(r"\bprio\s?([1-5])\b", text, re.I)
    return m.group(1) if m else None


def _what(service: str, text: str, cut_at: list[str]) -> str | None:
    s = _PRIO_A.sub("", text, count=1)
    s = _PRIO_P.sub("", s, count=1)
    if service == "politie":
        s = _PRIO_POL.sub("", s, count=1)
    # Opbouw is meestal: <code> <wat> [gebouw] <straat> <plaats> <nummers>.
    # Knip af bij de eerste straat- of plaatsnaam, of bij een snelweg.
    cut = len(s)
    for name in cut_at:
        if name:
            m = re.search(r"\b" + re.escape(name) + r"\b", s, re.I)
            if m:
                cut = min(cut, m.start())
    m = re.search(r"\b[ANS]\d{1,3}\b", s)
    if m:
        cut = min(cut, m.start())
    s = s[:cut]
    for rx, repl in _WORDS:
        s = rx.sub(repl, s)
    for rx in _NOISE:
        s = rx.sub(" ", s)
    s = re.sub(r"\(\s*\)", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -,:/.")
    s = _TRAILING_STREET.sub("", s).strip(" -,:/.")
    # Politie Groningen: "Ongeval/Wegvervoer/Letsel" -> "Wegvervoer, letsel".
    parts = [p.strip() for p in s.split("/")]
    if len(parts) >= 3:
        s = parts[-2] + ", " + parts[-1][:1].lower() + parts[-1][1:]
    # "(middelgrote brand) Woningbrand" -> "Woningbrand (middelgrote brand)"
    m = re.match(r"^(\([^)]*\))\s+(.+)$", s)
    if m:
        s = f"{m.group(2)} {m.group(1)}"
    if not re.search(r"[A-Za-z]{3}", s):
        return None
    if service == "ambulance" and not _AMBU_KNOWN.search(s):
        return None
    return s[0].upper() + s[1:]


def _location_from_link(link: str) -> tuple[str, str | None, str | None] | None:
    # https://112-nu.nl/melding/<id>/<plaats>[/<straat>[/<postcode4>]]/<soort>.html
    parts = urlparse(link).path.strip("/").split("/")
    if len(parts) < 4 or parts[0] != "melding":
        return None
    middle = parts[2:-1]
    place = middle[0]
    street = next((p for p in middle[1:] if not p.isdigit()), None)
    pc4 = next((p for p in middle[1:] if re.fullmatch(r"\d{4}", p)), None)
    return place, street, pc4


def _build(service: str, gemeentecode: str, item: dict, lat: float, lon: float, near_wp: set) -> dict | None:
    text = item["text"]
    prio = _priority(service, text)
    urgency = _URGENCY.get((service, prio))
    if urgency is None or _NOT_AN_INCIDENT.search(text):
        return None  # geen spoed (B-rit, prio 3) of geen echte melding (test, dienstbericht)

    loc = _location_from_link(item["link"])
    if not loc:
        return None
    place_slug, street_slug, pc4 = loc
    geo = _geocode(gemeentecode, place_slug, street_slug, pc4)
    place = geo["place"] if geo else None
    street = geo["street"] if geo else None
    precision = geo["precision"] if geo else None

    distance = direction = None
    if precision == "street":
        distance = _haversine_m(lat, lon, geo["lat"], geo["lon"])
        if distance > RADIUS_M:
            return None
        direction = _compass(_bearing_deg(lat, lon, geo["lat"], geo["lon"])) if distance > 300 else None
    elif f"{gemeentecode}:{place_slug}" not in near_wp:
        return None  # alleen de plaats bekend, en die ligt niet binnen de straal

    cut_at = [street, place, street_slug.replace("-", " ") if street_slug else None, place_slug.replace("-", " ")]
    what = _what(service, text, cut_at)
    if prio is None and what is None:
        return None  # politiebericht zonder prio en zonder herkenbare inhoud
    return {
        "id": item["guid"],
        "service": service,
        "code": prio,
        "urgency": urgency[0],
        "siren": urgency[1],
        "_rank": urgency[2],
        "what": what,
        "cancelled": bool(re.search(r"intrekken|ingetrokken", text, re.I)),
        "street": street,
        "place": place,
        "precision": precision,
        "distance_m": round(distance) if distance is not None else None,
        "direction": direction,
        "time": int(item["time"]),
        "_number": _incident_number(service, text),
    }


def _incident_number(service: str, text: str) -> str | None:
    m = re.search(r"\b(?:Rit|bon|ICnum)\b\s*:?\s*(\d{5,})", text, re.I)
    if not m and service == "ambulance":
        # Sommige regio's zetten het ritnummer kaal achteraan ("A1 Utrecht 172355").
        # Bij de brandweer zijn de getallen achteraan pagernummers, geen incident.
        m = re.search(r"\b(\d{5,6})\s*$", text)
    return m.group(1) if m else None


def _merge(incidents: list[dict]) -> list[dict]:
    """Eén incident levert vaak meerdere pagers op (per voertuig, of eerst
    alarm en later intrekken). Voeg die samen tot één kaartje."""
    incidents.sort(key=lambda x: x["time"])
    merged: list[dict] = []
    for inc in incidents:
        twin = None
        for m in merged:
            if m["service"] != inc["service"] or inc["time"] - m["time"] > 20 * 60:
                continue
            if inc["_number"] and m["_number"]:
                if inc["_number"] == m["_number"]:
                    twin = m
                    break
                continue  # verschillende ritnummers: echt twee inzetten
            same_place = (m["street"], m["place"]) == (inc["street"], inc["place"]) and m["place"]
            # Zelfde straat: ook als de omschrijving verandert (bv. opgeschaald
            # van "brand bijgebouw" naar "middelgrote woningbrand"). Zonder
            # straat alleen bij precies dezelfde omschrijving.
            if same_place and (m["precision"] == "street" or (m["what"] and m["what"] == inc["what"])):
                twin = m
                break
        if twin is None:
            merged.append(inc)
            continue
        if inc["_rank"] < twin["_rank"]:
            for k in ("code", "urgency", "siren", "_rank"):
                twin[k] = inc[k]
        twin["what"] = inc["what"] or twin["what"]  # nieuwste omschrijving
        twin["cancelled"] = twin["cancelled"] or inc["cancelled"]
    return merged


@app.get("/api/incidents")
def incidents():
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        abort(400)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        abort(400)

    try:
        near = _nearby(lat, lon)
    except (requests.RequestException, ValueError, KeyError):
        return jsonify({"error": "De plaatsnamen-dienst (PDOK) is nu even niet bereikbaar."}), 503
    gemeenten = {code: name for code, name in near["gemeenten"].items() if code in GEMEENTE_SLUGS}
    if not gemeenten:
        return jsonify({"incidents": [], "gemeenten": [], "outside_nl": True, "time": int(time.time())})
    near_wp = set(near["woonplaatsen"])

    jobs = [(svc, code, _pool.submit(_fetch_feed, svc, GEMEENTE_SLUGS[code]))
            for code in gemeenten for svc in SERVICES]
    since = time.time() - WINDOW_S
    raw, failed = [], 0
    for svc, code, fut in jobs:
        try:
            items = fut.result()
        except FeedError:
            failed += 1
            continue
        raw += [(svc, code, it) for it in items if it["time"] and it["time"] >= since]
    if failed == len(jobs):
        return jsonify({"error": "112-nu.nl is nu even niet bereikbaar."}), 503

    def build(job):
        svc, code, it = job
        try:
            return _build(svc, code, it, lat, lon, near_wp)
        except (requests.RequestException, ValueError, KeyError):
            return None  # PDOK even weg: deze melding overslaan, volgende keer opnieuw

    found = [x for x in _pool.map(build, raw) if x]
    result = sorted(_merge(found), key=lambda x: -x["time"])[:MAX_RESULTS]
    for inc in result:
        inc.pop("_rank"), inc.pop("_number")
    return jsonify({
        "incidents": result,
        "gemeenten": sorted(gemeenten.values()),
        "radius_m": RADIUS_M,
        "partial": failed > 0,
        "time": int(time.time()),
    })


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/manifest.json")
def manifest():
    return jsonify(
        {
            "name": "Sirene in de buurt",
            "short_name": "Sirene",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#0f172a",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        }
    )


@app.get("/sw.js")
def service_worker():
    # Lege service worker: alleen nodig zodat Chrome de app installeerbaar vindt.
    return Response("self.addEventListener('fetch', () => {});", mimetype="application/javascript")


@app.get("/icon-<int:size>.png")
def icon(size: int):
    if size not in (192, 512):
        abort(404)
    return send_from_directory(STATIC_DIR, f"icon-{size}.png")


@app.get("/icons/<name>")
def icons(name: str):
    # 3D-icoontjes van thiings.co (gratis licentie, naamsvermelding staat in de footer).
    return send_from_directory(STATIC_DIR / "icons", name, max_age=7 * 24 * 3600)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
