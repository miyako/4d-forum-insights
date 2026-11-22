import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { main } from '../scripts/questions_weekly_translate.mjs';
import { sourceHash } from '../scripts/localization.mjs';

test('weekly translation reuses current overlays and only translates a changed string', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'questions-weekly-'));
  try {
    const data = path.join(root, 'data'), store = path.join(root, 'locales');
    fs.mkdirSync(path.join(data, 'questions'), { recursive: true });
    fs.mkdirSync(path.join(store, 'fr'), { recursive: true });
    fs.writeFileSync(path.join(data, 'manifest.json'), JSON.stringify({
      private_preview: true, publish_allowed: false, questions: [],
      questions_view: { files: { entries: 'questions/entries.json' } },
    }));
    const fields = { title: 'Initial question?', body: 'Use ORDA.' };
    const writeSource = fields => fs.writeFileSync(path.join(data, 'questions/entries.json'),
      JSON.stringify({ version: 1, entries: [{ id: 1, ...fields }] }));
    writeSource(fields);
    fs.writeFileSync(path.join(store, 'fr/questions.json'), JSON.stringify({
      version: 1, locale: 'fr', page: 'questions', entries: {
        1: { source_hash: sourceHash(fields), translation_version: '1',
          fields: { title: 'Question initiale ?', body: 'Utilisez ORDA.' } },
      },
    }));
    let calls = 0;
    const translate = async prompt => {
      calls++;
      const batch = JSON.parse(prompt.slice(prompt.indexOf('INPUT JSON:\n') + 'INPUT JSON:\n'.length));
      assert.equal(batch.units.length, 1);
      return { batch_id: batch.batch_id, units: batch.units.map(unit => ({ id: unit.id, text: `Traduit ${unit.text}` })) };
    };
    const args = ['--data', data, '--store', store, '--locale', 'fr', '--model', 'synthetic-only',
      '--minutes', '1', '--workers', '1', '--entries', '10', '--batch-size', '10'];
    await main(args, { translate });
    assert.equal(calls, 0);
    writeSource({ ...fields, title: 'Changed question?' });
    await main(args, { translate });
    assert.equal(calls, 1);
    const overlay = JSON.parse(fs.readFileSync(path.join(store, 'fr/questions.json')));
    assert.equal(overlay.entries[1].fields.body, 'Utilisez ORDA.');
    assert.equal(overlay.entries[1].source_hash, sourceHash({ ...fields, title: 'Changed question?' }));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
