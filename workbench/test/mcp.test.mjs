import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { once } from 'node:events';
import { runInNewContext } from 'node:vm';
import { actions } from '../../plugins/opl-relay/workbench/server.mjs';

test('save rejects oversized attachment arrays before collecting item errors', () => {
  const valid = actions.save.schema.safeParse({
    account_id: 'demo', draft_id: 'draft', revision: 1, body: '',
    attachment_ids: Array.from({ length: 10 }, (_, index) => `attachment-${index}`),
  });
  assert.equal(valid.success, true);

  const oversized = actions.save.schema.safeParse({
    account_id: 'demo', draft_id: 'draft', revision: 1, body: '',
    attachment_ids: Array(20000).fill(0),
  });
  assert.equal(oversized.success, false);
  assert.deepEqual(oversized.error.issues.map(({ code, path }) => ({ code, path })), [
    { code: 'too_big', path: ['attachment_ids'] },
  ]);
});

test('opaque App sandboxes initialize without browser session storage', async () => {
  const source = (await readFile('app.js', 'utf8')).replace(/^import .*;\n/gm, '');
  let connected = false;
  let reads = 0;
  const surface = {};
  const context = {
    App: class { async connect() { connected = true; } getHostContext() { return {}; } }, DOMPurify: {},
    applyDocumentTheme() {}, applyHostStyleVariables() {},
    document: { documentElement: { dataset: {}, style: {} }, getElementById: () => ({ setAttribute() {} }), querySelectorAll: () => [] },
    window: { self: surface, top: {}, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) },
    location: { search: '', hash: '' }, URLSearchParams,
  };
  Object.defineProperty(context, 'sessionStorage', { get() { reads++; throw new Error('Opaque sandbox has no sessionStorage'); } });
  runInNewContext(source, context);
  await Promise.resolve();
  assert.equal(connected, true);
  assert.equal(reads, 0);
});

test('bundled browser module parses without template replacement corruption', async () => {
  const html = await readFile('../plugins/opl-relay/workbench/index.html', 'utf8');
  const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1];
  const result = spawnSync(process.execPath, ['--check', '--input-type=module'], { input: script, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
});

test('loopback adapter authenticates data and rejects foreign origins and undeclared operations', async () => {
  const profile = await mkdtemp(join(tmpdir(), 'relay-http-test-'));
  const ready = join(profile, 'ready.json');
  const child = spawn(process.execPath, ['../plugins/opl-relay/workbench/server.mjs', '--serve', '--ready-file', ready], {
    env: { ...process.env, OPL_PROFILE_WORKSPACE: profile }, stdio: 'ignore',
  });
  const closed = once(child, 'close');
  try {
    let saved;
    for (let attempt = 0; attempt < 100; attempt++) {
      try { saved = JSON.parse(await readFile(ready, 'utf8')); break; }
      catch { await new Promise((accept) => setTimeout(accept, 50)); }
    }
    assert.ok(saved, 'loopback server must become ready');
    const base = saved.url.split('#')[0];
    const headers = { Authorization: `Bearer ${saved.token}`, 'Content-Type': 'application/json' };
    assert.equal((await fetch(base + 'health')).status, 403);
    assert.equal((await fetch(base + 'health', { headers: { ...headers, Origin: 'https://unrelated.example.test' } })).status, 403);
    const healthy = await fetch(base + 'health', { headers });
    const health = await healthy.json();
    assert.equal(health.profile, resolve(profile));
    assert.equal(health.pid, child.pid);
    assert.match(health.bundle_sha256, /^[a-f0-9]{64}$/);
    assert.equal(health.cli, resolve('../plugins/opl-relay/bin/opl-relay'));
    const accounts = await fetch(base + 'api/accounts', { method: 'POST', headers, body: '{}' });
    assert.deepEqual((await accounts.json()).accounts, []);
    const extra = await fetch(base + 'api/accounts', { method: 'POST', headers, body: '{"password":"rejected"}' });
    assert.equal(extra.status, 400);
    const unknown = await fetch(base + 'api/delete', { method: 'POST', headers, body: '{}' });
    assert.equal(unknown.status, 400);
    const page = await fetch(base);
    assert.match(page.headers.get('content-security-policy'), /frame-ancestors 'none'/);
    assert.match(await page.text(), /OPL Relay/);
  } finally {
    child.kill('SIGTERM');
    await closed;
    await rm(profile, { recursive: true, force: true });
  }
});

test('packaged MCP server exposes a standard App without mailbox side effects', async () => {
  const client = new Client({ name: 'relay-protocol-test', version: '1.0.0' }, { capabilities: { extensions: { 'io.modelcontextprotocol/ui': { mimeTypes: ['text/html;profile=mcp-app'] } } } });
  const transport = new StdioClientTransport({ command: 'python3', args: ['../plugins/opl-relay/bin/opl-relay', '--json', 'workbench', 'mcp'] });
  try {
    await client.connect(transport);
    const { tools } = await client.listTools();
    const open = tools.find((item) => item.name === 'open_workbench');
    assert.equal(open._meta.ui.resourceUri, 'ui://opl-relay/mail-workbench.html');
    assert.equal(open._meta['ui/resourceUri'], open._meta.ui.resourceUri);
    assert.equal(open.annotations.readOnlyHint, true);
    const send = tools.find((item) => item.name === 'relay_send');
    assert.deepEqual(send._meta.ui.visibility, ['app']);
    assert.equal(send.inputSchema.properties.confirmed.const, true);
    const resource = await client.readResource({ uri: open._meta.ui.resourceUri });
    assert.equal(resource.contents[0].mimeType, 'text/html;profile=mcp-app');
    assert.match(resource.contents[0].text, /回复所有人/);
    const result = await client.callTool({ name: 'open_workbench', arguments: {} });
    assert.deepEqual(result.structuredContent, { focus: {}, presentation: { mode: 'mcp-app', resource_uri: open._meta.ui.resourceUri } });
  } finally { await client.close(); }
});

test('ordinary MCP clients open the same workbench and cannot invoke App-only delivery tools', async () => {
  const profile = await mkdtemp(join(tmpdir(), 'relay-portable-test-'));
  const seeded = spawnSync('python3', ['test/seed_profile.py', profile], { encoding: 'utf8' });
  assert.equal(seeded.status, 0, seeded.stderr);
  const client = new Client({ name: 'ordinary-mcp-host', version: '1.0.0' });
  const transport = new StdioClientTransport({ command: 'python3', args: ['../plugins/opl-relay/bin/opl-relay', '--json', 'workbench', 'mcp'], env: { ...process.env, OPL_PROFILE_WORKSPACE: profile } });
  let pid;
  try {
    await client.connect(transport);
    const { tools } = await client.listTools();
    assert.ok(tools.some(item => item.name === 'relay_reply'));
    assert.ok(!tools.some(item => item.name === 'relay_send' || item.name === 'relay_upload'));
    const forbidden = await client.callTool({ name: 'relay_send', arguments: { account_id: 'demo', request_id: 'never-send', approval: 'sha256:unreviewed', confirmed: true } });
    assert.equal(forbidden.isError, true);
    const messages = await client.callTool({ name: 'relay_messages', arguments: { account_id: 'demo', folder: 'inbox' } });
    const source = messages.structuredContent.messages[0].storage_ref;
    const reply = await client.callTool({ name: 'relay_reply', arguments: { account_id: 'demo', storage_ref: source, request_id: 'portable-reply', body: 'Controlled local edit.' } });
    assert.equal(reply.isError, undefined);
    const focus = { account_id: 'demo', draft_id: 'portable-reply' };
    const opened = await client.callTool({ name: 'open_workbench', arguments: focus });
    assert.deepEqual(opened.structuredContent.focus, { ...focus, revision: 1 });
    const presentation = opened.structuredContent.presentation;
    pid = presentation.pid;
    assert.equal(presentation.mode, 'browser');
    const url = new URL(presentation.url);
    assert.equal(url.hostname, '127.0.0.1');
    assert.equal(url.searchParams.get('draft_id'), focus.draft_id);
    const headers = { Authorization: `Bearer ${url.hash.slice(1)}`, 'Content-Type': 'application/json' };
    const saved = await fetch(`${url.origin}/api/draft`, { method: 'POST', headers, body: JSON.stringify({ account_id: 'demo', draft_id: focus.draft_id }) });
    const draft = (await saved.json()).draft;
    assert.equal(draft.body, 'Controlled local edit.');
    assert.deepEqual(draft.to, ['Conference secretary <secretary@example.test>']);
    assert.ok(draft.quote.includes('Previous correspondence'));
    const edited = await fetch(`${url.origin}/api/save`, { method: 'POST', headers, body: JSON.stringify({ ...focus, revision: 1, body: 'Human reviewed edit.', attachment_ids: [] }) });
    assert.equal((await edited.json()).draft.revision, 2);
    const readback = await client.callTool({ name: 'relay_draft', arguments: focus });
    assert.equal(readback.structuredContent.draft.body, 'Human reviewed edit.');
    assert.equal(readback.structuredContent.draft.revision, 2);
    const stale = await client.callTool({ name: 'relay_save', arguments: { ...focus, revision: 1, body: 'Stale edit.', attachment_ids: [] } });
    assert.equal(stale.isError, true);
    assert.equal(stale.structuredContent.error.code, 'revision_conflict');
    assert.equal(stale.structuredContent.disposition, 'not_dispatched');
    assert.deepEqual(stale.structuredContent.recovery, { action: 'draft', input: focus });
    const staleOpen = await client.callTool({ name: 'open_workbench', arguments: { ...focus, revision: 1 } });
    assert.equal(staleOpen.isError, true);
    assert.equal(staleOpen.structuredContent.error.code, 'revision_conflict');
    const reopened = await client.callTool({ name: 'open_workbench', arguments: focus });
    assert.deepEqual(reopened.structuredContent.focus, { ...focus, revision: 2 });
    assert.equal(reopened.structuredContent.presentation.pid, pid);
    const ambiguous = await client.callTool({ name: 'open_workbench', arguments: { ...focus, storage_ref: source } });
    assert.equal(ambiguous.isError, true);
    assert.equal(ambiguous.structuredContent.error.code, 'invalid_focus');
    const again = await client.callTool({ name: 'open_workbench', arguments: { account_id: 'demo', storage_ref: source } });
    assert.equal(again.structuredContent.presentation.pid, pid);
    assert.equal(new URL(again.structuredContent.presentation.url).searchParams.get('storage_ref'), source);
    assert.match(await (await fetch(url.origin)).text(), /回复所有人/);
  } finally {
    await client.close();
    if (pid) process.kill(pid, 'SIGTERM');
    await rm(profile, { recursive: true, force: true });
  }
});
