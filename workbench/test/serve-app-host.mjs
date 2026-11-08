import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { build } from 'esbuild';
import { createServer } from 'node:http';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

const profile = await mkdtemp(join(tmpdir(), 'relay-app-host-'));
const seed = spawnSync('python3', ['test/seed_profile.py', profile], { encoding: 'utf8' });
if (seed.status !== 0) throw new Error(seed.stderr);
const client = new Client({ name: 'Relay SDK qualification host', version: '1.0.0' }, { capabilities: { extensions: { 'io.modelcontextprotocol/ui': { mimeTypes: ['text/html;profile=mcp-app'] } } } });
await client.connect(new StdioClientTransport({ command: 'python3', args: ['../plugins/opl-relay/bin/opl-relay', '--json', 'workbench', 'mcp'], env: { ...process.env, OPL_PROFILE_WORKSPACE: profile } }));
const resource = await client.readResource({ uri: 'ui://opl-relay/mail-workbench.html' });
const bundle = await build({ entryPoints: ['test/app-host.js'], bundle: true, write: false, format: 'esm', platform: 'browser' });
const allowed = new Set(['open_workbench', 'relay_accounts', 'relay_messages', 'relay_read', 'relay_reply', 'relay_draft', 'relay_save', 'relay_drafts', 'relay_upload', 'relay_uploaded_attachment', 'relay_attachment']);
const server = createServer(async (request, response) => {
  if (request.url === '/view') {
    response.setHeader('Content-Type', 'text/html; charset=utf-8'); response.end(resource.contents[0].text); return;
  }
  if (request.url === '/host.js') {
    response.setHeader('Content-Type', 'text/javascript'); response.end(bundle.outputFiles[0].text); return;
  }
  if (request.url === '/tool' && request.method === 'POST') {
    try {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const input = JSON.parse(Buffer.concat(chunks));
      if (!allowed.has(input.name)) throw new Error('Outside controlled qualification');
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify(await client.callTool(input)));
    } catch (error) { response.writeHead(400).end(JSON.stringify({ error: error.message })); }
    return;
  }
  response.setHeader('Content-Type', 'text/html; charset=utf-8');
  response.end('<!doctype html><html><head><title>Relay standard MCP App qualification</title><style>body{margin:0}iframe{border:0;width:100%;height:100vh;display:block}</style></head><body><iframe title="Relay standard MCP App" sandbox="allow-scripts"></iframe><script type="module" src="/host.js"></script></body></html>');
});
server.listen(0, '127.0.0.1', async () => {
  const url = `http://127.0.0.1:${server.address().port}/`;
  if (process.argv[2]) await writeFile(process.argv[2], url, { mode: 0o600 });
  process.stdout.write('Controlled MCP App host ready.\n');
});
async function close() {
  await client.close();
  await new Promise(accept => server.close(accept));
  await rm(profile, { recursive: true, force: true });
  process.exit(0);
}
process.once('SIGTERM', close);
process.once('SIGINT', close);
