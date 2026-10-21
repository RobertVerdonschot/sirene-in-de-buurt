# Sirene in de buurt

Simpele webapp: hoor je een sirene, open de app en je ziet welke
spoedmeldingen van brandweer, ambulance en politie er het afgelopen uur binnen
3 km van je waren. Nieuwste eerst. Per melding: wat er aan de hand is, welke
dienst, hoeveel spoed, waar, hoe ver van je en hoe lang geleden. Geen kaart,
geen codes.

Zusje van [WatVliegtDaar](https://github.com/RobertVerdonschot/WatVliegtDaar),
zelfde opzet.

## Neerzetten (Google Cloud Shell)

1. Open [Google Cloud Shell](https://shell.cloud.google.com) en log in met het
   Google-account van het project. Onderin opent een terminal.
2. Plak deze regels en druk op Enter:

   ```bash
   git clone https://github.com/RobertVerdonschot/sirene-in-de-buurt.git
   cd sirene-in-de-buurt
   gcloud config set project meal-stress-analyzer
   ```

   Vraagt Cloud Shell om *Authorize*, klik dan op **Authorize**.
3. Controleer dat de meldingenbron bereikbaar is vanaf een Google-machine
   (Cloud Shell zit in hetzelfde netwerk als Cloud Run):

   ```bash
   curl -s -o /dev/null -w '%{http_code} %{content_type}\n' https://112-nu.nl/ambulance/gemeente-utrecht/rss/rss
   ```

   Goed is: `200 application/rss+xml; charset=utf-8`. Zie je iets anders
   (bv. `401 text/html`), dan blokkeert 112-nu.nl Google-servers en werkt de
   app daar niet; zie *Beperkingen*.
4. Zet de app neer:

   ```bash
   ./deploy.sh
   ```

   De eerste keer vraagt `gcloud` misschien of er een Artifact Registry-repository
   mag worden aangemaakt: antwoord `Y`. Het bouwen duurt een paar minuten.
   Draait in hetzelfde Google Cloud-project als WatVliegtDaar, als losse
   Cloud Run-service `sirene-in-de-buurt`. Geen secrets nodig.
5. Het script print een URL. Open die op je telefoon in Chrome, geef
   toestemming voor je locatie, en kies menu (⋮) > *Toevoegen aan startscherm*.

**Later bijwerken** (Cloud Shell onthoudt je bestanden, maar niet het project):

```bash
cd sirene-in-de-buurt
git pull
gcloud config set project meal-stress-analyzer
./deploy.sh
```

Kosten: binnen de gratis tier van Cloud Run, zolang alleen jij 'm af en toe
gebruikt.

## Hoe het werkt

- Je telefoon stuurt je locatie naar de server (`/api/incidents`), elke 15
  seconden zolang de app open staat.
- De server vraagt bij [PDOK](https://www.pdok.nl/introductie/-/article/pdok-locatieserver-1)
  (de gratis plaatsnamen-dienst van de overheid) welke gemeenten en
  woonplaatsen binnen 3 km liggen.
- Per gemeente haalt hij de RSS-feeds van [112-nu.nl](https://112-nu.nl) op
  voor brandweer, ambulance en politie. Elke feed bevat de 30 nieuwste
  meldingen van die gemeente; dat is ook in Amsterdam of Rotterdam ruim meer
  dan een uur. Daardoor is er geen opslag nodig, ook al schaalt Cloud Run
  naar 0 en is het geheugen tussendoor weg.
- Meldingen hebben geen coördinaten, alleen straat en plaats. De server zoekt
  de straat op bij PDOK (binnen de juiste gemeente) en rekent de afstand uit
  vanaf het midden van die straat. Vindt PDOK de straat niet precies, dan
  toont de app alleen de plaats en "afstand onbekend".
- Alleen spoed: B-ritten (gepland ambulancevervoer), brandweer prio 3 en lager
  en politie prio 3 en lager vallen weg. Test- en dienstberichten ook.
- Meerdere pagers voor hetzelfde incident (meerdere voertuigen, opschaling,
  intrekken) worden samengevoegd tot één kaartje.
- In-memory cache: feeds 60 s (zo vaak verversen ze), straatposities 24 uur,
  gemeenten bij een positie 6 uur. Valt 112-nu.nl even weg, dan gebruikt de
  server nog maximaal 10 minuten de vorige versie.

Straal en tijdvenster staan bovenin `main.py` (`RADIUS_M`, `WINDOW_S`).
Elke extra gemeente binnen de straal kost 3 feedverzoeken per minuut zolang
de app open staat, dus maak de straal niet onnodig groot.

## Wat de codes betekenen

| Code | In de app | Bron |
|------|-----------|------|
| A1 | Met spoed, levensbedreigend. Zwaailicht en sirene: ja | Ambulancezorg Nederland, [Uniform Begrippenkader 4.0](https://www.ambulancezorg.nl/static/upload/raw/fc57648d-70dd-432b-81a8-9dfc3e12c536/201028+2.2+%28niet%29+spoedeisend.pdf), 2.2 |
| A2 | Spoed, niet levensbedreigend. Sirene: soms ("onder bepaalde voorwaarden") | idem |
| A0 | Hoogste spoed. Sirene: onbekend | Staat niet in het begrippenkader; wordt in de praktijk o.a. bij reanimaties gebruikt. We tonen hem wel, maar beloven niks over de sirene. |
| B, B1, B2 | (niet getoond: gepland vervoer, geen spoed) | Uniform Begrippenkader 4.0 |
| Brandweer prio 1 | Met spoed. Zwaailicht en sirene: ja | Brandweer Nederland, [nota basisprioriteiten in GMS](https://archief.nipv.nl/documenten/nota-van-toelichting-aan-lmc-gekoppelde-basisprioriteiten-in-gms) (2018) |
| Brandweer prio 2 | Spoed. Zwaailicht en sirene: nee | idem: "spoed maar geen optische en geluidsignalen" |
| Brandweer prio 3+ | (niet getoond: geen spoed) | idem |
| Politie 1 / 2 | Prio 1 (hoogste) / Prio 2. Sirene: onbekend | Geen openbare definitie gevonden, dus alleen het nummer. |
| Politie zonder prio | Prio onbekend | Veel politieberichten hebben geen prio; die tonen we wel. |

Afkortingen in de meldingstekst (BR, OMS, Ass. Ambu, HV, gev. stof, ...)
worden vertaald naar gewone taal. Bij ambulance staat meestal niet in de
tekst wat er aan de hand is; dan zegt de app "soort melding niet vermeld" in
plaats van te gokken.

## Beperkingen, eerlijk gezegd

- **Bereikbaarheid vanaf Google.** De webpagina's van 112-nu.nl zitten achter
  een bot-check; de RSS-feeds (nu) niet. Getest vanaf een gewone
  internetverbinding, niet vanaf Cloud Run zelf. Stap 3 van *Neerzetten*
  test dit. Gaat 112-nu.nl ook de feeds afschermen, dan stopt de app en moet
  er een andere bron komen.
- **Politie is dun.** De politie alarmeert maar een deel via P2000 en lang
  niet elke regio zet een prio in de tekst. Zie je geen politie, dan betekent
  dat niet dat er geen politie rijdt.
- **Afstand is tot het midden van de straat.** Bij lange wegen kan dat een
  paar honderd meter schelen. Een melding op een snelweg krijgt geen afstand.
- **Gemeentegrens als filter.** Meldingen met alleen een plaatsnaam (vaak bij
  ambulance in Utrecht of Eindhoven) tonen we als die woonplaats binnen 3 km
  begint; de melding zelf kan aan de andere kant van de stad zijn.
- **Vertalingen zijn regels, geen AI.** Een onbekende afkorting komt er
  letterlijk door. Een gebouwnaam kan in de omschrijving blijven staan
  ("Reanimatie Albert Heijn").
- **Gemeentelijke herindelingen.** `gemeenten.json` koppelt elke gemeente aan
  de naam die 112-nu.nl gebruikt. Na een herindeling (meestal 1 januari):
  `python tools/gemeenten.py`, dan opnieuw deployen. Tot die tijd mist de
  app meldingen in een nieuwe gemeente.
- **De URL is openbaar.** Geen login. Wie de link heeft kan 'm gebruiken.
- **Niet voor noodgevallen.** P2000 loopt soms achter of mist berichten. Bel
  112 bij nood.

## Databronnen

- **[112-nu.nl](https://112-nu.nl)**: hun RSS-feeds mogen volgens de feed zelf
  op "een redactionele, commerciële of andersoortige website" worden
  geplaatst, mits er minstens één link (zonder `rel=nofollow`) naar 112-nu.nl
  staat. Die staat in de footer. Verder vragen ze geen grote hoeveelheden data
  binnen te halen; daarom de cache en de kleine straal.
- **PDOK Locatieserver**: gratis geocoder van de overheid, geen key nodig.
- Waarom niet een andere P2000-bron: de feeds van livep2000.nl staan sinds
  2021 uit, de feeds van alarmeringen.nl staan onder CC BY-NC-ND en bevatten
  maar 30 meldingen per regio, en de P2000-feed van brandweer-berkel-enschot.nl
  gaat maar ~5 minuten terug en heeft onbetrouwbare coördinaten.

## Icoontjes

De 3D-icoontjes in `static/icons/` en het startscherm-icoon komen van
[thiings.co](https://www.thiings.co), onder hun gratis licentie: alleen voor
persoonlijk, niet-commercieel gebruik, met zichtbare naamsvermelding (staat
in de footer van de app). Die licentie geldt voor de icoontjes, niet voor de
rest van deze repo. Wil je de app ooit commercieel inzetten, dan heb je een
betaalde licentie nodig of moeten de icoontjes eruit.

## Lokaal draaien

```bash
pip install -r requirements.txt
python main.py   # http://localhost:8080
```

Locatie werkt in de browser alleen via `localhost` of `https`.
