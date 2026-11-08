import { build } from 'esbuild';
import { readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';

const output = resolve('../plugins/opl-relay/workbench');
await mkdir(output, { recursive: true });
const ui = await build({ entryPoints: ['app.js'], bundle: true, format: 'esm', platform: 'browser', write: false, minify: true, legalComments: 'inline', metafile: true });
const script = ui.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const template = await readFile('index.html', 'utf8');
const css = await readFile('style.css', 'utf8');
const logo = await readFile(resolve(output, '../assets/icon.png'));
await writeFile(resolve(output, 'index.html'), template.replace('/* LOGO */', () => `data:image/png;base64,${logo.toString('base64')}`).replace('/* STYLE */', () => css).replace('/* SCRIPT */', () => script));
await build({ entryPoints: ['server.mjs'], outfile: resolve(output, 'server.mjs'), bundle: true, platform: 'node', format: 'esm', target: 'node20', minify: true, legalComments: 'inline', banner: { js: 'import { createRequire as _createRequire } from "node:module"; const require = _createRequire(import.meta.url);' } });
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
const manifestPath = resolve(output, '../opl-package.json');
const manifestText = await readFile(manifestPath, 'utf8');
const manifest = JSON.parse(manifestText);
const digest = createHash('sha256');
for (const path of manifest.content_lock.paths) {
  const pathBytes = Buffer.from(path, 'utf8');
  const fileBytes = await readFile(resolve(output, '..', path));
  for (const content of [pathBytes, fileBytes]) {
    const length = Buffer.alloc(8); length.writeBigUInt64BE(BigInt(content.length));
    digest.update(length); digest.update(content);
  }
}
const locked = `sha256:${digest.digest('hex')}`;
await writeFile(manifestPath, manifestText.replace(`"digest": "${manifest.content_lock.digest}"`, `"digest": "${locked}"`));
