import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, relative, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { spawnSync } from 'node:child_process';
import { installPublicPreview } from './questions_public_preview.mjs';

export function stageLocalizedPreview({ source, data, store = 'localization', archive, output = 'build/questions-translated-ui' }) {
  const target = resolve(output), base = resolve('build'), rel = relative(base, target);
  const overlaps = (input) => input && (target === resolve(input)
    || target.startsWith(resolve(input) + sep) || resolve(input).startsWith(target + sep));
  if (!rel || rel === '..' || rel.startsWith('..' + sep) || target === resolve(data)
    || [data, store, source, archive].some(overlaps)) {
    throw new Error('Local preview output must be an isolated build/ subdirectory');
  }
  const marker = resolve(target, 'data/manifest.json');
  if (existsSync(target) && (!existsSync(marker) || JSON.parse(readFileSync(marker)).private_preview !== true)) {
    throw new Error('Refusing to overwrite an unrelated or nonprivate output');
  }
  mkdirSync(target, { recursive: true });
  cpSync(resolve('site'), target, { recursive: true, filter: (file) => file !== resolve('site/data') });
  cpSync(resolve(data), resolve(target, 'data'), { recursive: true });
  installPublicPreview(source, resolve(target, 'data'));
  const manifest = JSON.parse(readFileSync(marker));
  manifest.private_preview = true;
  manifest.publish_allowed = false;
  manifest.questions_view = { ...manifest.questions_view, public_preview: false, semantic_enabled: false };
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
    archive: { type: 'string' }, output: { type: 'string', default: 'build/questions-translated-ui' },
  } });
  if (!values.source || !values.data) throw new Error('--source and --data are required');
  console.log(`Local-only translated preview: ${stageLocalizedPreview(values)}`);
}
