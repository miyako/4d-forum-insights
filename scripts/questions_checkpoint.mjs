import { createCipheriv, createDecipheriv, createHash, randomBytes, scryptSync } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, realpathSync, statSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { gzipSync, gunzipSync } from 'node:zlib';

const MAGIC = Buffer.from('QCKPT001');
const SQLITE = Buffer.from('SQLite format 3\0');
const LIMIT = 512 * 1024 * 1024;
const HEADER_SIZE = MAGIC.length + 16 + 12;

function derive(key, salt) {
  if (typeof key !== 'string' || !key.trim()) throw new Error('DB_KEY is required');
  return scryptSync(key, salt, 32, { N: 32768, r: 8, p: 1, maxmem: 64 * 1024 * 1024 });
}

function database(file) {
  if (existsSync(`${file}-wal`)) throw new Error('Create a standalone SQLite backup without a WAL');
  const stat = statSync(file);
  if (!stat.isFile() || stat.size > LIMIT || stat.size < SQLITE.length) throw new Error('Invalid SQLite snapshot size');
  const bytes = readFileSync(file);
  if (!bytes.subarray(0, SQLITE.length).equals(SQLITE)) throw new Error('Invalid SQLite snapshot');
  return bytes;
}

export function packCheckpoint({ source, ratings, output, key }) {
  source = realpathSync(source); ratings = realpathSync(ratings); output = resolve(output);
  const a = statSync(source), b = statSync(ratings);
  if (source === ratings || a.dev === b.dev && a.ino === b.ino) throw new Error('Snapshots must be separate');
  if (output === source || output === ratings) throw new Error('Checkpoint must not overwrite an input');
  const sourceBytes = database(source), ratingBytes = database(ratings);
  if (sourceBytes.length + ratingBytes.length + 8 > LIMIT) throw new Error('Checkpoint exceeds the 512 MiB limit');
  const size = Buffer.alloc(8);
  size.writeBigUInt64BE(BigInt(sourceBytes.length));
  const payload = gzipSync(Buffer.concat([size, sourceBytes, ratingBytes]));
  const salt = randomBytes(16), nonce = randomBytes(12);
  const header = Buffer.concat([MAGIC, salt, nonce]);
  const cipher = createCipheriv('aes-256-gcm', derive(key, salt), nonce);
  cipher.setAAD(header);
  const encrypted = Buffer.concat([header, cipher.update(payload), cipher.final(), cipher.getAuthTag()]);
  writeFileSync(output, encrypted, { flag: 'wx', mode: 0o600 });
  return { sha256: createHash('sha256').update(encrypted).digest('hex'), bytes: encrypted.length };
}

export function unpackCheckpoint({ input, output, key }) {
  input = resolve(input); output = resolve(output);
  if (existsSync(output)) throw new Error('Checkpoint output directory already exists');
  if (statSync(input).size > LIMIT + 1024 * 1024) throw new Error('Invalid checkpoint format or size');
  const encrypted = readFileSync(input);
  if (encrypted.length < HEADER_SIZE + 16 || !encrypted.subarray(0, MAGIC.length).equals(MAGIC)
    || encrypted.length > LIMIT + 1024 * 1024) throw new Error('Invalid checkpoint format or size');
  const header = encrypted.subarray(0, HEADER_SIZE);
  const salt = header.subarray(MAGIC.length, MAGIC.length + 16);
  const nonce = header.subarray(MAGIC.length + 16);
  const decipher = createDecipheriv('aes-256-gcm', derive(key, salt), nonce);
  decipher.setAAD(header);
  decipher.setAuthTag(encrypted.subarray(-16));
  let payload;
  try {
    payload = Buffer.concat([decipher.update(encrypted.subarray(HEADER_SIZE, -16)), decipher.final()]);
  } catch {
    throw new Error('Checkpoint authentication failed');
  }
  const plain = gunzipSync(payload, { maxOutputLength: LIMIT });
  if (plain.length < 8) throw new Error('Invalid checkpoint payload');
  const sourceLength = plain.readBigUInt64BE();
  if (sourceLength > BigInt(plain.length - 8)) throw new Error('Invalid checkpoint lengths');
  const sourceBytes = plain.subarray(8, 8 + Number(sourceLength));
  const ratingBytes = plain.subarray(8 + Number(sourceLength));
  if (!sourceBytes.subarray(0, SQLITE.length).equals(SQLITE) || !ratingBytes.subarray(0, SQLITE.length).equals(SQLITE)) {
    throw new Error('Invalid checkpoint SQLite snapshots');
  }
  mkdirSync(output, { mode: 0o700 });
  writeFileSync(resolve(output, 'source.db'), sourceBytes, { flag: 'wx', mode: 0o600 });
  writeFileSync(resolve(output, 'ratings.db'), ratingBytes, { flag: 'wx', mode: 0o600 });
  return { sha256: createHash('sha256').update(encrypted).digest('hex'), bytes: encrypted.length };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    source: { type: 'string' }, ratings: { type: 'string' }, input: { type: 'string' }, output: { type: 'string' },
  } });
  const key = process.env.DB_KEY;
  if (positionals.length !== 1 || !values.output) throw new Error('Expected pack or unpack and --output');
  if (positionals[0] === 'pack' && values.source && values.ratings && !values.input) {
    console.log(JSON.stringify(packCheckpoint({ ...values, key })));
  } else if (positionals[0] === 'unpack' && values.input && !values.source && !values.ratings) {
    console.log(JSON.stringify(unpackCheckpoint({ ...values, key })));
  } else throw new Error('Use pack --source FILE --ratings FILE --output FILE, or unpack --input FILE --output DIR');
}
