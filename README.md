# Viditelnost v AI

Měří, jak často AI nástroje (ChatGPT, Gemini, Perplexity) jmenují firmu, když se jich zákazník zeptá na doporučení, a ukazuje to v portálu.

## Jak to funguje

1. `config.json` obsahuje firmu, její web a seznam dotazů, které by položil zákazník.
2. `tracker/run.mjs` položí každý dotaz každému zapnutému nástroji (s hledáním na webu, lokalita Česko), z odpovědi vypíše jmenované firmy a zdroje a uloží měření do `data/runs/<datum>.json`.
3. Ze všech měření se přepočítá `data/dashboard.json`.
4. `portal/build.mjs` vloží data do `portal/template.html` a vytvoří `portal/index.html`, samostatnou stránku portálu.

## Příkazy

```
npm run measure                              # ostré měření podle config.json
node portal/build.mjs                        # portál z data/dashboard.json
npm run sample                               # ukázková data bez sítě a klíčů
node portal/build.mjs sample/dashboard.json  # portál z ukázkových dat
npm test                                     # testy vyhodnocení odpovědí
```

Další volby měření: `--limit 3` (jen první tři dotazy, na zkoušku), `--rebuild` (jen přepočet), `--date RRRR-MM-DD`.

## Klíče k API

Skript čte `OPENAI_API_KEY`, `GEMINI_API_KEY` a `PERPLEXITY_API_KEY`. V cloudové relaci Claude Code se klíče neukládají do proměnných; doplňuje je síťová proxy prostředí. Nástroj bez klíče vypněte v `config.json` (`"enabled": false`).

Model se u ChatGPT a Gemini vybírá automaticky (nejnovější malý model); pevně ho lze nastavit v `config.json` jako `"model": "..."`.

## Co čísla znamenají

- **Viditelnost**: podíl odpovědí, ve kterých AI firmu jmenuje.
- **Odkazy na váš web**: podíl odpovědí, které web firmy uvádějí jako zdroj.
- **Průměrné pořadí**: kolikátá je firma mezi jmenovanými, když ji AI zmíní.

Odpovědi AI se mezi dvěma položeními stejného dotazu liší a odpověď přes API se může lišit od odpovědi v aplikaci. Jedno měření je orientační; vypovídající je vývoj přes několik týdnů.
