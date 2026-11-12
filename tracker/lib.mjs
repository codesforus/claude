// Čisté funkce: normalizace názvů, detekce zmínky a souhrny. Bez sítě, bez I/O.

export const ENGINE_LABELS = { chatgpt: 'ChatGPT', gemini: 'Gemini', perplexity: 'Perplexity' };
export const ENGINE_IDS = Object.keys(ENGINE_LABELS);

const LEGAL_SUFFIX = /[\s,]+(s\.?\s?r\.?\s?o\.?|a\.?\s?s\.?|spol\.?(\s+s\s+r\.?\s?o\.?)?|k\.?\s?s\.?|v\.?\s?o\.?\s?s\.?|z\.?\s?s\.?|ltd\.?|gmbh|inc\.?|llc)$/i;

/** Klíč pro porovnávání názvů: bez diakritiky, právní formy, www a interpunkce. */
export function normalizeName(name) {
  let s = String(name ?? '').trim();
  let previous;
  do {
    previous = s;
    s = s.replace(LEGAL_SUFFIX, '').trim();
  } while (s !== previous);
  return s
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/^(https?:\/\/)?(www\.)?/, '')
    .replace(/[^a-z0-9.]+/g, ' ')
    .replace(/\.(?=\s|$)/g, '')
    .trim();
}

/** Doména z URL nebo holého názvu hostitele, bez www. Prázdný řetězec, když to doména není. */
export function domainOf(urlOrHost) {
  const raw = String(urlOrHost ?? '').trim();
  if (!raw) return '';
  let host = raw;
  try {
    host = new URL(raw.includes('://') ? raw : `https://${raw}`).hostname;
  } catch {
    return '';
  }
  host = host.toLowerCase().replace(/^www\./, '');
  return /^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(host) ? host : '';
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Najde alias jako celé slovo (ne uvnitř jiného slova). */
function containsWhole(haystack, needle) {
  if (!needle) return false;
  return new RegExp(`(^|[^a-z0-9])${escapeRegExp(needle)}([^a-z0-9]|$)`).test(haystack);
}

export function brandKeys(brand) {
  const keys = [brand.name, ...(brand.aliases ?? [])].map(normalizeName).filter(Boolean);
  const domain = domainOf(brand.domain);
  if (domain) keys.push(domain);
  return [...new Set(keys)];
}

export function isOwnBrand(name, brand) {
  const key = normalizeName(name);
  if (!key) return false;
  return brandKeys(brand).some((k) => key === k || containsWhole(key, k));
}

/**
 * Vyhodnotí jednu odpověď: je značka zmíněna, na kolikátém místě mezi
 * jmenovanými firmami a jestli AI cituje její web.
 */
export function analyzeAnswer({ text, sources = [], brands = [] }, brand) {
  const keys = brandKeys(brand);
  const normText = normalizeName(text);
  const inText = keys.some((k) => containsWhole(normText, k));
  const index = brands.findIndex((b) => isOwnBrand(b, brand));
  const ownDomain = domainOf(brand.domain);
  const cited =
    Boolean(ownDomain) &&
    sources.some((s) => s.domain === ownDomain || s.domain.endsWith(`.${ownDomain}`));
  return {
    mentioned: inText || index >= 0,
    position: index >= 0 ? index + 1 : null,
    cited,
  };
}

/** Odstraní duplicity ve výčtu firem, zachová pořadí prvního výskytu. */
export function uniqueBrands(names) {
  const seen = new Set();
  const out = [];
  for (const name of names ?? []) {
    const clean = String(name ?? '').replace(/\s+/g, ' ').trim();
    const key = normalizeName(clean);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(clean);
  }
  return out;
}

/** Vytáhne pole řetězců z odpovědi modelu, i když kolem JSONu něco přidal. */
export function parseBrandList(raw) {
  const text = String(raw ?? '');
  const start = text.indexOf('[');
  const end = text.lastIndexOf(']');
  if (start < 0 || end <= start) return [];
  try {
    const parsed = JSON.parse(text.slice(start, end + 1));
    return Array.isArray(parsed) ? uniqueBrands(parsed.filter((x) => typeof x === 'string')) : [];
  } catch {
    return [];
  }
}

const pct = (part, whole) => (whole ? Math.round((part / whole) * 100) : 0);
const round1 = (n) => Math.round(n * 10) / 10;

function statsRow(date, engine, answers) {
  const ok = answers.filter((a) => !a.error);
  const mentioned = ok.filter((a) => a.mentioned);
  const positions = mentioned.map((a) => a.position).filter((p) => p != null);
  return {
    date,
    engine,
    engine_label: engine === 'all' ? 'Celkem' : ENGINE_LABELS[engine] ?? engine,
    answers: ok.length,
    mentioned: mentioned.length,
    visibility: pct(mentioned.length, ok.length),
    cited: ok.filter((a) => a.cited).length,
    citation_rate: pct(ok.filter((a) => a.cited).length, ok.length),
    avg_position: positions.length ? round1(positions.reduce((a, b) => a + b, 0) / positions.length) : null,
  };
}

/** Řádky vývoje: za každé měření jeden souhrnný řádek a jeden za každý nástroj. */
export function runRows(run) {
  const engines = ENGINE_IDS.filter((e) => run.answers.some((a) => a.engine === e && !a.error));
  return [
    statsRow(run.date, 'all', run.answers),
    ...engines.map((e) => statsRow(run.date, e, run.answers.filter((a) => a.engine === e))),
  ];
}

function cellFor(answers) {
  const ok = answers.filter((a) => !a.error);
  if (!ok.length) return null;
  const hits = ok.filter((a) => a.mentioned);
  if (!hits.length) return 'ne';
  const positions = hits.map((a) => a.position).filter((p) => p != null);
  return positions.length ? `${Math.min(...positions)}.` : 'ano';
}

/** Tabulka dotazů: řádek na dotaz, sloupec na nástroj, plus kdo je jmenován místo vás. */
export function queryRows(run, brand) {
  const order = [];
  const byQuery = new Map();
  for (const a of run.answers) {
    if (!byQuery.has(a.query)) {
      byQuery.set(a.query, []);
      order.push(a.query);
    }
    byQuery.get(a.query).push(a);
  }
  return order.map((query) => {
    const answers = byQuery.get(query);
    const row = { query, topic: answers[0].topic ?? '' };
    let measured = 0;
    let hits = 0;
    for (const engine of ENGINE_IDS) {
      const cell = cellFor(answers.filter((a) => a.engine === engine));
      row[engine] = cell;
      if (cell != null) measured += 1;
      if (cell != null && cell !== 'ne') hits += 1;
    }
    const rivals = new Map();
    for (const a of answers) {
      for (const b of a.brands ?? []) {
        if (isOwnBrand(b, brand)) continue;
        const key = normalizeName(b);
        const entry = rivals.get(key) ?? { name: b, count: 0 };
        entry.count += 1;
        rivals.set(key, entry);
      }
    }
    row.hits = hits;
    row.engines = measured;
    row.rivals = [...rivals.values()]
      .sort((a, b) => b.count - a.count)
      .slice(0, 3)
      .map((r) => r.name)
      .join(', ');
    return row;
  });
}

/** Podíl hlasu: v kolika procentech odpovědí je která firma jmenována. */
export function brandRows(run, brand, limit = 10) {
  const ok = run.answers.filter((a) => !a.error);
  const counts = new Map();
  for (const a of ok) {
    for (const b of uniqueBrands(a.brands)) {
      if (isOwnBrand(b, brand)) continue;
      const key = normalizeName(b);
      const entry = counts.get(key) ?? { brand: b, mentions: 0 };
      entry.mentions += 1;
      counts.set(key, entry);
    }
  }
  const own = { brand: brand.name, mentions: ok.filter((a) => a.mentioned).length, own: true };
  const others = [...counts.values()]
    .sort((a, b) => b.mentions - a.mentions || a.brand.localeCompare(b.brand, 'cs'))
    .slice(0, limit)
    .map((r) => ({ ...r, own: false }));
  return [own, ...others]
    .sort((a, b) => b.mentions - a.mentions || Number(b.own) - Number(a.own))
    .map((r) => ({ brand: r.brand, mentions: r.mentions, share: pct(r.mentions, ok.length), own: r.own }));
}

/** Weby, které AI nejčastěji uvádí jako zdroj. */
export function domainRows(run, brand, limit = 12) {
  const ok = run.answers.filter((a) => !a.error);
  const ownDomain = domainOf(brand.domain);
  const counts = new Map();
  for (const a of ok) {
    for (const d of new Set((a.sources ?? []).map((s) => s.domain).filter(Boolean))) {
      counts.set(d, (counts.get(d) ?? 0) + 1);
    }
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, limit)
    .map(([domain, citations]) => ({
      domain,
      citations,
      share: pct(citations, ok.length),
      own: Boolean(ownDomain) && (domain === ownDomain || domain.endsWith(`.${ownDomain}`)),
    }));
}

/** Viditelnost podle tématu dotazu v jednom měření. */
export function topicRows(run) {
  const ok = run.answers.filter((a) => !a.error && a.topic);
  return [...new Set(ok.map((a) => a.topic))].map((topic) => {
    const answers = ok.filter((a) => a.topic === topic);
    const mentioned = answers.filter((a) => a.mentioned).length;
    return { topic, answers: answers.length, mentioned, visibility: pct(mentioned, answers.length) };
  });
}

/** Co která AI na který dotaz odpověděla: zkrácený text, jmenované firmy a zdroje. */
export function detailRows(run, maxChars = 1200) {
  return run.answers
    .filter((a) => !a.error)
    .map((a) => {
      const text = String(a.text ?? '').trim();
      return {
        query: a.query,
        engine: a.engine,
        mentioned: Boolean(a.mentioned),
        position: a.position ?? null,
        text: text.length > maxChars ? `${text.slice(0, maxChars).trimEnd()}…` : text,
        brands: uniqueBrands(a.brands).slice(0, 8),
        sources: [...new Set((a.sources ?? []).map((s) => s.domain).filter(Boolean))].slice(0, 6),
      };
    });
}

/** Jeden soubor pro dashboard: vývoj ze všech měření, detail z posledního. */
export function buildDashboard(runs, brand) {
  // Měření bez hledání na webu se s ostatními nemíchají: jakmile existuje měření s hledáním, vypadnou.
  const withSearch = runs.filter((r) => !r.noSearch);
  const sorted = [...(withSearch.length ? withSearch : runs)].sort((a, b) => a.date.localeCompare(b.date));
  const latest = sorted.at(-1);
  return {
    meta: [
      {
        brand: brand.name,
        domain: domainOf(brand.domain),
        updated: latest?.date ?? null,
        runs: sorted.length,
        queries: latest ? new Set(latest.answers.map((a) => a.query)).size : 0,
        sample: sorted.some((r) => r.sample === true),
        noSearch: latest?.noSearch === true,
      },
    ],
    runs: sorted.flatMap(runRows),
    queries: latest ? queryRows(latest, brand) : [],
    brands: latest ? brandRows(latest, brand) : [],
    domains: latest ? domainRows(latest, brand) : [],
    topics: latest ? topicRows(latest) : [],
    details: latest ? detailRows(latest) : [],
  };
}
