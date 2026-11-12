#!/usr/bin/env node
// Složí portál: do šablony vloží data z měření a zapíše portal/index.html.
//
//   node portal/build.mjs                         z data/dashboard.json
//   node portal/build.mjs sample/dashboard.json   z ukázkových dat

import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const dataFile = process.argv[2] ?? 'data/dashboard.json';

const data = JSON.parse(await readFile(dataFile, 'utf8'));
const template = await readFile(path.join(here, 'template.html'), 'utf8');
if (!template.includes('__DATA__')) throw new Error('V šabloně chybí značka __DATA__.');

// "<" se zapíše jako <, aby text z odpovědí AI nemohl ukončit značku <script>.
const json = JSON.stringify(data).replace(/</g, '\\u003c');
const out = path.join(here, 'index.html');
await writeFile(out, template.replace('__DATA__', () => json));
console.log(`Portál: ${out} (${data.meta?.[0]?.runs ?? 0} měření, zdroj ${dataFile})`);
