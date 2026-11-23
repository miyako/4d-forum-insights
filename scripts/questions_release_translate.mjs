import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { privateDirectory, readReviewedCandidate, translateReviewed } from './questions_translate_review.mjs';
import { sealRelease } from './questions_release_transfer.mjs';

export async function translateRelease({ directory, model, entries = 1000, minutes = 20 }, {
  translate = translateReviewed, checkpoint = () => {},
} = {}) {
  const root = privateDirectory(directory);
  for (const [value, maximum] of [[entries, 70000], [minutes, 150]]) {
    if (!Number.isSafeInteger(value) || value < 1 || value > maximum) throw Error('Invalid release translation budget');
  }
  const candidate = join(root, 'candidate.json'), audit = join(root, 'audit.json');
  const clearance = join(root, 'translation-clearance.json');
  const clearanceSha256 = createHash('sha256').update(readFileSync(clearance)).digest('hex');
  readReviewedCandidate({ candidate, audit, clearance, clearanceSha256 });
  const store = join(root, 'translation-store');
  if (!existsSync(store)) mkdirSync(store, { mode: 0o700 });
  const deadline = Date.now() + minutes * 60000;
  let requested = 0, translated = 0, calls = 0, complete = false, pass = 0;
  while (requested < entries && Date.now() < deadline && !complete) {
    let result, failure;
    const started = Date.now();
    try {
      result = await translate({ candidate, audit, clearance, clearanceSha256, store, model,
        ...(!existsSync(join(store, '.questions-review-store.json')) ? { seedStore: join(root, 'localization') } : {}),
        entries: Math.min(1000, entries - requested), minutes: Math.max(1, Math.min(20, Math.floor((deadline - Date.now()) / 60000))),
        workers: 2, batchSize: 40 });
    } catch (error) {
      failure = error;
      const reportFile = join(store, 'report.json');
      if (existsSync(reportFile)) {
        const report = JSON.parse(readFileSync(reportFile));
        if (report.page === 'questions' && Date.parse(report.started) >= started) result = report;
      }
    }
    if (result) {
      requested += result.requested;
      translated += result.translated;
      calls += result.model_calls;
      complete = result.complete;
      if (!complete && result.requested === 0) failure = Error('Incomplete translation made no progress');
    }
    const summary = { requested, translated, model_calls: calls, complete,
      failed: Boolean(failure), ...(failure ? { private_error: failure.message } : {}),
      pass: ++pass, translation_review_required: true, publication_performed: false };
    writeFileSync(join(root, 'translation-summary.json'), JSON.stringify(summary) + '\n', { mode: 0o600 });
    await checkpoint(summary);
    if (failure) throw failure;
  }
  return { requested, translated, model_calls: calls, complete, translation_review_required: true, publication_performed: false };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  process.umask(0o077);
  const { values } = parseArgs({ options: {
    directory: { type: 'string' }, entries: { type: 'string', default: '1000' },
    minutes: { type: 'string', default: '20' }, model: { type: 'string', default: process.env.OPENAI_MODEL },
    'checkpoint-prefix': { type: 'string' },
  } });
  if (!values.directory || !values['checkpoint-prefix']
      || !/^questions-release-output-[0-9]+-[0-9]+$/.test(values['checkpoint-prefix'])) throw Error('A private directory and run-bound checkpoint prefix are required');
  translateRelease({ ...values, entries: Number(values.entries), minutes: Number(values.minutes) }, {
    checkpoint: async ({ pass }) => {
      const file = join(privateDirectory(dirname(resolve(values.directory))), `${values['checkpoint-prefix']}-${pass}.enc`);
      const result = sealRelease(values.directory, file, process.env.QUESTIONS_RELEASE_TRANSFER_KEY);
      execFileSync('gh', ['release', 'upload', 'data', file, '--repo', process.env.GITHUB_REPOSITORY], { stdio: 'pipe' });
      console.log(JSON.stringify({ encrypted_checkpoint: file.split('/').at(-1), sha256: result.sha256 }));
    },
  }).then(result => console.log(JSON.stringify(result))).catch(() => {
    console.error('Release translation failed; inspect the encrypted checkpoint locally. No publication was performed.');
    process.exitCode = 1;
  });
}
