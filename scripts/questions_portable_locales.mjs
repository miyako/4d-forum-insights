#!/usr/bin/env node
// Stage public-shaped locale overlays privately for separate review.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { privateDirectory } from './questions_translate_review.mjs';
import { validatePublicPreview } from './questions_public_preview.mjs';
import { coverage, recordsFromColumns, validateOverlay, validateShape } from './localization.mjs';

export function stagePortableLocales({ preview, store, output }) {
  const source = privateDirectory(store), target = privateDirectory(output);
  if (source === target || source.startsWith(target + sep) || target.startsWith(source + sep)) {
    throw Error('Locale source and output directories must not overlap');
  }
  const data = validatePublicPreview(JSON.parse(readFileSync(preview)));
  if (!data.corpus_complete) throw Error('A complete Questions preview is required');
  const records = recordsFromColumns('questions', {
    ids: data.entries.map(row => row.id), title: data.entries.map(row => row.title),
    body: data.entries.map(row => row.body ?? ''),
  });
  const staged = {};
  for (const locale of ['fr', 'ja']) {
    const overlay = validateOverlay(JSON.parse(readFileSync(join(source, locale, 'questions.json'))), 'questions', locale);
    if (coverage(records, overlay).current !== records.length) throw Error(`Incomplete ${locale} Questions translations`);
    const entries = Object.fromEntries(records.map(record => {
      const entry = overlay.entries[record.id];
      validateShape('questions', entry.fields, record.fields);
      return [record.id, { source_hash: entry.source_hash, translation_version: entry.translation_version, fields: entry.fields }];
    }));
    const directory = privateDirectory(join(target, locale));
    if (directory !== join(target, locale)) throw Error('Locale output directories cannot be symlinks');
    const bytes = Buffer.from(JSON.stringify({ version: 1, locale, page: 'questions', entries }) + '\n');
    staged[locale] = { path: join(directory, 'questions.json'), bytes };
  }
  for (const { path, bytes } of Object.values(staged)) writeFileSync(path, bytes, { flag: 'wx', mode: 0o600 });
  return { fr_sha256: createHash('sha256').update(staged.fr.bytes).digest('hex'),
    ja_sha256: createHash('sha256').update(staged.ja.bytes).digest('hex'),
    processed_count: records.length, translation_review_required: true, publication_performed: false };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const { values } = parseArgs({ options: {
    preview: { type: 'string' }, store: { type: 'string' }, output: { type: 'string' },
  } });
  if (!values.preview || !values.store || !values.output) throw Error('Preview, store and output are required');
  process.umask(0o077);
  console.log(JSON.stringify(stagePortableLocales(values)));
}
