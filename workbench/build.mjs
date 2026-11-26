import { build } from 'esbuild';
import { readFile, writeFile, mkdir, readdir, copyFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const output = resolve('../workbench-dist');
const stripTrailingWhitespace = (text) => text.replace(/[ \t]+$/gm, '');
await mkdir(output, { recursive: true });
const ui = await build({ entryPoints: ['app.js'], bundle: true, format: 'esm', platform: 'browser', write: false, minify: true, legalComments: 'inline', metafile: true });
const script = stripTrailingWhitespace(ui.outputFiles[0].text.replace(/<\/script/gi, '<\\/script'));
const template = await readFile('index.html', 'utf8');
const css = '/*\n' + await readFile('DSH-LICENSE.txt', 'utf8') + '\n*/\n' + await readFile('dsh-theme.css', 'utf8') + '\n' + await readFile('style.css', 'utf8');
await copyFile('../assets/branding/opl-relay-logo.png', resolve(output, '../assets/icon.png'));
const logo = await readFile(resolve(output, '../assets/icon.png'));
await writeFile(resolve(output, 'index.html'), template.replace('/* LOGO */', () => `data:image/png;base64,${logo.toString('base64')}`).replace('/* STYLE */', () => css).replace('/* SCRIPT */', () => script));
await build({ entryPoints: ['server.mjs'], outfile: resolve(output, 'server.mjs'), bundle: true, platform: 'node', format: 'esm', target: 'node20', minify: true, legalComments: 'inline', banner: { js: 'import { createRequire as _createRequire } from "node:module"; const require = _createRequire(import.meta.url);' } });
await writeFile(resolve(output, 'server.mjs'), stripTrailingWhitespace(await readFile(resolve(output, 'server.mjs'), 'utf8')));
const lock = JSON.parse(await readFile('package-lock.json', 'utf8'));
let notices = 'OPL Relay Workbench third-party notices\n\n';
for (const [path, dependency] of Object.entries(lock.packages)) {
  if (!path || dependency.dev) continue;
  const root = resolve(path);
  const pkg = JSON.parse(await readFile(resolve(root, 'package.json'), 'utf8'));
  let license = '';
  for (const filename of (await readdir(root)).filter((name) => /^(licen[sc]e|copying|notice)(\.|$)/i.test(name))) {
    try { license += await readFile(resolve(root, filename), 'utf8') + '\n'; } catch {}
  }
  notices += `${pkg.name} ${pkg.version} (${pkg.license || dependency.license || 'See upstream'})\n${license || JSON.stringify(pkg.repository || '')}\n\n`;
}
await writeFile(resolve(output, 'THIRD_PARTY_NOTICES.txt'), notices);
