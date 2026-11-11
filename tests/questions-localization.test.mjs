import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { main as backfill, seedQuestionsCache, stringUnits } from '../scripts/backfill-requests.mjs';
import { sourceHash, readSourceRecords, readOverlay, writeOverlay, recordsFromColumns } from '../scripts/localization.mjs';
import { translatedFields, applyOverlay, sourceHash as browserHash } from '../site/js/locale.js';
import { stageLocalizedPreview } from '../scripts/questions_local_preview.mjs';

function fixture() {
  fs.mkdirSync('build', { recursive: true });
  const root = fs.mkdtempSync(path.resolve('build/questions-locale-test-'));
  const data = path.join(root, 'data'), store = path.join(root, 'localization');
  fs.mkdirSync(path.join(data, 'questions'), { recursive: true });
  fs.writeFileSync(path.join(data, 'manifest.json'), JSON.stringify({
    questions: [{ id: 'fr' }], questions_view: { files: { entries: 'questions/entries.json?current=1' } },
  }));
  const rows = [{ id: 1, title: 'Query selections', body: 'Find matching entities.', lang: 'fr' },
    { id: 2, title: 'Search records', body: 'Sort matching entities.', lang: 'ja' }];
  const save = () => fs.writeFileSync(path.join(data, 'questions/entries.json'), JSON.stringify({ version: 1, entries: rows }));
  save();
  return { root, data, store, rows, save, cleanup: () => fs.rmSync(root, { recursive: true }) };
}

test('Questions canonical display hashes are identical in cache/export/browser and independent of ratings/source language', async () => {
  const f = fixture();
  try {
    const records = readSourceRecords(f.data, 'questions');
    assert.deepEqual(records[0].fields, { title: f.rows[0].title, body: f.rows[0].body });
    assert.equal(records[0].source_hash, await browserHash(translatedFields('questions', f.rows[0])));
    f.rows[0].answers = { coverage: { choice: 'full' } };
    f.rows[0].lang = 'de'; f.save();
    assert.equal(readSourceRecords(f.data, 'questions')[0].source_hash, records[0].source_hash);
    f.rows[0].body = null; f.save();
    assert.equal(readSourceRecords(f.data, 'questions')[0].fields.body, '');
    assert.equal(readSourceRecords(f.data, 'questions')[0].source_hash,
      await browserHash(translatedFields('questions', f.rows[0])));
    const m = JSON.parse(fs.readFileSync(path.join(f.data, 'manifest.json')));
    m.questions_view.files.entries = '../outside.json';
    fs.writeFileSync(path.join(f.data, 'manifest.json'), JSON.stringify(m));
    assert.throws(() => readSourceRecords(f.data, 'questions'), /escapes/);
  } finally { f.cleanup(); }
});

test('Questions first run persists provenance; unchanged rebuild has zero calls; one changed field translates once per target locale', async () => {
  const f = fixture();
  try {
    let calls = 0;
    const translated = [];
    const translate = async (prompt) => {
      calls++;
      const batch = JSON.parse(prompt.split('INPUT JSON:\n')[1]);
      translated.push(...batch.units.map((unit) => unit.text));
      return { batch_id: batch.batch_id, units: batch.units.map((unit) => ({ id: unit.id, text: `Traduit ${unit.text}` })) };
    };
    const args = ['--page', 'questions', '--data', f.data, '--store', f.store, '--locale', 'all',
      '--model', 'gpt-6.1-sol', '--workers', '1', '--batch-size', '10', '--require-complete'];
    const first = await backfill(args, { translate });
    assert.equal(first.translated, 8);
    assert.equal(first.model_calls, 2);
    assert.equal(first.complete, true);
    const cache = JSON.parse(fs.readFileSync(path.join(f.store, 'text-cache/fr.json')));
    const originalBody = cache.entries[sourceHash(f.rows[0].body)];
    assert.equal(originalBody.model, 'gpt-6.1-sol');
    assert.equal(originalBody.prompt_version, 'forum-summary-units-v1');
    assert.match(originalBody.prompt_hash, /^[a-f0-9]{64}$/);
    const overlay = readOverlay(f.store, 'questions', 'fr');
    assert.equal(overlay.entries[1].field_sources.body.source_hash, sourceHash(f.rows[0].body));
    assert.equal(overlay.entries[1].field_sources.body.model, 'gpt-6.1-sol');
    calls = 0;
    const second = await backfill(args, { translate: async () => { throw Error('Unchanged rebuild must not call a model'); } });
    assert.equal(second.requested, 0);
    assert.equal(second.translated, 0);
    assert.equal(second.model_calls, 0);
    f.rows[0].title = 'Changed selection question'; f.save();
    translated.length = 0;
    const third = await backfill(args, { translate });
    assert.equal(third.requested, 2);
    assert.equal(third.translated, 2);
    assert.equal(calls, 2);
    assert.deepEqual(translated, ['Changed selection question', 'Changed selection question']);
    const changedCache = JSON.parse(fs.readFileSync(path.join(f.store, 'text-cache/fr.json')));
    assert.deepEqual(changedCache.entries[sourceHash(f.rows[0].body)], originalBody, 'unchanged body retains original translation/provenance');
    assert.deepEqual(readOverlay(f.store, 'questions', 'fr').entries[2], overlay.entries[2], 'unrelated topic remains byte-equivalent');
  } finally { f.cleanup(); }
});

test('existing current Requests/Tips fields seed Questions only on exact English content and valid source hashes', () => {
  const f = fixture();
  try {
    const seed = path.join(f.root, 'seed');
    fs.mkdirSync(path.join(f.data, 'tips'));
    const tips = { ids: [1], headline: [f.rows[0].title], body: [f.rows[0].body] };
    fs.writeFileSync(path.join(f.data, 'tips/entries.json'), JSON.stringify(tips));
    const [record] = recordsFromColumns('tips', tips);
    writeOverlay(seed, { version: 1, page: 'tips', locale: 'fr', entries: {
      1: { source_hash: record.source_hash, translation_version: '1', fields: { headline: 'Question en français', body: 'Résumé en français.' } },
    } });
    const units = stringUnits(readSourceRecords(f.data, 'questions'));
    const cache = { version: 1, locale: 'fr', translation_version: '1', protection_version: 2, entries: {} };
    assert.equal(seedQuestionsCache(cache, units, { data: f.data, store: seed, provenance: { model: 'gpt-6.1-sol' } }), 2);
    assert.equal(cache.entries[sourceHash(f.rows[0].title)].text, 'Question en français');
    assert.equal(cache.entries[sourceHash(f.rows[0].title)].reused_from, 'tips/1/headline');
    tips.body[0] = 'Changed English'; fs.writeFileSync(path.join(f.data, 'tips/entries.json'), JSON.stringify(tips));
    assert.equal(seedQuestionsCache({ ...cache, entries: {} }, units, { data: f.data, store: seed }), 0, 'stale source overlay cannot seed even a matching title');
  } finally { f.cleanup(); }
});

test('Questions overlays never change original metadata and stale records explicitly remain English', async () => {
  const topic = { id: 42, title: 'English title', body: 'English summary', lang: 'ja', replies: null, solved: true,
    closed: null, coverage: { choice: 'none', probabilities: { none: 1 } }, active: 12 };
  const fields = { title: 'Titre français', body: 'Résumé français' };
  const overlay = { version: 1, locale: 'fr', page: 'questions', entries: {
    42: { source_hash: sourceHash(translatedFields('questions', topic)), translation_version: '1', fields },
  } };
  const [result] = await applyOverlay([topic], overlay, 'questions', 'fr');
  assert.equal(result.title, fields.title);
  assert.equal(result.contentLocale, 'fr');
  for (const key of ['id', 'lang', 'replies', 'solved', 'closed', 'coverage', 'active']) assert.deepEqual(result[key], topic[key]);
  const [stale] = await applyOverlay([{ ...topic, body: 'Changed summary' }], overlay, 'questions', 'fr');
  assert.equal(stale.translationFallback, true);
  assert.equal(stale.contentLocale, 'en');
  assert.equal(stale.title, topic.title, 'do not mix one stale field with translated fields');
});

test('local staging refuses unrelated outputs and overlap with any durable input', () => {
  assert.throws(() => stageLocalizedPreview({ source: 'unused', data: 'site/data', output: 'site' }), /isolated/);
  assert.throws(() => stageLocalizedPreview({ source: 'unused', data: 'build/questions', output: 'build/questions' }), /isolated/);
  assert.throws(() => stageLocalizedPreview({ source: 'unused', data: 'site/data',
    store: 'build/questions/store', output: 'build/questions' }), /isolated/);
  assert.throws(() => stageLocalizedPreview({ source: 'build/questions/sample.json', data: 'site/data',
    output: 'build/questions' }), /isolated/);
  assert.throws(() => stageLocalizedPreview({ source: 'unused', data: 'site/data',
    archive: 'build/questions/locales.tar.gz', output: 'build/questions' }), /isolated/);
});
