import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto';
import { lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { gzipSync, gunzipSync } from 'node:zlib';
import { privateDirectory } from './questions_translate_review.mjs';

const HEADER = Buffer.from('questions-release-aes256gcm-v1\n');
const MAX_BYTES = 384 * 1024 * 1024;
const allowed = name => /^(?:candidate\.json|audit\.json|translation-clearance\.json|field-review-gate\.json|release-bindings\.json|source-selection\.json|translation-summary\.json|(?:localization|translation-store)\/(?:\.questions-review-store\.json|report\.json|(?:fr|ja)\/questions\.json|text-cache\/(?:fr|ja)\.json))$/.test(name);
const digest = value => createHash('sha256').update(value).digest('hex');

function keyBytes(key) {
  if (typeof key !== 'string' || !/^[a-f0-9]{64}$/.test(key.trim())) throw Error('A 256-bit release transfer key is required');
  return Buffer.from(key.trim(), 'hex');
}

export function sealRelease(directory, output, key) {
  const root = privateDirectory(directory), files = [];
  let total = 0;
  const walk = (relative = '') => {
    for (const entry of readdirSync(join(root, relative), { withFileTypes: true })) {
      const name = relative ? `${relative}/${entry.name}` : entry.name;
      const path = join(root, name), stat = lstatSync(path);
      if (stat.isSymbolicLink() || stat.uid !== process.getuid() || (stat.mode & 0o077)) throw Error('Release inputs must be private regular files/directories');
      if (stat.isDirectory()) walk(name);
      else {
        if (!stat.isFile() || !allowed(name)) throw Error('Unexpected release bundle file');
        const bytes = readFileSync(path);
        total += bytes.length;
        if (total > MAX_BYTES || files.length >= 32) throw Error('Release bundle exceeds its size limit');
        files.push({ name, sha256: digest(bytes), data: bytes.toString('base64') });
      }
    }
  };
  walk();
  if (!files.length) throw Error('Empty release bundle');
  const payload = gzipSync(Buffer.from(JSON.stringify({ schema: 'questions-release-transfer-v1', files })));
  const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', keyBytes(key), iv);
  cipher.setAAD(HEADER);
  const ciphertext = Buffer.concat([cipher.update(payload), cipher.final()]);
  const result = Buffer.concat([HEADER, iv, cipher.getAuthTag(), ciphertext]);
  privateDirectory(dirname(resolve(output)));
  writeFileSync(output, result, { flag: 'wx', mode: 0o600 });
  return { sha256: digest(result), files: files.length };
}

export function openRelease(input, directory, key) {
  const bytes = readFileSync(input);
  if (bytes.length > MAX_BYTES || bytes.length < HEADER.length + 29 || !bytes.subarray(0, HEADER.length).equals(HEADER)) {
    throw Error('Invalid encrypted release envelope');
  }
  const start = HEADER.length;
  const decipher = createDecipheriv('aes-256-gcm', keyBytes(key), bytes.subarray(start, start + 12));
  decipher.setAAD(HEADER);
  decipher.setAuthTag(bytes.subarray(start + 12, start + 28));
  const compressed = Buffer.concat([decipher.update(bytes.subarray(start + 28)), decipher.final()]);
  const bundle = JSON.parse(gunzipSync(compressed, { maxOutputLength: MAX_BYTES * 1.5 }).toString('utf8'));
  if (bundle.schema !== 'questions-release-transfer-v1' || !Array.isArray(bundle.files)
      || !bundle.files.length || bundle.files.length > 32) throw Error('Invalid release bundle');
  const names = new Set(), files = [];
  let total = 0;
  for (const file of bundle.files) {
    if (!allowed(file.name) || names.has(file.name) || typeof file.data !== 'string') throw Error('Invalid release member');
    names.add(file.name);
    const data = Buffer.from(file.data, 'base64');
    total += data.length;
    if (total > MAX_BYTES || digest(data) !== file.sha256) throw Error('Release member checksum/size mismatch');
    files.push({ name: file.name, data });
  }
  privateDirectory(dirname(resolve(directory)));
  mkdirSync(directory, { mode: 0o700 });
  for (const file of files) {
    const path = join(directory, file.name);
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    writeFileSync(path, file.data, { flag: 'wx', mode: 0o600 });
  }
  return { files: files.length, sha256: digest(bytes) };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  process.umask(0o077);
  const { values } = parseArgs({ options: {
    operation: { type: 'string' }, directory: { type: 'string' }, file: { type: 'string' },
    'key-file': { type: 'string' },
  } });
  const key = values['key-file'] ? readFileSync(values['key-file'], 'utf8') : process.env.QUESTIONS_RELEASE_TRANSFER_KEY;
  if (!values.directory || !values.file || !['seal', 'open'].includes(values.operation)) throw Error('Expected operation seal/open, directory and file');
  const result = values.operation === 'seal'
    ? sealRelease(values.directory, values.file, key) : openRelease(values.file, values.directory, key);
  console.log(JSON.stringify(result));
}
