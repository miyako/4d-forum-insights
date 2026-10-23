// Copy browser dependencies from node_modules into site/vendor (committed, so the site needs no build step).
import fs from 'node:fs';
const pkg = JSON.parse(fs.readFileSync('node_modules/minisearch/package.json', 'utf8'));
fs.mkdirSync('site/vendor', { recursive: true });
const src = fs.readFileSync('node_modules/minisearch/dist/es/index.js', 'utf8').replace(/\n\/\/# sourceMappingURL=.*$/m, '');
fs.writeFileSync('site/vendor/minisearch.js', `/*! MiniSearch ${pkg.version} | MIT License | https://github.com/lucaong/minisearch */\n` + src);
fs.copyFileSync('node_modules/minisearch/LICENSE.txt', 'site/vendor/minisearch.LICENSE.txt');
console.log(`vendored minisearch ${pkg.version}`);
