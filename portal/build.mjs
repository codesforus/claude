#!/usr/bin/env node
// Složí portál: do šablony vloží data z měření a zapíše portal/index.html.
//
//   node portal/build.mjs                         z data/dashboard.json
//   node portal/build.mjs sample/dashboard.json   z ukázkových dat

import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const dataFile = process.argv[2] ?? 'data/dashboard.json';

const data = JSON.parse(await readFile(dataFile, 'utf8'));
const template = await readFile(path.join(here, 'template.html'), 'utf8');
if (!template.includes('__DATA__')) throw new Error('V šabloně chybí značka __DATA__.');

// "<" se zapíše jako <, aby text z odpovědí AI nemohl ukončit značku <script>.
const json = JSON.stringify(data).replace(/</g, '\\u003c');
const page = template.replace('__DATA__', () => json);
const out = path.join(here, 'index.html');
await writeFile(out, page);

// Samostatná verze pro vlastní hosting (Netlify apod.): celý dokument včetně hlavičky.
// Verze výše je bez ní, protože ji při publikování jako artifact doplňuje claude.ai.
const head = `<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>
`;
const site = path.join(here, '..', 'site');
await mkdir(site, { recursive: true });
await writeFile(path.join(site, 'index.html'), `${head}${page}\n</html>\n`);
console.log(`Portál: ${out} a site/index.html (${data.meta?.[0]?.runs ?? 0} měření, zdroj ${dataFile})`);
