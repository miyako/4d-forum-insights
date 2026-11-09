import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, relative, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { buildQuestions } from '../site/js/questions-core.js';

/** Only allowlisted metadata/text crosses from the private snapshot into the private UI. */
export function adaptPreview(snapshot) {
  if (snapshot?.schema_version !== 'questions-ui-private-preview-v1'
    || snapshot.private_preview !== true || snapshot.publish_allowed !== false || !Array.isArray(snapshot.items)) {
    throw new Error('Expected an explicitly private Questions UI snapshot');
  }
  const data = {
    version: 1, private_preview: true, publish_allowed: false, generated_at: snapshot.generated_at,
    entries: snapshot.items.map((row) => ({
      id: row.id, title: row.title, body: row.summary, active: row.active,
      lang: row.source_language, accepted: row.solved, closed: row.closed, replies: row.replies,
      answers: row.answers, evidence_warnings: row.evidence_warnings,
      decision_model: row.decision_model, decision_version: row.decision_version,
    })),
  };
  buildQuestions(data);
  return data;
}

export function preparePreview({ snapshot, data, output = 'build/questions-ui' }) {
  const root = resolve('.');
  const target = resolve(output);
  const rel = relative(resolve('build'), target);
  if (!rel || rel === '..' || rel.startsWith('..' + sep) || resolve('build', rel) !== target) {
    throw new Error('Preview output must be a subdirectory of this worktree build/');
  }
  const entries = adaptPreview(JSON.parse(readFileSync(snapshot, 'utf8')));
  const sourceData = resolve(data);
  const manifest = JSON.parse(readFileSync(resolve(sourceData, 'manifest.json'), 'utf8'));
  if (target === sourceData || sourceData.startsWith(target + sep) || target.startsWith(sourceData + sep)) {
    throw new Error('Preview output and input data must not overlap');
  }
  // Refuse to overwrite unrelated artifacts or an existing non-preview site.
  const marker = resolve(target, 'data/manifest.json');
  if (existsSync(target) && (!existsSync(marker) || JSON.parse(readFileSync(marker, 'utf8')).private_preview !== true)) {
    throw new Error('Preview output already exists without a private-preview marker');
  }
  mkdirSync(target, { recursive: true });
  cpSync(resolve(root, 'site'), target, { recursive: true, filter: (path) => path !== resolve(root, 'site/data') });
  cpSync(sourceData, resolve(target, 'data'), { recursive: true });
  mkdirSync(resolve(target, 'data/questions'), { recursive: true });
  writeFileSync(resolve(target, 'data/questions/entries.json'), JSON.stringify(entries));
  manifest.private_preview = true;
  manifest.publish_allowed = false;
  manifest.questions_view = { enabled: false, preview: true, semantic_enabled: false, files: { entries: 'questions/entries.json' } };
  writeFileSync(marker, JSON.stringify(manifest));
  return target;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const args = process.argv.slice(2);
  const options = {};
  for (let i = 0; i < args.length; i += 2) {
    if (!['--snapshot', '--data', '--output'].includes(args[i]) || !args[i + 1] || args[i + 1].startsWith('--')) {
      throw new Error('Usage: node scripts/questions_preview.mjs --snapshot FILE --data DIRECTORY [--output build/questions-ui]');
    }
    options[args[i].slice(2)] = args[i + 1];
  }
  if (!options.snapshot || !options.data) throw new Error('--snapshot and --data are required');
  console.log(`Private preview staged at ${preparePreview(options)} (not for deployment)`);
}
