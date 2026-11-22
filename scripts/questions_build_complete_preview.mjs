#!/usr/bin/env node
// Stage a reviewed candidate locally; this does not authorize or perform publication.
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { readReviewedCandidate, privateDirectory } from './questions_translate_review.mjs';
import { validatePublicPreview } from './questions_public_preview.mjs';

export function buildCompletePreview({ candidate, audit, clearance, clearanceSha256, sourceSelectedAt, output }) {
  const { privateData } = readReviewedCandidate({ candidate, audit, clearance, clearanceSha256 });
  if (privateData.corpus_complete !== true) throw Error('A complete reviewed candidate is required');
  if (!output || !sourceSelectedAt) throw Error('Output and independently verified source selection time are required');
  const destination = join(privateDirectory(dirname(resolve(output))), basename(output));
  const preview = validatePublicPreview({
    ...privateData, schema_version: 'questions-ui-public-complete-v1',
    public_preview: true, private_preview: false, publish_allowed: true, representative: false,
    source_selected_at: sourceSelectedAt,
  });
  const bytes = Buffer.from(JSON.stringify(preview) + '\n');
  writeFileSync(destination, bytes, { flag: 'wx', mode: 0o600 });
  return { preview_sha256: createHash('sha256').update(bytes).digest('hex'),
    processed_count: preview.processed_count, translation_review_required: true, publication_performed: false };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const { values } = parseArgs({ options: {
    candidate: { type: 'string' }, audit: { type: 'string' }, clearance: { type: 'string' },
    'clearance-sha256': { type: 'string' }, 'source-selected-at': { type: 'string' }, output: { type: 'string' },
  } });
  if (!values.candidate || !values.audit || !values.clearance || !values['clearance-sha256']
      || !values['source-selected-at'] || !values.output) {
    throw Error('Candidate, audit, clearance, clearance SHA-256, source selection time and output are required');
  }
  process.umask(0o077);
  console.log(JSON.stringify(buildCompletePreview({ ...values,
    clearanceSha256: values['clearance-sha256'], sourceSelectedAt: values['source-selected-at'] })));
}
