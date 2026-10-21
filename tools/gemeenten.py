"""Maak gemeenten.json opnieuw: CBS-gemeentecode -> slug die 112-nu.nl gebruikt.

Nodig na een gemeentelijke herindeling (meestal per 1 januari). Haalt alle
gemeenten op bij PDOK en probeert per gemeente een paar slugs bij 112-nu.nl,
rustig (een paar honderd verzoeken, ~5 minuten). Draaien vanuit de repo-map:

    python tools/gemeenten.py
"""

import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

PDOK = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
FEED = "https://112-nu.nl/ambulance/gemeente-{}/rss/rss"
OUT = Path(__file__).resolve().parent.parent / "gemeenten.json"

# Namen die 112-nu.nl heel anders schrijft dan PDOK.
OVERRIDES = {"'s-Gravenhage": "den-haag", "Nuenen, Gerwen en Nederwetten": "nuenen-c-a"}

session = requests.Session()
session.headers["User-Agent"] = "sirene-in-de-buurt/1.0 (gemeenten.py)"


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def candidates(name: str) -> list[str]:
    c = [OVERRIDES.get(name), slug(name)]
    if name.startswith("'s-"):
        c.append(slug(name[3:]))
    c.append(slug(re.sub(r"\s*\(.*\)", "", name)))  # "Hengelo (O)" -> hengelo
    c.append(slug(name.replace("(NH)", "Noord-Holland").replace("(L)", "Limburg")))
    c.append(slug(name.split(",")[0]))
    return [x for x in dict.fromkeys(c) if x]


def feed_slug(candidate: str) -> str | None:
    for attempt in range(3):
        try:
            resp = session.get(FEED.format(candidate), timeout=20)
            break
        except requests.RequestException:
            time.sleep(3)
    else:
        return None
    time.sleep(0.4)
    if resp.status_code != 200 or "xml" not in resp.headers.get("Content-Type", ""):
        return None
    # 112-nu.nl stuurt oude namen door naar de nieuwe gemeente; neem de eindbestemming.
    m = re.search(r"/gemeente-([^/]+)/rss", resp.url)
    return m.group(1) if m else None


def main() -> None:
    gemeenten = []
    for start in range(0, 500, 100):
        resp = session.get(PDOK, params={"q": "*", "fq": "type:gemeente", "rows": 100, "start": start,
                                         "fl": "gemeentenaam,gemeentecode", "sort": "gemeentecode asc"}, timeout=20)
        docs = resp.json()["response"]["docs"]
        gemeenten += docs
        if len(docs) < 100:
            break
    result, missing = {}, []
    for g in gemeenten:
        found = next((s for s in map(feed_slug, candidates(g["gemeentenaam"])) if s), None)
        print(g["gemeentecode"], g["gemeentenaam"], "->", found, file=sys.stderr)
        if found:
            result[g["gemeentecode"]] = found
        else:
            missing.append(g["gemeentenaam"])
    OUT.write_text(json.dumps(result, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(result)} gemeenten geschreven naar {OUT.name}")
    if missing:
        print("Niet gevonden (voeg toe aan OVERRIDES):", ", ".join(missing))


if __name__ == "__main__":
    main()
