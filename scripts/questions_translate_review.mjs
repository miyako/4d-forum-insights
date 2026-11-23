#!/usr/bin/env node
// Translate only a separately reviewed private candidate; never promote or publish it.
import { createHash } from 'node:crypto';
import { cpSync, lstatSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { adaptPreview } from './questions_preview.mjs';
import { main as backfill } from './backfill-requests.mjs';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const STORE_MARKER = '.questions-review-store.json';

export function privateDirectory(directory) {
  const resolved = realpathSync(directory), stat = statSync(resolved);
  if (!stat.isDirectory() || (stat.mode & 0o077) || stat.uid !== process.getuid()) {
    throw Error('Translation stores must be existing owner-only private directories');
  }
  return resolved;
}

function digestTree(directory, relative = '') {
  const result = [];
  const entries = readdirSync(join(directory, relative), { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
    if (!relative && entry.name === STORE_MARKER) continue;
    const name = join(relative, entry.name), file = join(directory, name);
    if (entry.isSymbolicLink() || (!entry.isFile() && !entry.isDirectory())) {
      throw Error('Translation seed store must contain only regular files and directories');
    }
    if (entry.isDirectory()) result.push(['directory', name], ...digestTree(directory, name));
    else result.push(['file', name, hash(readFileSync(file))]);
  }
  return result;
}

function initializeStore(storePath, seedStore, candidateHash, auditHash) {
  const entries = readdirSync(storePath);
  if (entries.length) {
    const marker = join(storePath, STORE_MARKER);
    let identity;
    try {
      if (!lstatSync(marker).isFile()) throw Error('Invalid marker');
      identity = JSON.parse(readFileSync(marker, 'utf8'));
    }
    catch { throw Error('Translation store must be a new empty directory or a matching review store'); }
    if (identity.schema_version !== 'questions-review-store-v1' || identity.state !== 'ready'
        || identity.candidate_sha256 !== candidateHash || identity.audit_sha256 !== auditHash || seedStore) {
      throw Error('Translation review store identity does not match; use a new store for this candidate');
    }
    return;
  }
  const marker = join(storePath, STORE_MARKER);
  const identity = { schema_version: 'questions-review-store-v1', state: 'initializing',
    candidate_sha256: candidateHash, audit_sha256: auditHash };
  writeFileSync(marker, JSON.stringify(identity), { flag: 'wx', mode: 0o600 });
  if (seedStore) {
    const seed = privateDirectory(seedStore);
    if (storePath === seed || storePath.startsWith(seed + sep) || seed.startsWith(storePath + sep)) {
      throw Error('Translation seed and review store must not overlap');
    }
    const lock = `${seed}.lock`;
    mkdirSync(lock, { mode: 0o700 });
    try {
      const before = JSON.stringify(digestTree(seed));
      for (const entry of readdirSync(seed)) {
        if (entry !== STORE_MARKER) {
          cpSync(join(seed, entry), join(storePath, entry), { recursive: true, force: false, errorOnExist: true });
        }
      }
      if (before !== JSON.stringify(digestTree(seed)) || before !== JSON.stringify(digestTree(storePath))) {
        throw Error('Translation seed store changed during copying');
      }
    } finally {
      rmSync(lock, { recursive: true });
    }
  }
  identity.state = 'ready';
  writeFileSync(marker, JSON.stringify(identity), { mode: 0o600 });
}

export function readReviewedCandidate({ candidate, audit, clearance, clearanceSha256 }) {
  const clearanceBytes = readFileSync(clearance);
  if (hash(clearanceBytes) !== clearanceSha256) throw Error('Clearance does not match the independently verified SHA-256');
  const approval = JSON.parse(clearanceBytes);
  const candidateBytes = readFileSync(candidate), auditBytes = readFileSync(audit);
  const snapshot = JSON.parse(candidateBytes), auditData = JSON.parse(auditBytes);
  const candidateHash = hash(candidateBytes), auditHash = hash(auditBytes);
  if (approval.schema_version !== 'questions-private-translation-clearance-v1'
      || approval.candidate_sha256 !== candidateHash || approval.audit_sha256 !== auditHash
      || approval.source_linked_privacy_review !== true || approval.openai_translation_authorized !== true
      || typeof approval.reviewer !== 'string' || !approval.reviewer.trim()
      || typeof approval.reviewed_at !== 'string' || !Number.isFinite(Date.parse(approval.reviewed_at))) {
    throw Error('Independent source-linked translation clearance does not match the candidate and audit');
  }
  const privateData = adaptPreview(snapshot);
  if (snapshot.snapshot_scope !== 'processed' || !Number.isSafeInteger(privateData.processed_count)
      || auditData.schema_version !== 'questions-export-private-audit-v1'
      || auditData.candidate_sha256 !== candidateHash || auditData.privacy_review_required !== true
      || auditData.publication_authorized !== false
      || ['processed_count', 'eligible_question_count', 'corpus_total', 'corpus_complete']
        .some(key => auditData[key] !== privateData[key])) {
    throw Error('Private candidate and audit do not match');
  }
  return { privateData, candidateHash, auditHash };
}

export async function translateReviewed({ candidate, audit, clearance, clearanceSha256, store, seedStore, model,
  entries = '100', minutes = '20', workers = '1', batchSize = '20' }, dependencies = {}) {
  const { privateData, candidateHash, auditHash } = readReviewedCandidate({ candidate, audit, clearance, clearanceSha256 });
  if (!model?.trim() || !process.env.OPENAI_API_KEY?.trim()) {
    throw Error('OPENAI_MODEL and OPENAI_API_KEY are required; no implicit translation provider');
  }
  for (const [value, name, maximum] of [[entries, 'entries', 1000], [minutes, 'minutes', 40],
    [workers, 'workers', 2], [batchSize, 'batch-size', 40]]) {
    if (!/^[0-9]+$/.test(String(value)) || Number(value) < 1 || Number(value) > maximum) {
      throw Error(`Invalid ${name}`);
    }
  }
  const storePath = privateDirectory(store);
  if ([candidate, audit, clearance].some(file => {
    const target = realpathSync(file);
    return target === storePath || target.startsWith(storePath + sep);
  })) throw Error('Review inputs must not be inside the writable translation store');
  initializeStore(storePath, seedStore, candidateHash, auditHash);
  for (const relative of ['fr', 'ja', 'text-cache', 'fr/questions.json', 'ja/questions.json',
    'text-cache/fr.json', 'text-cache/ja.json', 'report.json']) {
    const target = join(storePath, relative);
    let stat;
    try { stat = lstatSync(target); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (stat?.isSymbolicLink()) throw Error('Translation store cannot contain symlinks');
  }
  const temporary = mkdtempSync(join(storePath, '.questions-translation-'));
  try {
    const data = join(temporary, 'data');
    mkdirSync(join(data, 'questions'), { recursive: true, mode: 0o700 });
    writeFileSync(join(data, 'manifest.json'), JSON.stringify({
      private_preview: true, publish_allowed: false,
      questions_view: { enabled: false, files: { entries: 'questions/entries.json' } },
    }), { mode: 0o600 });
    writeFileSync(join(data, 'questions/entries.json'), JSON.stringify(privateData), { mode: 0o600 });
    const report = await backfill(['--page', 'questions', '--data', data, '--store', storePath, '--locale', 'all',
      '--model', model, '--entries', String(entries), '--minutes', String(minutes), '--workers', String(workers),
      '--batch-size', String(batchSize)], dependencies);
    if (report.errors.length) throw Error(`Translation failed; inspect the private report in ${storePath}`);
    return { candidate_sha256: candidateHash, audit_sha256: auditHash, clearance_sha256: clearanceSha256,
      requested: report.requested, translated: report.translated, model_calls: report.model_calls,
      complete: report.complete, translation_review_required: true, publication_authorized: false };
  } finally {
    rmSync(temporary, { recursive: true });
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const { values } = parseArgs({ options: {
    candidate: { type: 'string' }, audit: { type: 'string' }, clearance: { type: 'string' },
    'clearance-sha256': { type: 'string' }, store: { type: 'string' }, 'seed-store': { type: 'string' },
    model: { type: 'string', default: process.env.OPENAI_MODEL }, entries: { type: 'string' },
    minutes: { type: 'string' }, workers: { type: 'string' }, 'batch-size': { type: 'string' },
  } });
  if (!values.candidate || !values.audit || !values.clearance || !values['clearance-sha256'] || !values.store) {
    throw Error('Candidate, audit, clearance, clearance SHA-256 and private store are required');
  }
  process.umask(0o077);
  translateReviewed({ ...values, clearanceSha256: values['clearance-sha256'], seedStore: values['seed-store'],
    batchSize: values['batch-size'] })
    .then(result => console.log(JSON.stringify(result)))
    .catch(error => { console.error(error.message); process.exitCode = 1; });
}
