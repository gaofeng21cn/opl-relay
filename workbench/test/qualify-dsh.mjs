import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const runtime = process.argv[2];
if (!runtime) throw new Error('Pass the directory containing the official DSH node_modules');
const requireRuntime = createRequire(resolve(runtime, 'package.json'));
const load = (name) => import(pathToFileURL(requireRuntime.resolve(name)).href);
const [{ Context }, prompt, tools, native, resources, client] = await Promise.all([
  load('@deepseek-ai/cordis'), load('@deepseek-ai/dsh-system-prompt'), load('@deepseek-ai/dsh-tools'),
  load('@deepseek-ai/dsh-tool-executor-native'), load('@deepseek-ai/dsh-mcp-resources'), load('@deepseek-ai/dsh-mcp-client'),
]);
const profile = await mkdtemp(join(tmpdir(), 'relay-dsh-profile-'));
const env = { ...process.env, OPL_PROFILE_WORKSPACE: profile };
const cli = resolve(process.argv[3] || '../plugins/opl-relay/bin/opl-relay');
const config = spawnSync('python3', [cli, '--json', 'workbench', 'connection', '--host', 'dsh', '--output', join(profile, 'bundle')], { env, encoding: 'utf8' });
assert.equal(config.status, 0, config.stderr);
const seeded = spawnSync('python3', ['test/seed_profile.py', profile], { encoding: 'utf8' });
assert.equal(seeded.status, 0, seeded.stderr);
const patch = JSON.parse(await readFile(join(profile, 'bundle', 'cordis.patch.yml'), 'utf8'));
const ctx = new Context();
let pid;
try {
  await ctx.plugin(prompt.default);
  await ctx.plugin(tools.default);
  await ctx.plugin(native);
  await ctx.plugin(resources.default);
  const mount = await ctx.plugin(client, patch[0].insert[0].config);
  const names = ctx.tools.definitions().map(item => item.name);
  assert.ok(names.includes('mcp__opl-relay__open_workbench'));
  assert.ok(!names.includes('mcp__opl-relay__relay_send'));
  const invoke = async (name, args) => {
    const batch = await ctx.tools.execute({ calls: [{ type: 'tool-call', id: crypto.randomUUID(), name, arguments: JSON.stringify(args) }], signal: AbortSignal.timeout(15000) });
    const result = batch.results[0].result;
    assert.ok(!result.isError, JSON.stringify(result));
    assert.ok(result.value, JSON.stringify(result));
    return result.value.structuredContent;
  };
  const messages = await invoke('mcp__opl-relay__relay_messages', { account_id: 'demo', folder: 'inbox' });
  assert.equal(messages.messages.length, 3);
  const reply = await invoke('mcp__opl-relay__relay_reply', { account_id: 'demo', storage_ref: messages.messages[0].storage_ref, request_id: 'dsh-controlled-reply', body: 'Native DSH controlled edit.' });
  assert.deepEqual(reply.draft.to, ['Conference secretary <secretary@example.test>']);
  const opened = await invoke('mcp__opl-relay__open_workbench', { account_id: 'demo', draft_id: reply.draft.id });
  assert.equal(opened.presentation.mode, 'browser');
  pid = opened.presentation.pid;
  assert.equal(new URL(opened.presentation.url).searchParams.get('draft_id'), reply.draft.id);
  await mount.dispose();
  assert.ok(!ctx.tools.definitions().some(item => item.name.startsWith('mcp__opl-relay__')));
  process.stdout.write('Official DSH plugin: config load, discovery, mailbox read, Reply All, browser handoff and unload passed.\n');
} finally {
  await ctx.fiber.dispose();
  if (pid) process.kill(pid, 'SIGTERM');
  await rm(profile, { recursive: true, force: true });
}
