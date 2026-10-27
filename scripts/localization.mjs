import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const VERSION = '1';
export const LOCALES = ['fr', 'ja'];
export const PAGES = ['voices', 'tips'];
export const FIELDS = { tips: ['headline', 'body'], voices: ['question', 'positions', 'outcome'] };

export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function sourceHash(fields) {
  return createHash('sha256').update(canonical(fields)).digest('hex');
}

export function recordsFromColumns(page, data) {
  if (!PAGES.includes(page) || !Array.isArray(data.ids)) throw Error('Invalid source page');
  for (const key of FIELDS[page]) {
    if (!Array.isArray(data[key]) || data[key].length !== data.ids.length) throw Error(`Invalid source column ${key}`);
  }
  const seen = new Set();
  return data.ids.map((id, i) => {
    if (!Number.isSafeInteger(id) || id <= 0 || seen.has(id)) throw Error(`Invalid/duplicate topic ID ${id}`);
    seen.add(id);
    const fields = Object.fromEntries(FIELDS[page].map(k => [k, data[k][i]]));
    validateShape(page, fields);
    return { id, source_hash: sourceHash(fields), fields };
  });
}

export function validateShape(page, fields, source) {
  if (!fields || canonical(Object.keys(fields).sort()) !== canonical([...FIELDS[page]].sort())) {
    throw Error(`Unexpected ${page} translation fields`);
  }
  for (const key of FIELDS[page]) {
    const value = fields[key];
    if (key === 'positions') {
      if (!Array.isArray(value) || value.length < 1 || value.some(v => typeof v !== 'string' || !v.trim())) {
        throw Error('Invalid positions');
      }
      if (source && value.length !== source[key].length) throw Error('Changed position count');
    } else if (typeof value !== 'string' || !value.trim()) throw Error(`Invalid ${key}`);
  }
}

// Mask code, links and technical identifiers before translation, then restore them verbatim.
const PROTECTED = /```[\s\S]*?```|`[^`\n]+`|https?:\/\/[^\s<>")]*|\{[A-Za-z_][\w.]*\}|%[sdif]|\$\w+(?:\.\w+|\[(?:"[^"\n]*"|'[^'\n]*'|\d+)\])*|\b(?:4D(?: Write Pro| View Pro| Internet Commands| Server| Mobile| REST API)?|ORDA|Qodly|SQL|JSON|XML|HTTP[S]?|API|macOS|Windows|Linux|JavaScript|TypeScript)\b|`/g;
export function protect(fields) {
  const tokens = [];
  const mask = text => {
    if (/__KEEP_\d+__/.test(text)) throw Error('Reserved placeholder in source');
    return text.replace(PROTECTED, text => {
      const marker = `__KEEP_${tokens.length}__`;
      tokens.push({ marker, text });
      return marker;
    });
  };
  return {
    fields: Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, Array.isArray(v) ? v.map(mask) : mask(v)])),
    tokens,
  };
}

export function restore(page, fields, masked) {
  validateShape(page, fields, masked.fields);
  const unmask = (text, source) => {
    const expected = source.match(/__KEEP_\d+__/g) || [];
    const actual = text.match(/__KEEP_\d+__/g) || [];
    if (canonical([...expected].sort()) !== canonical([...actual].sort())) throw Error('Changed protected placeholders');
    if (/https?:\/\/|```|`/.test(text)) throw Error('Unprotected code/link introduced');
    return text.replace(/__KEEP_\d+__/g, marker => {
      const token = masked.tokens.find(t => t.marker === marker);
      if (!token) throw Error(`Unknown placeholder ${marker}`);
      return token.text;
    });
  };
  return Object.fromEntries(Object.entries(fields).map(([k, v]) => [k,
    Array.isArray(v) ? v.map((s, i) => unmask(s, masked.fields[k][i])) : unmask(v, masked.fields[k])]));
}

export function emptyOverlay(page, locale) {
  return { version: 1, locale, page, entries: {} };
}

export function validateOverlay(overlay, page, locale) {
  if (overlay.version !== 1 || overlay.locale !== locale || overlay.page !== page ||
      !overlay.entries || typeof overlay.entries !== 'object' || Array.isArray(overlay.entries)) {
    throw Error(`Invalid overlay ${locale}/${page}`);
  }
  for (const [id, entry] of Object.entries(overlay.entries)) {
    if (!/^[1-9]\d*$/.test(id) || !Number.isSafeInteger(Number(id)) ||
        !/^[a-f0-9]{64}$/.test(entry.source_hash) ||
        typeof entry.translation_version !== 'string' || !/^[A-Za-z0-9._-]{1,64}$/.test(entry.translation_version)) {
      throw Error(`Invalid translation identity ${locale}/${page}/${id}`);
    }
    validateShape(page, entry.fields);
  }
  return overlay;
}

export function readOverlay(root, page, locale) {
  const file = path.join(root, locale, `${page}.json`);
  if (!fs.existsSync(file)) return emptyOverlay(page, locale);
  return validateOverlay(JSON.parse(fs.readFileSync(file, 'utf8')), page, locale);
}

export function writeOverlay(root, overlay) {
  validateOverlay(overlay, overlay.page, overlay.locale);
  const file = path.join(root, overlay.locale, `${overlay.page}.json`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(overlay) + '\n');
  fs.renameSync(temp, file);
}

export function coverage(records, overlay) {
  let current = 0, stale = 0;
  for (const record of records) {
    const entry = overlay.entries[record.id];
    if (entry?.source_hash === record.source_hash && entry.translation_version === VERSION) current++;
    else if (entry) stale++;
  }
  return { total: records.length, current, stale, missing: records.length - current - stale };
}

export function makeBatch(page, locale, records) {
  const batch = {
    version: 1, translation_version: VERSION, page, locale,
    records: records.map(record => {
      const masked = protect(record.fields);
      restore(page, masked.fields, masked);
      return { id: record.id, source_hash: record.source_hash, fields: masked.fields };
    }),
  };
  return { ...batch, batch_id: sourceHash(batch) };
}

export function importBatch(overlay, sourceRecords, batch, response) {
  if (batch.version !== 1 || batch.page !== overlay.page || batch.locale !== overlay.locale ||
      batch.translation_version !== VERSION || !Array.isArray(batch.records)) {
    throw Error('Batch identity mismatch');
  }
  if (!Array.isArray(response.records) || response.records.length !== batch.records.length) {
    throw Error('Translation batch incomplete');
  }
  const { batch_id: batchId, ...content } = batch;
  if (batchId !== sourceHash(content)) throw Error('Batch integrity mismatch');
  if (response.batch_id !== batchId) throw Error('Response batch identity mismatch');
  const sources = new Map(sourceRecords.map(r => [r.id, r]));
  const requested = new Map(batch.records.map(r => [r.id, r]));
  if (requested.size !== batch.records.length) throw Error('Duplicate requested translation ID');
  const additions = {};
  for (const translated of response.records) {
    const source = sources.get(translated.id), request = requested.get(translated.id);
    const responseBound = translated.source_hash === undefined || translated.source_hash === source?.source_hash;
    if (!source || !request || additions[translated.id] || !responseBound ||
        request.source_hash !== source.source_hash) throw Error('Unknown, duplicate or stale translation ID/hash');
    const masked = protect(source.fields);
    if (canonical(request.fields) !== canonical(masked.fields)) throw Error('Changed source batch fields');
    const fields = restore(overlay.page, translated.fields, masked);
    additions[translated.id] = { source_hash: source.source_hash, translation_version: VERSION, fields };
  }
  // Validate the entire batch before changing the checkpoint.
  Object.assign(overlay.entries, additions);
  return additions;
}

export function persistBatch(root, overlay, sourceRecords, batch, response) {
  const candidate = { ...overlay, entries: { ...overlay.entries } };
  const additions = importBatch(candidate, sourceRecords, batch, response);
  writeOverlay(root, candidate);
  overlay.entries = candidate.entries;
  return additions;
}
