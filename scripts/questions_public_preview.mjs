import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { buildQuestions, questionsEnabled, questionsPublicPreview, COVERAGE, IMPACT, COMPLEXITY, DOMAINS } from '../site/js/questions-core.js';

const ROW_FIELDS = ['id', 'title', 'body', 'active', 'lang', 'accepted', 'closed', 'replies', 'answers', 'evidence_warnings', 'decision_model', 'decision_version'];
const CHOICES = { coverage: COVERAGE, impact: IMPACT, technicality: COMPLEXITY };
const BINARY = ['has_question', 'clarification', 'version_dependent', ...Object.keys(DOMAINS).map((k) => 'domain_' + k)];

export function validatePublicPreview(data) {
  if (data?.schema_version !== 'questions-ui-public-preview-v1' || data.version !== 1
    || data.public_preview !== true || data.private_preview !== false || data.publish_allowed !== true
    || data.representative !== false || !Array.isArray(data.entries) || data.entries.length < 1 || data.entries.length > 60
    || typeof data.generated_at !== 'string' || !Number.isFinite(Date.parse(data.generated_at))) {
    throw new Error('Expected a separately publication-approved, small public Questions preview');
  }
  // Strip every root/row field not used by the public UI, including private approval/runtime provenance.
  const result = {
    schema_version: data.schema_version, version: 1, public_preview: true, private_preview: false,
    publish_allowed: true, representative: false, generated_at: data.generated_at,
    entries: data.entries.map((row) => {
      const entry = Object.fromEntries(ROW_FIELDS.filter((k) => Object.hasOwn(row, k)).map((k) => [k, row[k]]));
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
      return entry;
    }),
  };
  if (!buildQuestions(result).length) throw new Error('Public preview contains no classified questions');
  return result;
}

export function installPublicPreview(source, directory) {
  const data = validatePublicPreview(JSON.parse(readFileSync(source, 'utf8')));
  const manifestFile = resolve(directory, 'manifest.json');
  const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'));
  if (manifest.private_preview === true || manifest.publish_allowed === false) throw new Error('Cannot publish a private site manifest');
  if (!Array.isArray(manifest.questions)) throw new Error('Requests classification array must be preserved');
  if (questionsEnabled(manifest)) throw new Error('Refusing to replace an enabled production Questions dataset with a sample');
  mkdirSync(resolve(directory, 'questions'), { recursive: true });
  writeFileSync(resolve(directory, 'questions/preview.json'), JSON.stringify(data));
  manifest.questions_view = {
    enabled: false, preview: true, public_preview: true, semantic_enabled: false,
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
  if (args.length !== 4 || args[0] !== '--source' || args[2] !== '--data') {
    throw new Error('Usage: node scripts/questions_public_preview.mjs --source FILE --data DIRECTORY');
  }
  installPublicPreview(args[1], args[3]);
  console.log('Publication-approved Questions sample installed; production rollout and semantic search remain disabled');
}
