# Viditelnost v AI (Chatbotique)

Měří, jak často AI nástroje jmenují firmu Chatbotique (chatbotique.eu), a ukazuje to v HTML portálu. Popis a příkazy jsou v `README.md`.

## Stav

- Portál je publikovaný jako artifact: https://claude.ai/artifact/LHVujnBLt3Year5XvXfYSC. Aktualizuje se publikováním `portal/index.html` na tuto adresu (ne jako nový artifact).
- Zatím existuje jedno měření (`data/runs/2026-11-12.json`): jen Gemini, bez hledání na webu, 10 z 18 dotazů. Bezplatný tarif Gemini dovoluje 20 požadavků denně na model a hledání na webu odmítá.
- V `config.json` je proto `gemini.search: false` a pevný model. Po zapnutí fakturace u Gemini obojí odstranit; ChatGPT a Perplexity zapnout (`enabled: true`), až budou klíče.
- Volání OpenAI a Perplexity v `tracker/engines.mjs` nebyla ověřena proti skutečnému API.
- Výpis firem z odpovědí zatím nerozlišuje dodavatele od zmíněných nástrojů (v žebříčku se objevuje např. OpenAI nebo Shoptet).

## Pravidla

- API klíče nikdy neukládat do souborů ani do repozitáře; čtou se z proměnných prostředí.
- Portál je jedna samostatná HTML stránka v barvách chatbotique.eu (tokeny v `portal/template.html`). Upravuje se šablona, `portal/index.html` je výstup z `portal/build.mjs`.
- Texty v portálu jsou česky.
- Po každém měření: `node portal/build.mjs`, publikovat portál, commitnout `data/`.
