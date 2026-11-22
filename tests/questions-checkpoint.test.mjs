import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { packCheckpoint, unpackCheckpoint } from '../scripts/questions_checkpoint.mjs';

function fixture() {
  mkdirSync('build', { recursive: true });
  const root = mkdtempSync(resolve('build/questions-checkpoint-test-'));
  const source = resolve(root, 'source.db'), ratings = resolve(root, 'ratings.db');
  for (const file of [source, ratings]) {
    const result = spawnSync('python3', ['-c', `
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("CREATE TABLE sample (text TEXT)")
    db.execute("INSERT INTO sample VALUES ('PRIVATE THREAD CONTENT')")
`, file], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
  }
  return { root, source, ratings, cleanup: () => rmSync(root, { recursive: true }) };
}

test('authenticated checkpoint round-trips both private snapshots without exposing plaintext', () => {
  const f = fixture();
  try {
    const output = resolve(f.root, 'checkpoint.enc'), restored = resolve(f.root, 'restored');
    const saved = packCheckpoint({ ...f, output, key: 'test-secret' });
    assert.equal(statSync(output).mode & 0o777, 0o600);
    assert.equal(readFileSync(output).includes(Buffer.from('PRIVATE THREAD CONTENT')), false);
    const loaded = unpackCheckpoint({ input: output, output: restored, key: 'test-secret' });
    assert.deepEqual(loaded, saved);
    assert.equal(statSync(restored).mode & 0o777, 0o700);
    assert.deepEqual(readFileSync(resolve(restored, 'source.db')), readFileSync(f.source));
    assert.deepEqual(readFileSync(resolve(restored, 'ratings.db')), readFileSync(f.ratings));
    assert.throws(() => unpackCheckpoint({ input: output, output: restored, key: 'test-secret' }), /already exists/);
  } finally { f.cleanup(); }
});

test('wrong keys and tampering never expose restored snapshots', () => {
  const f = fixture();
  try {
    const output = resolve(f.root, 'checkpoint.enc'), restored = resolve(f.root, 'restored');
    packCheckpoint({ ...f, output, key: 'test-secret' });
    assert.throws(() => unpackCheckpoint({ input: output, output: restored, key: 'wrong' }), /authentication failed/);
    const bytes = readFileSync(output);
    bytes[bytes.length - 17] ^= 1;
    writeFileSync(output, bytes);
    assert.throws(() => unpackCheckpoint({ input: output, output: restored, key: 'test-secret' }), /authentication failed/);
    assert.throws(() => statSync(restored), /ENOENT/);
  } finally { f.cleanup(); }
});

test('checkpoint rejects live WALs, identical databases, and overwrites', () => {
  const f = fixture();
  try {
    const output = resolve(f.root, 'checkpoint.enc');
    assert.throws(() => packCheckpoint({ ...f, ratings: f.source, output, key: 'secret' }), /separate/);
    writeFileSync(`${f.source}-wal`, '');
    assert.throws(() => packCheckpoint({ ...f, output, key: 'secret' }), /without a WAL/);
    rmSync(`${f.source}-wal`);
    writeFileSync(output, 'existing');
    assert.throws(() => packCheckpoint({ ...f, output, key: 'secret' }), /EEXIST/);
    assert.equal(readFileSync(output, 'utf8'), 'existing');
  } finally { f.cleanup(); }
});
