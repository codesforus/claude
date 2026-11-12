#!/usr/bin/env node
// Jedno měření: položí všechny dotazy všem zapnutým AI nástrojům, vyhodnotí
// odpovědi a přepočítá soubor pro dashboard.
//
//   node tracker/run.mjs                       ostré měření podle config.json
//   node tracker/run.mjs --mock --weeks 8      ukázková data (bez sítě a klíčů)
//   node tracker/run.mjs --rebuild             jen přepočítá dashboard z uložených měření

import { spawnSync } from 'node:child_process';
import { mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { parseArgs } from 'node:util';

import { createEngines, extractBrands } from './engines.mjs';
import { analyzeAnswer, buildDashboard } from './lib.mjs';

// Vestavěný fetch v Node čte HTTPS_PROXY jen s tímto přepínačem.
if ((process.env.HTTPS_PROXY || process.env.https_proxy) && !process.env.NODE_USE_ENV_PROXY) {
  const child = spawnSync(process.execPath, process.argv.slice(1), {
    stdio: 'inherit',
    env: { ...process.env, NODE_USE_ENV_PROXY: '1' },
  });
  process.exit(child.status ?? 1);
}

const { values: args } = parseArgs({
  options: {
    config: { type: 'string', default: 'config.json' },
    out: { type: 'string', default: 'data' },
    date: { type: 'string' },
    mock: { type: 'boolean', default: false },
    weeks: { type: 'string', default: '1' },
    rebuild: { type: 'boolean', default: false },
    limit: { type: 'string' },
    concurrency: { type: 'string', default: '4' },
    pause: { type: 'string', default: '0' },
  },
});

const today = () => new Date().toISOString().slice(0, 10);

async function pool(items, size, worker) {
  const results = new Array(items.length);
  let next = 0;
  await Promise.all(
    Array.from({ length: Math.min(size, items.length) }, async () => {
      while (next < items.length) {
        const i = next;
        next += 1;
        results[i] = await worker(items[i], i);
      }
    }),
  );
  return results;
}

async function measure(config, date, { mockRun }) {
  const engines = createEngines(config, { mockRun: mockRun ? { date } : null });
  if (!engines.length) throw new Error('V config.json není zapnutý žádný AI nástroj.');
  const queries = (config.queries ?? [])
    .map((q) => (typeof q === 'string' ? { text: q } : q))
    .slice(0, args.limit ? Number(args.limit) : undefined);
  if (!queries.length) throw new Error('V config.json nejsou žádné dotazy.');

  const models = {};
  for (const engine of engines) models[engine.id] = await engine.model();
  const extractor = engines[0];

  const jobs = [];
  for (const query of queries) {
    for (const engine of engines) {
      for (let sample = 1; sample <= (config.samples ?? 1); sample += 1) jobs.push({ query, engine, sample });
    }
  }

  let done = 0;
  const answers = await pool(jobs, Number(args.concurrency), async ({ query, engine, sample }) => {
    const base = { query: query.text, topic: query.topic ?? '', engine: engine.id, sample };
    let answer;
    if (Number(args.pause)) await new Promise((r) => setTimeout(r, Number(args.pause) * 1000));
    try {
      const { text, sources } = await engine.ask(query.text);
      if (!text.trim()) throw new Error('prázdná odpověď');
      const brands = await extractBrands(extractor, text);
      answer = { ...base, text, sources, brands, ...analyzeAnswer({ text, sources, brands }, config.brand) };
    } catch (error) {
      answer = { ...base, error: String(error.message ?? error) };
    }
    done += 1;
    if (!mockRun) {
      const mark = answer.error ? `chyba: ${answer.error}` : answer.mentioned ? 'zmíněni' : 'bez zmínky';
      console.log(`[${done}/${jobs.length}] ${engine.id} · ${query.text} → ${mark}`);
    }
    return answer;
  });

  const noSearch = engines.some((e) => e.search === false) || undefined;
  return { date, brand: config.brand.name, sample: mockRun || undefined, noSearch, models, answers };
}

async function loadRuns(dir) {
  const files = (await readdir(dir).catch(() => [])).filter((f) => f.endsWith('.json')).sort();
  return Promise.all(files.map(async (f) => JSON.parse(await readFile(path.join(dir, f), 'utf8'))));
}

const config = JSON.parse(await readFile(args.config, 'utf8'));
const runsDir = path.join(args.out, 'runs');
await mkdir(runsDir, { recursive: true });

if (!args.rebuild) {
  const lastDate = args.date ?? today();
  const weeks = args.mock ? Number(args.weeks) : 1;
  for (let w = weeks - 1; w >= 0; w -= 1) {
    const date = new Date(Date.parse(lastDate) - w * 7 * 864e5).toISOString().slice(0, 10);
    const run = await measure(config, date, { mockRun: args.mock });
    const failed = run.answers.filter((a) => a.error);
    if (failed.length === run.answers.length) {
      throw new Error(`Žádná odpověď se nepovedla. První chyba: ${failed[0].error}`);
    }
    await writeFile(path.join(runsDir, `${date}.json`), `${JSON.stringify(run, null, 1)}\n`);
    const ok = run.answers.length - failed.length;
    const hits = run.answers.filter((a) => a.mentioned).length;
    console.log(`${date}: ${ok} odpovědí, zmínka v ${hits} (${ok ? Math.round((hits / ok) * 100) : 0} %), chyb ${failed.length}`);
  }
}

const dashboard = buildDashboard(await loadRuns(runsDir), config.brand);
const dashboardFile = path.join(args.out, 'dashboard.json');
await writeFile(dashboardFile, `${JSON.stringify(dashboard, null, 1)}\n`);
console.log(`Soubor pro dashboard: ${dashboardFile} (${dashboard.meta[0].runs} měření)`);
