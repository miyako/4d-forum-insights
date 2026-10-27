import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import {
  canonical, sourceHash, recordsFromColumns, protect, restore, emptyOverlay, makeBatch,
  importBatch, persistBatch, coverage, readOverlay, writeOverlay, validateOverlay,
} from '../scripts/localization.mjs';
import { main, callCopilot, translationPrompt } from '../scripts/localize.mjs';

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
  const root = mkdtempSync(path.join(tmpdir(), 'locale-test-'));
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
  const root = mkdtempSync(path.join(tmpdir(), 'locale-write-failure-'));
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
  const root = mkdtempSync(path.join(tmpdir(), 'locale-cli-'));
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
  const root = mkdtempSync(path.join(tmpdir(), 'locale-partial-')), oldExitCode = process.exitCode;
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
