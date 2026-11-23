#!/usr/bin/env node
// Check independently reviewed preview artifacts without publishing or approving them.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { adaptPreview } from './questions_preview.mjs';
import { validatePublicPreview } from './questions_public_preview.mjs';
import { canonical, recordsFromColumns, validateOverlay, validateShape, coverage } from './localization.mjs';

const digest = value => createHash('sha256').update(value).digest('hex');
const same = (left, right) => canonical(left) === canonical(right);

export function publicationPreflight({ candidate, audit, preview, fr, ja, clearance, clearanceSha256 }) {
  const approvalBytes = readFileSync(clearance);
  if (digest(approvalBytes) !== clearanceSha256) throw Error('Publication review hash mismatch');
  const approval = JSON.parse(approvalBytes);
  const files = { candidate, audit, preview, fr, ja };
  const bytes = Object.fromEntries(Object.entries(files).map(([key, file]) => [key, readFileSync(file)]));
  const hashes = Object.fromEntries(Object.entries(bytes).map(([key, value]) => [key, digest(value)]));
  const translationApproved =
    (approval.schema_version === 'questions-private-publication-review-v1' && approval.translation_review === true)
    || (approval.schema_version === 'questions-private-publication-owner-acceptance-v1'
      && approval.translation_review === false && approval.unreviewed_translations_accepted === true);
  if (!translationApproved
      || approval.candidate_sha256 !== hashes.candidate || approval.audit_sha256 !== hashes.audit
      || approval.preview_sha256 !== hashes.preview || approval.fr_sha256 !== hashes.fr || approval.ja_sha256 !== hashes.ja
      || approval.source_linked_privacy_review !== true
      || approval.publication_authorized !== true || typeof approval.reviewer !== 'string' || !approval.reviewer.trim()
      || typeof approval.reviewed_at !== 'string' || !Number.isFinite(Date.parse(approval.reviewed_at))
      || typeof approval.expected_current_preview_sha256 !== 'string'
      || !/^[a-f0-9]{64}$/.test(approval.expected_current_preview_sha256)) {
    throw Error('Independent publication review does not bind all artifacts and the expected live preview');
  }
  const snapshot = JSON.parse(bytes.candidate), auditData = JSON.parse(bytes.audit);
  const privateData = adaptPreview(snapshot);
  if (snapshot.snapshot_scope !== 'processed' || !Number.isSafeInteger(privateData.processed_count)
      || auditData.schema_version !== 'questions-export-private-audit-v1'
      || auditData.candidate_sha256 !== hashes.candidate || auditData.privacy_review_required !== true
      || auditData.publication_authorized !== false
      || ['processed_count', 'eligible_question_count', 'corpus_total', 'corpus_complete']
        .some(key => auditData[key] !== privateData[key])) {
    throw Error('Private candidate and audit do not match');
  }
  const publicData = validatePublicPreview(JSON.parse(bytes.preview));
  if (publicData.snapshot_scope !== 'processed' || publicData.processed_count !== privateData.processed_count
      || publicData.corpus_total !== privateData.corpus_total) throw Error('Public and private snapshot scopes differ');
  const original = new Map(privateData.entries.map(row => [row.id, row]));
  if (original.size !== publicData.entries.length) throw Error('Public and private membership differs');
  const invariant = ['active', 'lang', 'accepted', 'closed', 'replies', 'answers', 'evidence_warnings',
    ...(publicData.corpus_complete ? [] : ['decision_model', 'decision_version'])];
  for (const row of publicData.entries) {
    const source = original.get(row.id);
    if (!source || invariant.some(field => !same(source[field], row[field]))) {
      throw Error('Public preview changed private membership or decision metadata');
    }
  }
  const records = recordsFromColumns('questions', {
    ids: publicData.entries.map(row => row.id), title: publicData.entries.map(row => row.title),
    body: publicData.entries.map(row => row.body ?? ''),
  });
  for (const locale of ['fr', 'ja']) {
    const overlay = validateOverlay(JSON.parse(bytes[locale]), 'questions', locale);
    if (coverage(records, overlay).current !== records.length) throw Error(`Incomplete ${locale} Questions translations`);
    for (const record of records) validateShape('questions', overlay.entries[record.id].fields, record.fields);
  }
  return { technical_checks_passed: true, publication_performed: false,
    processed_count: publicData.processed_count, expected_current_preview_sha256: approval.expected_current_preview_sha256,
    preview_sha256: hashes.preview, fr_sha256: hashes.fr, ja_sha256: hashes.ja };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const { values } = parseArgs({ options: {
    candidate: { type: 'string' }, audit: { type: 'string' }, preview: { type: 'string' },
    fr: { type: 'string' }, ja: { type: 'string' }, clearance: { type: 'string' }, 'clearance-sha256': { type: 'string' },
  } });
  if (Object.keys(values).length !== 7) throw Error('Candidate, audit, preview, both locales, clearance and its hash are required');
  console.log(JSON.stringify(publicationPreflight({ ...values, clearanceSha256: values['clearance-sha256'] })));
}
