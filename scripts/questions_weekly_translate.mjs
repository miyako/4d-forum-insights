#!/usr/bin/env node
// Reuse exact-current reviewed overlays as validated string caches before missing-only translation.
import fs from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';
import { main as backfill, readTextCache, stringUnits } from './backfill-requests.mjs';
import { readSourceRecords, readOverlay, sourceHash, VERSION, REQUESTS_PROTECTION_VERSION, LOCALES } from './localization.mjs';

export function seedCurrentOverlays(data, store, locales = LOCALES) {
  const records = readSourceRecords(data, 'questions'), units = stringUnits(records);
  let reused = 0;
  for (const locale of locales) {
    const file = path.join(store, 'text-cache', `${locale}.json`);
    const cache = readTextCache(file, locale, units);
    const overlay = readOverlay(store, 'questions', locale);
    for (const record of records) {
      const previous = overlay.entries[record.id];
      if (previous?.source_hash !== record.source_hash || previous.translation_version !== VERSION) continue;
      for (const [field, text] of Object.entries(record.fields)) {
        const hash = sourceHash(text);
        if (!text.trim() || cache.entries[hash]) continue;
        const candidate = {
          ...cache, entries: { ...cache.entries, [hash]: {
            source_hash: hash, translation_version: VERSION, protection_version: REQUESTS_PROTECTION_VERSION,
            text: previous.fields[field], reused_from: `questions/${record.id}/${field}`,
            legacy_prompt_unrecorded: true,
          } },
        };
        fs.mkdirSync(path.dirname(file), { recursive: true });
        const pending = `${file}.${process.pid}.seed.tmp`;
        try {
          fs.writeFileSync(pending, JSON.stringify(candidate), { mode: 0o600, flag: 'wx' });
          // Reuse the existing technical-token/multiplicity validator; no relaxed cache fallback.
          readTextCache(pending, locale, units);
          fs.renameSync(pending, file);
          cache.entries = candidate.entries;
          reused++;
        } catch (error) {
          console.error(`Cannot reuse current ${locale}/questions/${record.id}/${field}: ${error.message}`);
          throw error;
        } finally {
          if (fs.existsSync(pending)) fs.unlinkSync(pending);
        }
      }
    }
  }
  return reused;
}

export async function main(args = process.argv.slice(2), options = {}) {
  const { values } = parseArgs({ args, strict: false, options: {
    data: { type: 'string' }, store: { type: 'string' }, locale: { type: 'string', default: 'all' },
  } });
  if (!values.data || !values.store || (values.locale !== 'all' && !LOCALES.includes(values.locale))) {
    throw Error('Weekly translation requires explicit cleared data/store and supported locale');
  }
  const lock = `${path.resolve(values.store)}.weekly-seed.lock`;
  fs.mkdirSync(path.dirname(lock), { recursive: true });
  fs.mkdirSync(lock); // Preserve interrupted lock for explicit recovery, never steal it.
  try {
    const seedLock = `${path.resolve(values.store)}.lock`;
    fs.mkdirSync(seedLock); // Same atomic lock as direct backfill callers.
    let reused;
    try {
      reused = seedCurrentOverlays(values.data, values.store, values.locale === 'all' ? LOCALES : [values.locale]);
    } finally {
      fs.rmdirSync(seedLock);
    }
    console.log(`Questions: seeded ${reused} exact-current overlay strings; hosted requests remain missing-only`);
    return await backfill([...args, '--page', 'questions'], options);
  } finally {
    fs.rmdirSync(lock);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
