import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import path from 'node:path';
import {
  canonical, sourceHash, recordsFromColumns, protect, restore, emptyOverlay, makeBatch,
  importBatch, persistBatch, coverage, readOverlay, writeOverlay, validateOverlay, readSourceRecords,
} from '../scripts/localization.mjs';
import { main, callCopilot, translationPrompt } from '../scripts/localize.mjs';
import {
  stringUnits, makeUnitBatch, importUnitBatch, readTextCache, materialize, main as backfill,
} from '../scripts/backfill-requests.mjs';

const testRoot = path.resolve('build/localization-tests');
mkdirSync(testRoot, { recursive: true });

const source = { headline: 'How to use `QUERY` with 4D?', body: 'See https://example.com/a and $value.' };
const records = recordsFromColumns('tips', { ids: [42], headline: [source.headline], body: [source.body] });

test('canonical hash is key-order independent and content-sensitive', () => {
  assert.equal(canonical({ b: 2, a: ['x'] }), '{"a":["x"],"b":2}');
  assert.equal(sourceHash(source), sourceHash({ body: source.body, headline: source.headline }));
  assert.notEqual(sourceHash(source), sourceHash({ ...source, body: 'changed' }));
});

test('protected code, URL, product and variable tokens survive translation exactly', () => {
  const masked = protect(source);
  assert.deepEqual(restore('tips', masked.fields, masked), source);
  assert.throws(() => restore('tips', { ...masked.fields, body: 'missing tokens' }, masked), /placeholders/);
  assert.throws(() => restore('tips', { ...masked.fields, body: masked.fields.body + ' `new code`' }, masked), /introduced/);
});

test('existing unmatched code delimiters, object paths and bare URL schemes round-trip safely', () => {
  for (const body of [
    'Use `$object.subObject.property` or `$object["subObject"]["property"]. Then parse the path.',
    'URLs must begin with http:// or https://.',
    'Include a URL scheme (https:// or http://).',
  ]) {
    const fields = { headline: '4D', body }, masked = protect(fields);
    assert.deepEqual(restore('tips', masked.fields, masked), fields);
    assert.equal(/https?:\/\/|`/.test(masked.fields.body), false);
  }
});

test('batch import rejects partial, duplicate, stale and malformed output without mutation', () => {
  const overlay = emptyOverlay('tips', 'ja'), batch = makeBatch('tips', 'ja', records);
  assert.throws(() => importBatch(overlay, records, batch, { records: [] }), /incomplete/);
  assert.throws(() => importBatch(overlay, records, batch, {
    batch_id: batch.batch_id,
    records: [{ ...batch.records[0], source_hash: '0'.repeat(64) }],
  }), /stale/);
  assert.deepEqual(overlay.entries, {});
  importBatch(overlay, records, batch, { batch_id: batch.batch_id, records: batch.records });
  assert.deepEqual(overlay.entries[42].fields, source);
  assert.equal(coverage(records, overlay).current, 1);
  assert.equal(coverage([{ ...records[0], source_hash: '0'.repeat(64) }], overlay).stale, 1);
});

test('Japanese and French need shape checks, not English word/question checks', () => {
  const source = recordsFromColumns('voices', { ids: [1], question: ['Question?'],
    positions: [['One view.', 'Another view.']], outcome: ['Unresolved.'] });
  for (const [locale, fields] of [
    ['ja', { question: '何が問題ですか。', positions: ['一つの見方。', '別の見方。'], outcome: '未解決です。' }],
    ['fr', { question: 'Quel est le problème ?', positions: ['Un point de vue.', 'Un autre avis.'], outcome: 'Non résolu.' }],
  ]) {
    const overlay = emptyOverlay('voices', locale), batch = makeBatch('voices', locale, source);
    importBatch(overlay, source, batch, { batch_id: batch.batch_id, records: [{ ...batch.records[0], fields }] });
    assert.equal(coverage(source, overlay).current, 1);
    assert.throws(() => importBatch(overlay, source, batch, {
      batch_id: batch.batch_id,
      records: [{ ...batch.records[0], fields: { ...fields, positions: ['One'] } }],
    }), /position count/);
  }
});

test('checkpoints round-trip independently of English export and reject invalid identities', () => {
  const root = mkdtempSync(path.join(testRoot, 'locale-test-'));
  try {
    const overlay = emptyOverlay('tips', 'fr'), batch = makeBatch('tips', 'fr', records);
    importBatch(overlay, records, batch, { batch_id: batch.batch_id, records: batch.records });
    writeOverlay(root, overlay);
    assert.deepEqual(readOverlay(root, 'tips', 'fr'), overlay);
    assert.equal(coverage(records, readOverlay(root, 'tips', 'ja')).missing, 1);
    overlay.entries[42].translation_version = 'previous-prompt';
    writeOverlay(root, overlay);
    assert.equal(coverage(records, readOverlay(root, 'tips', 'fr')).stale, 1);
    assert.throws(() => validateOverlay({ ...overlay, locale: 'ja' }, 'tips', 'fr'), /Invalid overlay/);
  } finally { rmSync(root, { recursive: true }); }
});

test('import validates the complete batch before persisting any records', () => {
  const source = recordsFromColumns('tips', { ids: [42, 43],
    headline: ['First', 'Second'], body: ['First answer', 'Second answer'] });
  const overlay = emptyOverlay('tips', 'fr'), batch = makeBatch('tips', 'fr', source);
  assert.throws(() => importBatch(overlay, source, batch, {
    batch_id: batch.batch_id,
    records: [batch.records[0], { ...batch.records[1], fields: { headline: 'Missing body' } }],
  }), /fields/);
  assert.deepEqual(overlay.entries, {});
  assert.throws(() => importBatch(overlay, source, batch, {
    batch_id: batch.batch_id,
    records: [batch.records[0], batch.records[0]],
  }), /duplicate/);
  const changed = structuredClone(batch);
  changed.records[0].fields.body = 'Tampered source';
  assert.throws(() => importBatch(overlay, source, changed, { records: batch.records }), /integrity/);
});

test('one batch identity binds locale, source hashes and prompt version without repeated copied hashes', () => {
  const overlay = emptyOverlay('tips', 'fr'), batch = makeBatch('tips', 'fr', records);
  const response = { batch_id: batch.batch_id,
    records: batch.records.map(({ id, fields }) => ({ id, fields })) };
  importBatch(overlay, records, batch, response);
  assert.equal(coverage(records, overlay).current, 1);
  assert.throws(() => importBatch(emptyOverlay('tips', 'ja'), records,
    makeBatch('tips', 'ja', records), response), /batch identity/);
  assert.throws(() => importBatch(emptyOverlay('tips', 'fr'), records, batch,
    { records: response.records }), /batch identity/);
});

test('checkpoint write failure cannot create success-shaped in-memory coverage', () => {
  const root = mkdtempSync(path.join(testRoot, 'locale-write-failure-'));
  try {
    const overlay = emptyOverlay('tips', 'fr'), batch = makeBatch('tips', 'fr', records);
    writeFileSync(path.join(root, 'fr'), 'Blocks the locale directory');
    assert.throws(() => persistBatch(root, overlay, records, batch,
      { batch_id: batch.batch_id, records: batch.records }));
    assert.equal(coverage(records, overlay).current, 0);
    assert.deepEqual(overlay.entries, {});
  } finally { rmSync(root, { recursive: true }); }
});

test('export, import and coverage CLI share the portable checkpoint format', async () => {
  const root = mkdtempSync(path.join(testRoot, 'locale-cli-'));
  try {
    const data = path.join(root, 'data'), store = path.join(root, 'store');
    mkdirSync(path.join(data, 'tips'), { recursive: true });
    writeFileSync(path.join(data, 'tips', 'entries.json'), JSON.stringify({
      ids: [42], headline: [source.headline], body: [source.body],
    }));
    const batchFile = path.join(root, 'batch.json'), responseFile = path.join(root, 'response.json');
    const report = path.join(root, 'report.json');
    const common = ['--data', data, '--store', store, '--page', 'tips', '--locale', 'ja', '--report', report];
    await main([...common, '--export-batch', batchFile]);
    const batch = JSON.parse(readFileSync(batchFile));
    writeFileSync(responseFile, JSON.stringify({ batch_id: batch.batch_id, records: batch.records }));
    await main([...common, '--import-batch', batchFile, '--response', responseFile]);
    assert.equal(JSON.parse(readFileSync(report)).complete, true);
    const before = readFileSync(path.join(store, 'ja', 'tips.json'), 'utf8');
    await main([...common, '--check-only']);
    assert.equal(readFileSync(path.join(store, 'ja', 'tips.json'), 'utf8'), before);
    await assert.rejects(() => main(common), /explicit|Set --model/);
  } finally { rmSync(root, { recursive: true }); }
});

test('translation invocation errors are explicit, with no silent model fallback', async () => {
  await assert.rejects(callCopilot('test', { model: 'test', executable: '/usr/bin/false' }), /exit/);
  const prompt = translationPrompt(makeBatch('tips', 'ja', records));
  assert.match(prompt, /untrusted data/);
  assert.match(prompt, /Japanese/);
});

test('a failed worker retains successful peers and a rerun resumes only unfinished records', async () => {
  const root = mkdtempSync(path.join(testRoot, 'locale-partial-')), oldExitCode = process.exitCode;
  try {
    const data = path.join(root, 'data'), store = path.join(root, 'store'), report = path.join(root, 'report.json');
    mkdirSync(path.join(data, 'tips'), { recursive: true });
    writeFileSync(path.join(data, 'tips', 'entries.json'), JSON.stringify({
      ids: [42, 43], headline: ['First', 'Second'], body: ['First answer', 'Second answer'],
    }));
    const args = ['--data', data, '--store', store, '--page', 'tips', '--locale', 'fr',
      '--report', report, '--model', 'test-frontier', '--batch-size', '1', '--workers', '2', '--retries', '1'];
    const extract = prompt => JSON.parse(prompt.split('INPUT JSON:\n')[1]);
    const partial = await main(args, { translate: async prompt => {
      const batch = extract(prompt);
      if (batch.records[0].id === 42) throw Error('Simulated quota failure');
      return { batch_id: batch.batch_id, records: batch.records };
    } });
    assert.equal(process.exitCode, 1);
    assert.equal(partial.coverage['fr/tips'].current, 1);
    assert.equal(partial.complete, false);
    assert.equal(readOverlay(store, 'tips', 'fr').entries[43].fields.body, 'Second answer');
    assert.deepEqual(partial.errors[0].ids, [42]);
    const requested = [];
    process.exitCode = oldExitCode;
    const resumed = await main(args, { translate: async prompt => {
      const batch = extract(prompt);
      requested.push(...batch.records.map(r => r.id));
      return { batch_id: batch.batch_id, records: batch.records };
    } });
    assert.deepEqual(requested, [42]);
    assert.equal(resumed.coverage['fr/tips'].current, 2);
    assert.equal(resumed.complete, true);
    await main([...args, '--check-only', '--require-complete']);
    assert.equal(process.exitCode, oldExitCode);
    await main([...args, '--locale', 'ja', '--check-only', '--require-complete']);
    assert.equal(process.exitCode, 1);
  } finally { process.exitCode = oldExitCode; rmSync(root, { recursive: true }); }
});

function requestFixture(root) {
  mkdirSync(root, { recursive: true });
  writeFileSync(path.join(root, 'manifest.json'), JSON.stringify({
    files: { topics: 'topics.json?v=123', text: 'text.json?v=456' },
  }));
  writeFileSync(path.join(root, 'topics.json'), JSON.stringify({
    fields: ['id', 'title', 'title_en', 'line'],
    rows: [[1, 'Raw private title never translated', null, 'Use 4D.'],
      [2, 'Another raw title', 'Use 4D.', 'Use 4D.']],
  }));
  writeFileSync(path.join(root, 'text.json'), JSON.stringify({ para: ['Same generated summary.', 'Same generated summary.'] }));
}

test('Requests read only generated summaries, strip cache queries and normalize absent English titles', () => {
  const root = mkdtempSync(path.join(testRoot, 'requests-source-'));
  try {
    requestFixture(root);
    const records = readSourceRecords(root, 'requests');
    assert.deepEqual(records[0].fields, { title_en: '', line: 'Use 4D.', para: 'Same generated summary.' });
    assert.equal(JSON.stringify(records).includes('Raw private'), false);
    assert.equal(stringUnits(records).size, 2);
    writeFileSync(path.join(root, 'topics.json'), JSON.stringify({
      fields: ['id', 'title_en', 'line'], rows: [[1, null, null]],
    }));
    writeFileSync(path.join(root, 'text.json'), JSON.stringify({ para: [null] }));
    assert.deepEqual(readSourceRecords(root, 'requests')[0].fields, { title_en: '', line: '', para: '' });
    writeFileSync(path.join(root, 'manifest.json'), JSON.stringify({
      files: { topics: '../outside.json', text: 'text.json' },
    }));
    assert.throws(() => readSourceRecords(root, 'requests'), /escapes/);
  } finally { rmSync(root, { recursive: true }); }
});

test('Requests text cache deduplicates fields and topics with source/version/token validation', () => {
  const records = recordsFromColumns('requests', { ids: [1, 2], title_en: ['', 'Use 4D.'],
    line: ['Use 4D.', 'Use 4D.'], para: ['Same summary.', 'Same summary.'] });
  const units = stringUnits(records), batch = makeUnitBatch('fr', units);
  const empty = { version: 1, locale: 'fr', translation_version: '1', entries: {} };
  const response = { batch_id: batch.batch_id, units: batch.units.map(unit => ({ ...unit,
    text: unit.text.replace('Use', 'Utilisez').replace('Same summary.', 'Même résumé.') })) };
  const cache = importUnitBatch(empty, units, batch, response);
  assert.equal(Object.keys(empty.entries).length, 0);
  const overlay = materialize(records, cache, emptyOverlay('requests', 'fr'));
  assert.equal(coverage(records, overlay).current, 2);
  assert.deepEqual(overlay.entries[1].fields, { title_en: '', line: 'Utilisez 4D.', para: 'Même résumé.' });
  assert.throws(() => importUnitBatch(empty, units, batch, { ...response, units: response.units.slice(1) }), /Incomplete/);
  assert.throws(() => importUnitBatch(empty, units, batch, { ...response, batch_id: 'changed' }), /identity/);
  assert.throws(() => importUnitBatch(empty, units, batch,
    { ...response, units: [response.units[0], response.units[0]] }), /duplicate/);
  const broken = structuredClone(response);
  broken.units[0].text = 'Unprotected translation';
  assert.throws(() => importUnitBatch(empty, units, batch, broken), /placeholders/);
  assert.throws(() => importUnitBatch(empty, new Map([...units].map(([id]) => [id, 'changed'])), batch, response), /Stale/);
  const changed = records.map(record => ({ ...record, fields: { ...record.fields, para: 'Changed summary.' },
    source_hash: sourceHash({ ...record.fields, para: 'Changed summary.' }) }));
  assert.equal(coverage(changed, materialize(changed, cache, overlay)).stale, 2);
});

test('portable Requests text cache rejects changed protected tokens and versions', () => {
  const root = mkdtempSync(path.join(testRoot, 'requests-cache-'));
  try {
    const units = new Map([[sourceHash('Use 4D and `QUERY`.'), 'Use 4D and `QUERY`.']]);
    const batch = makeUnitBatch('ja', units), file = path.join(root, 'cache.json');
    const cache = importUnitBatch({ locale: 'ja', entries: {} }, units, batch,
      { batch_id: batch.batch_id, units: batch.units });
    Object.assign(cache, { version: 1, translation_version: '1' });
    writeFileSync(file, JSON.stringify(cache));
    assert.deepEqual(readTextCache(file, 'ja', units), cache);
    cache.entries[batch.units[0].source_hash].text = 'Changed protected text';
    writeFileSync(file, JSON.stringify(cache));
    assert.throws(() => readTextCache(file, 'ja', units), /protected token/);
    cache.translation_version = 'old';
    writeFileSync(file, JSON.stringify(cache));
    assert.throws(() => readTextCache(file, 'ja', units), /identity/);
  } finally { rmSync(root, { recursive: true }); }
});

test('Requests backfill checkpoints both locales and resumes only unfinished exact strings', async () => {
  const root = mkdtempSync(path.join(testRoot, 'requests-run-')), oldExitCode = process.exitCode;
  try {
    const data = path.join(root, 'data'), store = path.join(root, 'store');
    requestFixture(data);
    const args = ['--data', data, '--store', store, '--model', 'test-frontier', '--workers', '2',
      '--batch-size', '1', '--retries', '1'];
    const extract = prompt => JSON.parse(prompt.split('INPUT JSON:\n')[1]);
    const first = await backfill(args, { translate: async prompt => {
      const batch = extract(prompt);
      if (batch.units[0].text.includes('Same generated')) throw Error('Simulated failure');
      return batch;
    } });
    assert.equal(first.translated, 2);
    assert.equal(first.coverage.fr.current, 0);
    assert.equal(first.coverage.ja.current, 0);
    assert.equal(first.strings.fr.current, 1);
    assert.equal(first.strings.ja.current, 1);
    assert.equal(process.exitCode, 1);
    process.exitCode = oldExitCode;
    const requested = [];
    const second = await backfill([...args, '--require-complete'], { translate: async prompt => {
      const batch = extract(prompt);
      requested.push(...batch.units.map(unit => unit.text));
      return batch;
    } });
    assert.equal(second.complete, true);
    assert.equal(requested.length, 2);
    assert.equal(requested.every(text => text === 'Same generated summary.'), true);
    assert.equal(second.coverage.fr.current, 2);
    assert.equal(second.coverage.ja.current, 2);
    assert.equal(process.exitCode, oldExitCode);
    await backfill([...args, '--check-only', '--require-complete']);
    const records = readSourceRecords(data, 'requests');
    assert.equal(coverage(records, readOverlay(store, 'requests', 'fr')).current, 2);
    // Existing topic-oriented export/import remains valid for Requests.
    const batch = makeBatch('requests', 'fr', records), overlay = emptyOverlay('requests', 'fr');
    importBatch(overlay, records, batch, { batch_id: batch.batch_id, records: batch.records });
    assert.equal(coverage(records, overlay).current, 2);
  } finally { process.exitCode = oldExitCode; rmSync(root, { recursive: true }); }
});

test('Requests bounded entry budget and exclusive writer lock cannot inflate coverage', async () => {
  const root = mkdtempSync(path.join(testRoot, 'requests-bounds-')), oldExitCode = process.exitCode;
  try {
    const data = path.join(root, 'data'), store = path.join(root, 'store');
    requestFixture(data);
    const args = ['--data', data, '--store', store, '--model', 'test-frontier', '--entries', '1',
      '--workers', '8', '--batch-size', '30', '--require-complete'];
    let calls = 0;
    const report = await backfill(args, { translate: async prompt => {
      calls++;
      return JSON.parse(prompt.split('INPUT JSON:\n')[1]);
    } });
    assert.equal(calls, 1);
    assert.equal(report.requested, 1);
    assert.equal(report.translated, 1);
    assert.equal(report.complete, false);
    assert.equal(report.coverage.fr.current, 0);
    assert.equal(report.coverage.ja.current, 0);
    assert.equal(process.exitCode, 1);
    mkdirSync(`${store}.lock`);
    await assert.rejects(() => backfill(args, { translate: async () => { throw Error('Must not call model'); } }), /EEXIST/);
    assert.equal(calls, 1);
    await assert.rejects(() => backfill([...args, '--workers', '9']), /Invalid workers/);
  } finally { process.exitCode = oldExitCode; rmSync(root, { recursive: true }); }
});

test('Requests invalid model batch is subdivided and only validated text reaches the cache', async () => {
  const root = mkdtempSync(path.join(testRoot, 'requests-split-')), oldExitCode = process.exitCode;
  try {
    const data = path.join(root, 'data'), store = path.join(root, 'store');
    requestFixture(data);
    const report = await backfill(['--data', data, '--store', store, '--locale', 'fr',
      '--model', 'test-frontier', '--retries', '1', '--workers', '1', '--batch-size', '30'],
    { translate: async prompt => {
      const batch = JSON.parse(prompt.split('INPUT JSON:\n')[1]);
      return batch.units.length > 1 ? { ...batch, units: [] } : batch;
    } });
    assert.equal(report.complete, true);
    assert.equal(report.translated, 2);
    assert.equal(report.requested, 4);
    assert.deepEqual(report.errors, []);
    assert.equal(process.exitCode, oldExitCode);
  } finally { process.exitCode = oldExitCode; rmSync(root, { recursive: true }); }
});

test('Requests failed text-cache write cannot produce successful topic coverage', async () => {
  const root = mkdtempSync(path.join(testRoot, 'requests-write-')), oldExitCode = process.exitCode;
  try {
    const data = path.join(root, 'data'), store = path.join(root, 'store');
    requestFixture(data);
    mkdirSync(store);
    writeFileSync(path.join(store, 'text-cache'), 'Blocks atomic cache persistence');
    const report = await backfill(['--data', data, '--store', store, '--locale', 'fr',
      '--model', 'test-frontier', '--retries', '1', '--workers', '1'],
    { translate: async prompt => JSON.parse(prompt.split('INPUT JSON:\n')[1]) });
    assert.equal(report.translated, 0);
    assert.equal(report.strings.fr.current, 0);
    assert.equal(report.coverage.fr.current, 0);
    assert.equal(report.complete, false);
    assert.equal(process.exitCode, 1);
    assert.deepEqual(readOverlay(store, 'requests', 'fr').entries, {});
  } finally { process.exitCode = oldExitCode; rmSync(root, { recursive: true }); }
});
