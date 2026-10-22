# AI obchodník pro Freeheating

Systém, který sám najde na internetu firmy, které by mohly potřebovat mobilní kotelnu, nechá je obvolat
AI obchodníkem ve Vapi, hovory vyhodnotí a výsledky pošle Freeheatingu e-mailem: horké leady hned,
všechno ostatní v pondělním týdenním reportu.

```
 1. HLEDÁNÍ         každý pracovní den v 6:00
    Claude prohledá web (volitelně i Google Maps) po segmentech a krajích,
    ověří firmu, telefon a IČO (ARES), dá skóre 0–100 a větu „proč voláme právě vám“
        │
 2. DATABÁZE        deduplikace podle telefonu, webu a IČO, seznam „nevolat“
        │
 3. VOLÁNÍ          po–pá 9:00–11:45 a 13:00–16:30, ne o svátcích, max. N hovorů naráz / za den
    Vapi asistent dostane {{company}}, {{pitch_hook}}, {{contact_name}} …
        │
 4. VYHODNOCENÍ     Claude přečte přepis → horký / vlažný / zavolat znovu / bez zájmu / nevolat
    nezvedli → zkusí znovu za 3 h, 24 h, 48 h; „zavolejte v úterý“ → zavolá v úterý
        │
 5. E-MAILY         horký lead → Freeheatingu hned (kontakt, co potřebují, nahrávka, přepis)
                    pondělí 7:30 → týdenní report + CSV pro Excel
```

Vše běží jako jeden malý server (Python, SQLite). Má i jednoduchý přehled v prohlížeči s exporty do CSV.

## Rychlý start

Potřebujete: API klíč Claude (console.anthropic.com), Vapi (API klíč, ID asistenta, ID telefonního čísla)
a SMTP schránku, ze které budou chodit e-maily.

```bash
cp .env.example .env               # klíče, e-maily, heslo do přehledu
cp config.example.yaml config.yaml # koho hledat, kdy volat (předvyplněno pro Freeheating)
docker compose up -d --build

docker compose exec leadgen python -m leadgen init              # kontrola nastavení
docker compose exec leadgen python -m leadgen discover --tasks 1 # první hledání (pár minut)
docker compose exec leadgen python -m leadgen preview            # koho by volal
docker compose exec leadgen python -m leadgen test-call +420777123456  # zavolá vám
```

Až test sedí, nastavte v `config.yaml` `calling.enabled: true` a `docker compose restart`.
Volání je ve výchozím stavu **vypnuté**, aby nic nevytáčelo dřív, než to chcete.

Bez Dockeru: `python -m venv .venv && . .venv/bin/activate && pip install -e .`, pak
`python -m leadgen serve`.

### Nasazení

Server musí běžet nonstop (VPS, Railway, Render, Fly.io…) s trvalým diskem pro složku `data/`
(databáze). Spouštějte **jednu** instanci – plánovač běží uvnitř serveru.

Pokud má server veřejnou HTTPS adresu, vyplňte `PUBLIC_BASE_URL` a `WEBHOOK_SECRET`: Vapi pak po každém
hovoru pošle výsledek na `/vapi/webhook` a horký lead odejde během chvilky. Bez veřejné adresy to funguje
taky – systém si výsledky každé 3 minuty stáhne z Vapi sám.

## Nastavení Vapi

Použije se váš stávající asistent. Co do něj doplnit (proměnné, úvodní věta, nahrávání, detekce schránky)
a návrh promptu je v [docs/vapi-asistent.md](docs/vapi-asistent.md).

## Co a kdy chodí e-mailem

- **Hned – horký lead** (`reporting.instant_outcomes`, lze přidat i `warm`): firma, kontaktní osoba,
  telefon, e-mail, co přesně potřebují, naléhavost, doporučený další krok, odkaz na nahrávku a celý přepis.
- **Každé pondělí – týdenní report**: kolik firem se našlo, kolik hovorů proběhlo, s kolika se mluvilo,
  tabulky horkých a vlažných leadů, domluvené zpětné hovory na další týden, souhrn výsledků a CSV se všemi
  hovory (otevře se rovnou v Excelu).
- Výsledky zkušebních hovorů (`test-call`) chodí jen na `TEST_EMAIL`, nikdy Freeheatingu.

Bez nastaveného SMTP se e-maily ukládají do `data/outbox/*.eml` – hodí se na vyzkoušení.

## Přehled v prohlížeči

`https://vas-server/` (jméno a heslo z `ADMIN_USER` / `ADMIN_PASSWORD`): stavy leadů, výsledky, poslední hovory
s nahrávkami, export leadů a hovorů do CSV.

## Příkazy

`python -m leadgen <příkaz>` (v Dockeru `docker compose exec leadgen python -m leadgen <příkaz>`):

| Příkaz | Co dělá |
|---|---|
| `init` | vytvoří databázi a vypíše nastavení |
| `discover [--tasks N] [--force]` | najde nové firmy hned (jinak běží samo ráno) |
| `plan` | co se bude hledat příště |
| `qualify` | AI posoudí firmy bez skóre (z Google Maps nebo importu) |
| `preview` | koho systém zavolá jako další |
| `test-call +420…` | zkušební hovor na vaše číslo včetně vyhodnocení |
| `dial` / `reconcile` | jedno kolo vytáčení / dotažení výsledků z Vapi |
| `report [--send]` | týdenní report teď (bez `--send` jen do `data/outbox`) |
| `import-leads soubor.csv [--score 80]` | nahraje vlastní seznam firem (firma;telefon;web;…) |
| `import-blocklist zakaznici.csv` | stávající zákazníci Freeheatingu – nebudou se volat |
| `block +420… [--kind phone\|domain\|ico]` | ručně na seznam nevolat |
| `stats` | čísla v terminálu |

## Konfigurace (`config.yaml`)

- `client` – co Freeheating nabízí, ideální zákazník, koho vynechat. Čte to AI, pište konkrétně.
- `segments` – typy firem (správci nemovitostí, SVJ, topenáři, stavebníci, teplárny, průmysl, nemocnice…).
  Každý má popis pro AI a volitelně dotazy pro Google Maps. Vypnutí: `enabled: false`.
- `discovery` – zdroje (`ai_search`, `google_places`), kdy hledat, kolik úloh a firem, minimální skóre,
  kraje a `intent_searches`: hledání firem s aktuální potřebou (veřejné zakázky na rekonstrukci kotelen,
  havárie a odstávky tepla). Ty se obnovují každý týden a mají přednost – jsou to nejteplejší kontakty.
- `calling` – okna pro volání, kapacita, počet pokusů, prodlevy mezi pokusy, úvodní věta.
- `reporting` – kdy týdenní report, které výsledky posílat hned.

Systém postupně prochází všechny kombinace segment × kraj (nejdéle neprohledané první) a nehledá, když
ve frontě čeká dost firem (`target_queue_size`) – neplatí se tedy za hledání do zásoby.

## Náklady

Orientačně, ceny si ověřte v aktuálních cenících:

- **Claude** – hledání je nejdražší část: jedna úloha (segment × kraj, ~12 firem) typicky desítky Kč
  (tokeny + webové vyhledávání). Vyhodnocení hovoru a posouzení firmy stojí haléře až jednotky Kč.
  Ladí se přes `tasks_per_run`, `max_web_searches`, `effort`; levnější model: `CLAUDE_MODEL=claude-sonnet-5-5`.
- **Vapi** – podle minut hovorů a zvolených poskytovatelů (hlas, přepis, telefonie).
- **Google Places** (volitelné) – placené za dotaz.

## Právní rizika – projděte před ostrým spuštěním

Tohle není právní rada, ale body, které je potřeba s právníkem (ideálně spolu s Freeheatingem) vyřešit:

1. **Telemarketing.** Od roku 2022 platí v ČR podle zákona o elektronických komunikacích (§ 96) pro
   marketingové hovory princip opt-in: volat se smí jen tomu, kdo s tím souhlasil (typicky zápisem ve
   veřejném seznamu účastníků) – a podle výkladů se to týká i firem, nejen spotřebitelů. Dozoruje
   ÚOOÚ, viz jeho [výkladové stanovisko k telemarketingu](https://uoou.gov.cz/media/poskytnute-informace/2022/1-9-2022/telemarketing-vykladove-stanovisko-s-odpovedmi-na-nejcastejsi-dotazy-verejnosti.pdf).
   Studené volání na čísla z webů je proto rizikové. Co systém dělá pro snížení rizika: upřednostňuje firmy
   se zjevnou potřebou (vypsané zakázky a poptávky), volá jen na firemní čísla, ukládá a respektuje
   „nevolat“ (telefon, doména i IČO), umí vyřadit stávající zákazníky. Rozhodnutí, zda a komu volat,
   je ale na vás.
2. **AI Act (čl. 50).** Volaný musí vědět, že mluví s AI – proto úvodní věta „virtuální asistentka…“.
3. **Nahrávání hovorů** – oznámit hned na začátku (je v úvodní větě).
4. **GDPR** – jména a kontakty osob ve firmách: oprávněný zájem, informační povinnost, doba uchování
   (přepisy a nahrávky nedržet déle, než je potřeba).
5. **Google Maps** – podmínky Google omezují dlouhodobé ukládání dat z Places API. Proto je výchozím
   zdrojem AI rešerše z veřejných webů firem; Google Places zapínejte vědomě.

## Struktura projektu

```
leadgen/
  discovery/      hledání: ai_search.py (Claude + web), google_places.py, ares.py, plánování úloh
  qualify.py      AI posouzení firem bez skóre
  dialer.py       spouštění hovorů ve Vapi (okna, kapacita, pořadí)
  calls.py        zpracování konce hovoru, opakování, zpětná volání, nevolat
  classify.py     AI vyhodnocení přepisu
  reports.py      e-mail s horkým leadem, týdenní report, CSV
  web.py          webhook pro Vapi, přehled, exporty
  scheduler.py    co kdy běží
  db.py           SQLite
tests/            python -m pytest
```
