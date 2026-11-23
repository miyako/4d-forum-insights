import { mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { buildQuestions, questionDomains, questionsEnabled, questionsPublicPreview, COVERAGE, IMPACT, COMPLEXITY, DOMAINS } from '../site/js/questions-core.js';
import { Semantic } from '../site/js/semantic.js';

const ROW_FIELDS = ['id', 'title', 'body', 'active', 'lang', 'accepted', 'closed', 'replies', 'answers', 'evidence_warnings', 'decision_model', 'decision_version'];
const CHOICES = { coverage: COVERAGE, impact: IMPACT, technicality: COMPLEXITY };
const BINARY = ['has_question', 'clarification', 'version_dependent', ...Object.keys(DOMAINS).map((k) => 'domain_' + k)];
const COMPLETE_WARNINGS = new Set([
  'Code-dependent understanding is limited because code was replaced by placeholders during classification.',
  'Some listed posts were unavailable in the source snapshot.',
  'Fresh English opening was unavailable during classification.',
  'A source fetch error was recorded; available evidence may be incomplete.',
  'The reply count is unknown.',
]);

export function validatePublicPreview(data) {
  const processed = data?.schema_version === 'questions-ui-public-processed-preview-v1';
  const complete = data?.schema_version === 'questions-ui-public-complete-v1';
  if ((!processed && !complete && data?.schema_version !== 'questions-ui-public-preview-v1') || data.version !== 1
    || data.public_preview !== true || data.private_preview !== false || data.publish_allowed !== true
    || data.representative !== false || !Array.isArray(data.entries) || data.entries.length < 1
    || (!processed && !complete && data.entries.length > 60)
    || (processed && (data.snapshot_scope !== 'processed' || data.corpus_complete !== false
      || data.processed_count !== data.entries.length || !Number.isSafeInteger(data.corpus_total)
      || data.corpus_total <= data.processed_count))
    || (complete && (data.snapshot_scope !== 'processed' || data.corpus_complete !== true
      || data.processed_count !== data.entries.length || !Number.isSafeInteger(data.corpus_total)
      || data.corpus_total !== data.processed_count
      || typeof data.source_selected_at !== 'string'
      || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(data.source_selected_at)
      || !Number.isFinite(Date.parse(data.source_selected_at))))
    || typeof data.generated_at !== 'string' || !Number.isFinite(Date.parse(data.generated_at))) {
    throw new Error('Expected a separately publication-approved Questions preview with explicit scope');
  }
  // Strip every root/row field not used by the public UI, including private approval/runtime provenance.
  const result = {
    schema_version: data.schema_version, version: 1, public_preview: true, private_preview: false,
    publish_allowed: true, representative: false, generated_at: data.generated_at,
    ...((processed || complete) ? { snapshot_scope: 'processed', processed_count: data.processed_count,
      corpus_total: data.corpus_total, corpus_complete: complete,
      ...(complete ? { source_selected_at: data.source_selected_at } : {}) } : {}),
    entries: data.entries.map((row) => {
      const entry = Object.fromEntries(ROW_FIELDS.filter((k) => Object.hasOwn(row, k)
        && (!complete || !['decision_model', 'decision_version'].includes(k))).map((k) => [k, row[k]]));
      const answers = row.answers || {};
      entry.answers = Object.fromEntries([
        ...Object.entries(CHOICES).map(([key, labels]) => [key, {
          choice: answers[key]?.choice,
          probabilities: Object.fromEntries(Object.keys(labels).map((k) => [k, answers[key]?.probabilities?.[k]])),
        }]),
        ...BINARY.filter((key) => Object.hasOwn(answers, key)).map((key) => [key, { noul: answers[key]?.noul }]),
      ]);
      if (entry.evidence_warnings != null && (!Array.isArray(entry.evidence_warnings)
        || entry.evidence_warnings.some((v) => typeof v !== 'string'))) throw new Error('Invalid public preview evidence warnings');
      if (complete && (!Array.isArray(entry.evidence_warnings)
        || entry.evidence_warnings.some((v) => !COMPLETE_WARNINGS.has(v))
        || typeof entry.lang !== 'string' || !/^(?:[a-z]{2,3}|unknown)$/.test(entry.lang))) {
        throw new Error('Invalid complete-snapshot warnings or language');
      }
      if (complete && (!Number.isFinite(entry.answers.version_dependent?.noul)
        || entry.answers.version_dependent.noul < 0 || entry.answers.version_dependent.noul > 1)) {
        throw new Error('Invalid complete-snapshot version probability');
      }
      return entry;
    }),
  };
  const questions = buildQuestions(result).length;
  if (!questions) throw new Error('Public preview contains no classified questions');
  if ((processed || complete) && data.eligible_question_count !== questions) throw new Error('Incorrect eligible question count');
  if (complete) result.entries.forEach(entry => { entry.domains = questionDomains(entry.answers); });
  if (processed || complete) result.eligible_question_count = questions;
  return result;
}

function validateSemanticIndex(data, manifest, directory) {
  const meta = manifest.embeddings;
  if (!new Semantic(meta).available) throw new Error('Complete Questions requires a compatible semantic index');
  const asset = (name) => {
    const path = typeof name === 'string' ? name.split('?')[0] : '';
    if (!/^[\w./-]+$/.test(path) || path.startsWith('/') || path.split('/').some(part => !part || part === '.' || part === '..')) {
      throw new Error('Invalid semantic index asset path');
    }
    return resolve(directory, path);
  };
  const ids = JSON.parse(readFileSync(asset(meta.ids), 'utf8'));
  if (!Array.isArray(ids) || !Number.isSafeInteger(meta.count) || ids.length !== meta.count
    || ids.some(id => !Number.isSafeInteger(id) || id <= 0) || new Set(ids).size !== ids.length
    || statSync(asset(meta.bin)).size !== ids.length * meta.dim) {
    throw new Error('Invalid semantic index IDs or vector size');
  }
  const indexed = new Set(ids);
  const missing = buildQuestions(data).filter(question => !indexed.has(question.id)).length;
  if (missing) throw new Error(`Semantic index is missing ${missing} eligible Questions topics`);
}

export function installPublicPreview(source, directory, { allowComplete = false } = {}) {
  const data = validatePublicPreview(JSON.parse(readFileSync(source, 'utf8')));
  if (data.corpus_complete === true && !allowComplete) throw new Error('Complete Questions snapshot is not enabled');
  const manifestFile = resolve(directory, 'manifest.json');
  const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'));
  if (manifest.private_preview === true || manifest.publish_allowed === false) throw new Error('Cannot publish a private site manifest');
  if (!Array.isArray(manifest.questions)) throw new Error('Requests classification array must be preserved');
  if (questionsEnabled(manifest)) throw new Error('Refusing to replace an enabled production Questions dataset with a sample');
  if (data.corpus_complete === true) validateSemanticIndex(data, manifest, directory);
  mkdirSync(resolve(directory, 'questions'), { recursive: true });
  writeFileSync(resolve(directory, 'questions/preview.json'), JSON.stringify(data));
  manifest.questions_view = {
    enabled: false, preview: true, public_preview: true, semantic_enabled: data.corpus_complete === true,
    ...(data.snapshot_scope === 'processed' ? { snapshot_scope: 'processed', processed_count: data.processed_count,
      corpus_total: data.corpus_total, corpus_complete: data.corpus_complete,
      ...(data.corpus_complete ? { source_selected_at: data.source_selected_at } : {}) } : {}),
    files: { entries: 'questions/preview.json' },
  };
  writeFileSync(manifestFile, JSON.stringify(manifest));
}

export function gateQuestionsRoute(directory) {
  const manifest = JSON.parse(readFileSync(resolve(directory, 'manifest.json'), 'utf8'));
  if (manifest.private_preview === true || manifest.publish_allowed === false) throw new Error('Private preview data cannot be deployed');
  return questionsEnabled(manifest) || questionsPublicPreview(manifest);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const args = process.argv.slice(2);
  if (args.length === 2 && args[0] === '--validate') {
    console.log(JSON.stringify(validatePublicPreview(JSON.parse(readFileSync(args[1], 'utf8')))));
  } else {
    if (![4, 5].includes(args.length) || args[0] !== '--source' || args[2] !== '--data'
      || (args.length === 5 && args[4] !== '--allow-complete')) {
      throw new Error('Usage: node scripts/questions_public_preview.mjs --source FILE --data DIRECTORY [--allow-complete]');
    }
    installPublicPreview(args[1], args[3], { allowComplete: args.length === 5 });
    console.log('Publication-approved Questions preview installed');
  }
}
