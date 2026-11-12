import assert from 'node:assert/strict';
import test from 'node:test';

import { pickNewest } from '../tracker/engines.mjs';
import {
  analyzeAnswer,
  brandRows,
  buildDashboard,
  detailRows,
  domainOf,
  isOwnBrand,
  normalizeName,
  parseBrandList,
  queryRows,
  runRows,
  topicRows,
} from '../tracker/lib.mjs';

const brand = { name: 'Okna Novák', domain: 'https://www.oknanovak.cz/', aliases: ['Novák okna s.r.o.'] };

test('normalizeName strips diacritics, legal forms and www', () => {
  assert.equal(normalizeName('  Okna Novák, s.r.o. '), 'okna novak');
  assert.equal(normalizeName('OKNA NOVÁK a.s.'), 'okna novak');
  assert.equal(normalizeName('www.OknaNovak.cz'), 'oknanovak.cz');
  assert.equal(normalizeName('Žaluzie & Rolety spol. s r.o.'), 'zaluzie rolety');
});

test('domainOf handles URLs, bare hosts and junk', () => {
  assert.equal(domainOf('https://www.Example.cz/path?x=1'), 'example.cz');
  assert.equal(domainOf('blog.example.cz'), 'blog.example.cz');
  assert.equal(domainOf('Nějaký titulek stránky'), '');
  assert.equal(domainOf(''), '');
});

test('isOwnBrand matches name, alias and domain but not a longer word', () => {
  assert.ok(isOwnBrand('Okna Novák s.r.o.', brand));
  assert.ok(isOwnBrand('novák okna', brand));
  assert.ok(isOwnBrand('OknaNovak.cz', brand));
  assert.ok(!isOwnBrand('Okna Nováková', brand));
  assert.ok(!isOwnBrand('Okna Macek', brand));
});

test('analyzeAnswer reports mention, position and citation', () => {
  const hit = analyzeAnswer(
    {
      text: 'Doporučuji Okna Macek nebo firmu Okna Novák.',
      sources: [{ domain: 'oknanovak.cz' }, { domain: 'srovnavac.cz' }],
      brands: ['Okna Macek', 'Okna Novák'],
    },
    brand,
  );
  assert.deepEqual(hit, { mentioned: true, position: 2, cited: true });

  const textOnly = analyzeAnswer({ text: 'Zkuste oknanovak.cz.', sources: [], brands: [] }, brand);
  assert.deepEqual(textOnly, { mentioned: true, position: null, cited: false });

  const miss = analyzeAnswer(
    { text: 'Doporučuji Okna Macek.', sources: [{ domain: 'macek.cz' }], brands: ['Okna Macek'] },
    brand,
  );
  assert.deepEqual(miss, { mentioned: false, position: null, cited: false });
});

test('parseBrandList tolerates text around the JSON and drops duplicates', () => {
  assert.deepEqual(parseBrandList('Tady je seznam: ["Alfa", "alfa s.r.o.", "Beta", 3]\nHotovo.'), ['Alfa', 'Beta']);
  assert.deepEqual(parseBrandList('nic'), []);
  assert.deepEqual(parseBrandList('[rozbité'), []);
});

test('pickNewest compares versions numerically', () => {
  const ids = ['gpt-4.1-mini', 'gpt-5-mini', 'gpt-5.10-mini', 'gpt-5.2-mini', 'gpt-5.2-mini-2026-01-01'];
  assert.equal(pickNewest(ids, /^gpt-(\d+(?:\.\d+)?)-mini$/), 'gpt-5.10-mini');
  assert.equal(pickNewest(['o3'], /^gpt-(\d+(?:\.\d+)?)-mini$/), null);
});

const run = {
  date: '2026-11-12',
  answers: [
    { query: 'A', engine: 'chatgpt', mentioned: true, position: 2, cited: true, brands: ['Macek', 'Okna Novák'], sources: [{ domain: 'oknanovak.cz' }] },
    { query: 'A', engine: 'gemini', mentioned: false, position: null, cited: false, brands: ['Macek', 'Svoboda'], sources: [{ domain: 'srovnavac.cz' }] },
    { query: 'B', engine: 'chatgpt', mentioned: false, position: null, cited: false, brands: ['Macek'], sources: [{ domain: 'srovnavac.cz' }] },
    { query: 'B', engine: 'gemini', error: 'timeout' },
  ],
};

test('runRows ignores failed answers and splits by engine', () => {
  const rows = runRows(run);
  assert.deepEqual(rows.map((r) => r.engine), ['all', 'chatgpt', 'gemini']);
  assert.deepEqual(
    { answers: rows[0].answers, visibility: rows[0].visibility, citation_rate: rows[0].citation_rate, avg_position: rows[0].avg_position },
    { answers: 3, visibility: 33, citation_rate: 33, avg_position: 2 },
  );
  assert.equal(rows[1].visibility, 50);
  assert.equal(rows[2].visibility, 0);
  assert.equal(rows[2].avg_position, null);
});

test('queryRows builds one row per query with a cell per engine', () => {
  const rows = queryRows(run, brand);
  assert.deepEqual(rows[0], { query: 'A', topic: '', chatgpt: '2.', gemini: 'ne', perplexity: null, hits: 1, engines: 2, rivals: 'Macek, Svoboda' });
  assert.equal(rows[1].gemini, null);
  assert.equal(rows[1].engines, 1);
});

test('brandRows always includes the own brand and ranks by mentions', () => {
  const rows = brandRows(run, brand);
  assert.deepEqual(rows[0], { brand: 'Macek', mentions: 3, share: 100, own: false });
  assert.deepEqual(rows.find((r) => r.own), { brand: 'Okna Novák', mentions: 1, share: 33, own: true });
});

test('buildDashboard takes detail from the latest run', () => {
  const older = { date: '2026-11-05', answers: [{ query: 'A', engine: 'chatgpt', mentioned: false, position: null, cited: false, brands: [], sources: [] }] };
  const dash = buildDashboard([run, older], brand);
  assert.equal(dash.meta[0].updated, '2026-11-12');
  assert.equal(dash.meta[0].runs, 2);
  assert.deepEqual(dash.runs.map((r) => r.date), ['2026-11-05', '2026-11-05', '2026-11-12', '2026-11-12', '2026-11-12']);
  assert.equal(dash.domains[0].domain, 'srovnavac.cz');
  assert.equal(dash.domains.find((d) => d.own).domain, 'oknanovak.cz');
});

test('topicRows and detailRows describe the latest run', () => {
  const tagged = {
    date: '2026-11-12',
    answers: [
      { query: 'A', topic: 'Okna', engine: 'chatgpt', mentioned: true, position: 1, text: 'x'.repeat(50), brands: ['Okna Novák', 'okna novák'], sources: [{ domain: 'a.cz' }, { domain: 'a.cz' }] },
      { query: 'B', topic: 'Okna', engine: 'chatgpt', mentioned: false, position: null, text: 'krátká', brands: [], sources: [] },
      { query: 'C', topic: 'Dveře', engine: 'chatgpt', error: 'timeout' },
    ],
  };
  assert.deepEqual(topicRows(tagged), [{ topic: 'Okna', answers: 2, mentioned: 1, visibility: 50 }]);
  const details = detailRows(tagged, 20);
  assert.equal(details.length, 2);
  assert.equal(details[0].text, `${'x'.repeat(20)}…`);
  assert.deepEqual(details[0].brands, ['Okna Novák']);
  assert.deepEqual(details[0].sources, ['a.cz']);
  assert.equal(details[1].text, 'krátká');
});

test('buildDashboard drops no-search runs once a run with search exists', () => {
  const answer = { query: 'A', engine: 'gemini', mentioned: false, position: null, cited: false, brands: [], sources: [] };
  const plain = { date: '2026-11-12', noSearch: true, answers: [answer] };
  assert.equal(buildDashboard([plain], brand).meta[0].noSearch, true);
  const mixed = buildDashboard([plain, { date: '2026-11-19', answers: [answer] }], brand);
  assert.equal(mixed.meta[0].runs, 1);
  assert.equal(mixed.meta[0].noSearch, false);
});
