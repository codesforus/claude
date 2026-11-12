// Napojení na jednotlivé AI nástroje. Každý umí `ask` (odpověď s hledáním na webu)
// a `complete` (obyčejné doplnění textu, používá se na výčet firem z odpovědi).

import { domainOf, parseBrandList } from './lib.mjs';

// V cloudové relaci klíč do hlavičky doplní síťová proxy; skript ho nikdy nevidí.
const PLACEHOLDER_KEY = 'doplni-proxy';

async function http(url, { method = 'POST', headers = {}, body, timeoutMs = 180_000, tries = 3 } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= tries; attempt += 1) {
    try {
      const res = await fetch(url, {
        method,
        headers: { 'content-type': 'application/json', ...headers },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
      });
      const text = await res.text();
      if (res.ok) return text ? JSON.parse(text) : {};
      const error = new Error(`${new URL(url).hostname} vrátil ${res.status}: ${text.slice(0, 300)}`);
      error.status = res.status;
      if (res.status !== 429 && res.status < 500) throw error;
      lastError = error;
    } catch (error) {
      if (error.status && error.status !== 429 && error.status < 500) throw error;
      lastError = error;
    }
    if (attempt < tries) await new Promise((r) => setTimeout(r, 2000 * 2 ** (attempt - 1)));
  }
  throw lastError;
}

function toSources(items) {
  const seen = new Set();
  const out = [];
  for (const { url, title } of items) {
    const domain = domainOf(url) || domainOf(title);
    if (!domain || seen.has(`${domain}|${url}`)) continue;
    seen.add(`${domain}|${url}`);
    out.push({ url: url ?? '', title: title ?? '', domain });
  }
  return out;
}

/** Vybere nejnovější model, jehož název odpovídá vzoru; verze se porovnávají číselně. */
export function pickNewest(ids, pattern) {
  const versions = ids
    .map((id) => ({ id, m: id.match(pattern) }))
    .filter((x) => x.m)
    .map((x) => ({ id: x.id, v: x.m[1].split('.').map(Number) }));
  versions.sort((a, b) => b.v[0] - a.v[0] || (b.v[1] ?? 0) - (a.v[1] ?? 0));
  return versions[0]?.id ?? null;
}

function openai(cfg, locale) {
  const base = 'https://api.openai.com/v1';
  const headers = { authorization: `Bearer ${process.env.OPENAI_API_KEY || PLACEHOLDER_KEY}` };
  let model = cfg.model || null;
  const resolve = async () => {
    if (!model) {
      const list = await http(`${base}/models`, { method: 'GET', headers });
      const ids = (list.data ?? []).map((m) => m.id);
      model = pickNewest(ids, /^gpt-(\d+(?:\.\d+)?)-mini$/) ?? pickNewest(ids, /^gpt-(\d+(?:\.\d+)?)$/);
      if (!model) throw new Error('OpenAI: nenašel jsem žádný použitelný model, nastavte ho v config.json.');
    }
    return model;
  };
  const textOf = (res) =>
    (res.output ?? [])
      .filter((item) => item.type === 'message')
      .flatMap((item) => item.content ?? [])
      .filter((part) => part.type === 'output_text');
  return {
    id: 'chatgpt',
    model: resolve,
    async ask(query) {
      const location = { type: 'approximate', country: locale.country };
      if (locale.city) location.city = locale.city;
      const res = await http(`${base}/responses`, {
        headers,
        body: { model: await resolve(), input: query, tools: [{ type: 'web_search', user_location: location }] },
      });
      const parts = textOf(res);
      return {
        text: parts.map((p) => p.text).join('\n'),
        sources: toSources(
          parts.flatMap((p) => p.annotations ?? []).filter((a) => a.type === 'url_citation'),
        ),
      };
    },
    async complete(prompt) {
      const res = await http(`${base}/responses`, { headers, body: { model: await resolve(), input: prompt } });
      return textOf(res).map((p) => p.text).join('\n');
    },
  };
}

function gemini(cfg) {
  const base = 'https://generativelanguage.googleapis.com/v1beta';
  const headers = { 'x-goog-api-key': process.env.GEMINI_API_KEY || PLACEHOLDER_KEY };
  let model = cfg.model || null;
  const resolve = async () => {
    if (!model) {
      const list = await http(`${base}/models?pageSize=1000`, { method: 'GET', headers });
      const ids = (list.models ?? [])
        .filter((m) => (m.supportedGenerationMethods ?? []).includes('generateContent'))
        .map((m) => m.name.replace(/^models\//, ''));
      model = pickNewest(ids, /^gemini-(\d+(?:\.\d+)?)-flash$/);
      if (!model) throw new Error('Gemini: nenašel jsem žádný použitelný model, nastavte ho v config.json.');
    }
    return model;
  };
  const call = async (prompt, tools) => {
    const res = await http(`${base}/models/${await resolve()}:generateContent`, {
      headers,
      body: { contents: [{ role: 'user', parts: [{ text: prompt }] }], ...(tools ? { tools } : {}) },
    });
    const candidate = res.candidates?.[0] ?? {};
    return {
      text: (candidate.content?.parts ?? []).map((p) => p.text ?? '').join(''),
      chunks: candidate.groundingMetadata?.groundingChunks ?? [],
    };
  };
  return {
    id: 'gemini',
    model: resolve,
    async ask(query) {
      const { text, chunks } = await call(query, [{ google_search: {} }]);
      // Odkazy od Googlu jsou přesměrování; skutečnou doménu nese titulek zdroje.
      return {
        text,
        sources: toSources(
          chunks
            .filter((c) => c.web)
            .map((c) => ({ url: domainOf(c.web.title) ? `https://${domainOf(c.web.title)}` : c.web.uri, title: c.web.title })),
        ),
      };
    },
    async complete(prompt) {
      return (await call(prompt)).text;
    },
  };
}

function perplexity(cfg, locale) {
  const url = 'https://api.perplexity.ai/chat/completions';
  const headers = { authorization: `Bearer ${process.env.PERPLEXITY_API_KEY || PLACEHOLDER_KEY}` };
  const model = cfg.model || 'sonar';
  const call = (prompt, extra = {}) =>
    http(url, { headers, body: { model, messages: [{ role: 'user', content: prompt }], ...extra } });
  return {
    id: 'perplexity',
    model: async () => model,
    async ask(query) {
      const res = await call(query, { web_search_options: { user_location: { country: locale.country } } });
      const message = res.choices?.[0]?.message ?? {};
      const results = res.search_results ?? message.search_results ?? [];
      const citations = res.citations ?? message.citations ?? [];
      return {
        text: message.content ?? '',
        sources: toSources(results.length ? results : citations.map((u) => ({ url: u }))),
      };
    },
    async complete(prompt) {
      return (await call(prompt)).choices?.[0]?.message?.content ?? '';
    },
  };
}

// --- Ukázkový nástroj: žádná síť, odpovědi jsou vymyšlené a opakovatelné. ---

function hash(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i += 1) h = Math.imul(h ^ str.charCodeAt(i), 16777619);
  return (h >>> 0) / 2 ** 32;
}

function mock(id, { brand, mock: m, date }) {
  const bias = { chatgpt: 0.0, gemini: 0.08, perplexity: -0.06 }[id] ?? 0;
  const weeks = (Date.parse(date) - Date.parse(m.start)) / (7 * 864e5);
  return {
    id,
    model: async () => 'ukazka',
    async ask(query) {
      const chance = Math.min(0.85, m.baseChance + m.weeklyGain * weeks + bias + (hash(`q|${query}`) - 0.5) * 0.5);
      const rivals = [...m.rivals]
        .sort((a, b) => hash(`${a.name}|${query}|${id}|${date}`) - a.weight - (hash(`${b.name}|${query}|${id}|${date}`) - b.weight))
        .slice(0, 3 + Math.floor(hash(`n|${query}|${id}`) * 3))
        .map((r) => r.name);
      const names = [...rivals];
      if (hash(`own|${query}|${id}|${date}`) < chance) {
        names.splice(Math.floor(hash(`pos|${query}|${id}|${date}`) * Math.min(3, names.length)), 0, brand.name);
      }
      const sourcePool = [...m.sources];
      if (names.includes(brand.name) && hash(`cite|${query}|${id}|${date}`) < 0.55) sourcePool.unshift(brand.domain);
      return {
        text: `Ukázková odpověď na dotaz „${query}“:\n${names.map((n, i) => `${i + 1}. **${n}**`).join('\n')}`,
        sources: toSources(
          sourcePool
            .filter((d, i) => i === 0 || hash(`${d}|${query}|${id}|${date}`) < 0.45)
            .slice(0, 5)
            .map((d) => ({ url: `https://${d}` })),
        ),
      };
    },
    async complete(prompt) {
      return JSON.stringify([...prompt.matchAll(/\*\*(.+?)\*\*/g)].map((x) => x[1]));
    },
  };
}

const FACTORIES = { chatgpt: openai, gemini, perplexity };

export function createEngines(config, { mockRun = null } = {}) {
  const locale = { country: config.country || 'CZ', city: config.city || null };
  return Object.entries(config.engines ?? {})
    .filter(([id, cfg]) => FACTORIES[id] && cfg?.enabled !== false)
    .map(([id, cfg]) =>
      mockRun ? mock(id, { brand: config.brand, mock: config.mock, date: mockRun.date }) : FACTORIES[id](cfg ?? {}, locale),
    );
}

const EXTRACT_PROMPT = `Níže je odpověď AI asistenta na dotaz zákazníka. Vypiš všechny konkrétní firmy, značky, e-shopy a poskytovatele služeb, které odpověď jmenuje nebo doporučuje, v pořadí prvního výskytu. Nevypisuj obecné pojmy, kategorie produktů, města ani weby, které jsou jen uvedené jako zdroj informací. Vrať pouze JSON pole řetězců, nic dalšího.

Odpověď:
"""
{{text}}
"""`;

/** Výčet firem jmenovaných v odpovědi, v pořadí prvního výskytu. */
export async function extractBrands(engine, text) {
  if (!text.trim()) return [];
  return parseBrandList(await engine.complete(EXTRACT_PROMPT.replace('{{text}}', text.slice(0, 12_000))));
}
