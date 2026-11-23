import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { openRelease, sealRelease } from '../scripts/questions_release_transfer.mjs';
import { translateRelease } from '../scripts/questions_release_translate.mjs';
import { COVERAGE, IMPACT, COMPLEXITY, DOMAINS } from '../site/js/questions-core.js';

const hash = data => createHash('sha256').update(data).digest('hex');
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'questions-release-test-'));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true }));
  const input = path.join(root, 'input');
  fs.mkdirSync(input, { mode: 0o700 });
  const write = (name, value) => fs.writeFileSync(path.join(input, name), JSON.stringify(value), { mode: 0o600 });
  return { root, input, write, key: randomBytes(32).toString('hex') };
}

function reviewed(f) {
  const rating = labels => ({ choice: Object.keys(labels)[0],
    probabilities: Object.fromEntries(Object.keys(labels).map((key, index) => [key, index === 0 ? 1 : 0])) });
  f.write('candidate.json', { schema_version: 'questions-ui-private-preview-v1', version: 1,
    private_preview: true, publish_allowed: false, generated_at: '2026-11-23T00:00:00Z',
    snapshot_scope: 'processed', corpus_complete: true, processed_count: 1, eligible_question_count: 1, corpus_total: 1,
    entries: [{ id: 1, title: 'Query records', body: 'A query needs clarification.', lang: 'en',
      active: null, accepted: null, closed: null, replies: null, answers: {
        has_question: { noul: 1 }, clarification: { noul: 0 },
        coverage: rating(COVERAGE), impact: rating(IMPACT), technicality: rating(COMPLEXITY),
        ...Object.fromEntries(Object.keys(DOMAINS).map(key => ['domain_' + key, { noul: 0 }])),
      } }] });
  const candidateHash = hash(fs.readFileSync(path.join(f.input, 'candidate.json')));
  f.write('audit.json', { schema_version: 'questions-export-private-audit-v1',
    candidate_sha256: candidateHash, privacy_review_required: true, publication_authorized: false,
    processed_count: 1, eligible_question_count: 1, corpus_total: 1, corpus_complete: true });
  f.write('translation-clearance.json', { schema_version: 'questions-private-translation-clearance-v1',
    candidate_sha256: candidateHash, audit_sha256: hash(fs.readFileSync(path.join(f.input, 'audit.json'))),
    source_linked_privacy_review: true, openai_translation_authorized: true,
    reviewer: 'Synthetic fixture', reviewed_at: '2026-11-23T00:00:00Z' });
}

test('authenticated encrypted transfer preserves only allowlisted private file bytes', t => {
  const f = fixture(t);
  f.write('candidate.json', { private: 'synthetic-only' });
  const cipher = path.join(f.root, 'bundle.enc'), output = path.join(f.root, 'output');
  const receipt = sealRelease(f.input, cipher, f.key);
  assert.equal(receipt.sha256, hash(fs.readFileSync(cipher)));
  assert.equal(fs.readFileSync(cipher).includes(Buffer.from('synthetic-only')), false);
  openRelease(cipher, output, f.key);
  assert.deepEqual(fs.readFileSync(path.join(output, 'candidate.json')), fs.readFileSync(path.join(f.input, 'candidate.json')));
  assert.equal(fs.statSync(output).mode & 0o777, 0o700);
  assert.equal(fs.statSync(path.join(output, 'candidate.json')).mode & 0o777, 0o600);
});

test('wrong keys and tampering fail authentication before creating an output directory', t => {
  const f = fixture(t);
  f.write('candidate.json', {});
  const cipher = path.join(f.root, 'bundle.enc'), output = path.join(f.root, 'output');
  sealRelease(f.input, cipher, f.key);
  assert.throws(() => openRelease(cipher, output, randomBytes(32).toString('hex')));
  assert.equal(fs.existsSync(output), false);
  const bytes = fs.readFileSync(cipher);
  bytes[bytes.length - 1] ^= 1;
  fs.writeFileSync(cipher, bytes);
  assert.throws(() => openRelease(cipher, output, f.key));
  assert.equal(fs.existsSync(output), false);
});

test('transfer rejects unallowlisted source files, nonprivate files and symlinks', t => {
  const f = fixture(t), cipher = path.join(f.root, 'bundle.enc');
  f.write('source.db', {});
  assert.throws(() => sealRelease(f.input, cipher, f.key), /Unexpected/);
  fs.unlinkSync(path.join(f.input, 'source.db'));
  f.write('candidate.json', {});
  fs.chmodSync(path.join(f.input, 'candidate.json'), 0o644);
  assert.throws(() => sealRelease(f.input, cipher, f.key), /private/);
  fs.unlinkSync(path.join(f.input, 'candidate.json'));
  fs.symlinkSync(path.join(f.root, 'outside.json'), path.join(f.input, 'candidate.json'));
  assert.throws(() => sealRelease(f.input, cipher, f.key), /private/);
});

test('release translation drains the exact budget, checkpoints and never authorizes publication', async t => {
  const f = fixture(t);
  reviewed(f);
  const calls = [], saved = [];
  const result = await translateRelease({ directory: f.input, model: 'synthetic-model', entries: 1001, minutes: 1 }, {
    translate: async options => {
      calls.push(options.entries);
      return { requested: options.entries, translated: options.entries, model_calls: 1, complete: calls.length === 2 };
    },
    checkpoint: async summary => saved.push(summary),
  });
  assert.deepEqual(calls, [1000, 1]);
  assert.equal(saved.length, 2);
  assert.equal(result.complete, true);
  assert.equal(result.translation_review_required, true);
  assert.equal(result.publication_performed, false);
});

test('failure checkpoints partial results without automatic redispatch; stale clearance blocks calls', async t => {
  const f = fixture(t);
  reviewed(f);
  let calls = 0, checkpoints = 0;
  await assert.rejects(translateRelease({ directory: f.input, model: 'synthetic-model', entries: 2000, minutes: 1 }, {
    translate: async () => { calls++; throw Error('Synthetic transport failure'); },
    checkpoint: async summary => { checkpoints++; assert.equal(summary.failed, true); },
  }), /Synthetic transport failure/);
  assert.equal(calls, 1);
  assert.equal(checkpoints, 1);
  f.write('candidate.json', {});
  await assert.rejects(translateRelease({ directory: f.input, model: 'synthetic-model' }, {
    translate: async () => { calls++; },
  }), /clearance/);
  assert.equal(calls, 1);
});
