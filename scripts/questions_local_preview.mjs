import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, relative, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { installPublicPreview } from './questions_public_preview.mjs';
import { adaptPreview } from './questions_preview.mjs';

export function stageLocalizedPreview({ source, data, store = 'localization', archive, audit, output = 'build/questions-translated-ui' }) {
  const target = resolve(output), base = resolve('build'), rel = relative(base, target);
  const overlaps = (input) => input && (target === resolve(input)
    || target.startsWith(resolve(input) + sep) || resolve(input).startsWith(target + sep));
  if (!rel || rel === '..' || rel.startsWith('..' + sep) || target === resolve(data)
    || [data, store, source, archive, audit].some(overlaps)) {
    throw new Error('Local preview output must be an isolated build/ subdirectory');
  }
  const marker = resolve(target, 'data/manifest.json');
  if (existsSync(target) && (!existsSync(marker) || JSON.parse(readFileSync(marker)).private_preview !== true)) {
    throw new Error('Refusing to overwrite an unrelated or nonprivate output');
  }
  const sourceBytes = readFileSync(source);
  const snapshot = JSON.parse(sourceBytes);
  const privateData = snapshot.private_preview === true ? adaptPreview(snapshot) : null;
  if (privateData?.processed_count !== undefined) {
    if (!audit) throw new Error('Private processed candidate requires its source audit');
    const report = JSON.parse(readFileSync(audit, 'utf8'));
    if (report.schema_version !== 'questions-export-private-audit-v1'
      || report.privacy_review_required !== true || report.publication_authorized !== false
      || report.candidate_sha256 !== createHash('sha256').update(sourceBytes).digest('hex')
      || ['processed_count', 'eligible_question_count', 'corpus_total', 'corpus_complete']
        .some((key) => report[key] !== privateData[key])) {
      throw new Error('Private Questions candidate and source audit do not match');
    }
  }
  mkdirSync(target, { recursive: true });
  cpSync(resolve('site'), target, { recursive: true, filter: (file) => file !== resolve('site/data') });
  cpSync(resolve(data), resolve(target, 'data'), { recursive: true });
  if (privateData) {
    mkdirSync(resolve(target, 'data/questions'), { recursive: true });
    writeFileSync(resolve(target, 'data/questions/entries.json'), JSON.stringify(privateData));
  } else installPublicPreview(source, resolve(target, 'data'));
  const manifest = JSON.parse(readFileSync(marker));
  manifest.private_preview = true;
  manifest.publish_allowed = false;
  manifest.questions_view = {
    enabled: false, preview: true, public_preview: false, semantic_enabled: false,
    snapshot_scope: snapshot.snapshot_scope === 'processed' ? 'processed' : 'sample',
    ...(Number.isSafeInteger(snapshot.corpus_total) ? { corpus_total: snapshot.corpus_total } : {}),
    ...(privateData?.corpus_complete !== undefined ? { corpus_complete: privateData.corpus_complete } : {}),
    files: { entries: privateData ? 'questions/entries.json' : manifest.questions_view.files.entries },
  };
  writeFileSync(marker, JSON.stringify(manifest));
  const result = spawnSync('python3', ['scripts/publish_data.py', 'prepare-localization',
    '--data', resolve(target, 'data'), '--seeds', resolve(store), '--store', resolve(target, 'data/locales'),
    ...(archive ? ['--archive', resolve(archive)] : [])], { encoding: 'utf8' });
  if (result.status !== 0) throw new Error(`Preparing local overlays failed: ${result.stderr || result.error?.message}`);
  return target;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const { values } = parseArgs({ options: {
    source: { type: 'string' }, data: { type: 'string' }, store: { type: 'string', default: 'localization' },
    archive: { type: 'string' }, audit: { type: 'string' }, output: { type: 'string', default: 'build/questions-translated-ui' },
  } });
  if (!values.source || !values.data) throw new Error('--source and --data are required');
  console.log(`Local-only translated preview: ${stageLocalizedPreview(values)}`);
}
