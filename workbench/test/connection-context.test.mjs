import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import net from 'node:net';
import { join } from 'node:path';
import test from 'node:test';
import { createRelayConnectionService, loadOpldConnectionHost } from '../connection-context.mjs';

const root = '/installed/opl-relay';
function fixture() {
  const calls = [];
  let active = true;
  let authorized = true;
  let starts = 0;
  const service = createRelayConnectionService({ packageRoot: root, loadHost: async () => ({
    activateInstalledPackageConnectionContext(identity) {
      starts++;
      assert.deepEqual(identity, { packageRoot: root, packageId: 'opl-relay' });
      return { dispose() { active = false; }, connect: {
        async listConnections() { return [{ connection_id: 'mail', connection_type: 'opl-relay' }, { connection_id: 'other', connection_type: 'other' }]; },
        async requestAccess(id, capabilities) { return { connection_id: id, capability_ids: capabilities, status: 'requested' }; },
        async invoke(id, capability, input) {
          if (!active || !authorized) throw Object.assign(new Error('denied'), { details: { reason_code: 'connection_grant_missing', disposition: 'not_dispatched' } });
          calls.push({ id, capability, input });
          return {
            invocation_id: 'inv-1',
            disposition: 'completed',
            result: { ok: true, disposition: 'completed', result: { messages: [], source: 'relay-local-cache' } },
          };
        },
      } };
    },
  }) });
  return { service, calls, starts: () => starts, revoke: () => { authorized = false; } };
}

test('Agent and App actions share one installed consumer and fixed mailbox capabilities', async () => {
  const f = fixture();
  assert.deepEqual((await f.service.invoke('connections', {})).connections.map(x => x.connection_id), ['mail']);
  const searchRes = await f.service.invoke('connection_search', { connection_id: 'mail', query: 'review' });
  assert.deepEqual(searchRes.messages, []);
  assert.equal(searchRes.source, 'relay-local-cache');
  assert.equal(searchRes.ok, true);
  assert.equal(searchRes.invocation_id, 'inv-1');
  await f.service.invoke('connection_read', { connection_id: 'mail', storage_ref: 'email-store://message' });
  assert.equal(f.starts(), 1);
  assert.deepEqual(f.calls.map(x => x.capability), ['mailbox.search', 'mailbox.read']);
  assert.equal((await f.service.invoke('connection_request', { connection_id: 'mail', capability_ids: ['mailbox.read'] })).request.status, 'requested');
  await assert.rejects(f.service.invoke('connection_search', { connection_id: 'mail', package_id: 'other' }));
  await assert.rejects(f.service.invoke('connection_request', { connection_id: 'mail', capability_ids: ['mail.send'] }));
  assert.equal(f.calls.length, 2);
  f.revoke();
  await assert.rejects(f.service.invoke('connection_read', { connection_id: 'mail', storage_ref: 'email-store://message' }), error => error.envelope.code === 'connection_grant_missing' && error.envelope.disposition === 'not_dispatched');
  assert.equal(f.calls.length, 2);
});

test('provider failure inside invocation.result preserves ok: false, error code, message and disposition', async () => {
  const service = createRelayConnectionService({
    packageRoot: root,
    loadHost: async () => ({
      activateInstalledPackageConnectionContext: () => ({
        dispose() {},
        connect: {
          async invoke() {
            return {
              invocation_id: 'inv-err-1',
              disposition: 'completed',
              result: {
                ok: false,
                disposition: 'not_dispatched',
                error: { code: 'MAILBOX_NOT_FOUND', message: '邮箱连接不存在或未在 Relay 中配置' },
              },
            };
          },
        },
      }),
    }),
  });

  await assert.rejects(
    service.invoke('connection_search', { connection_id: 'mail', query: 'missing' }),
    (error) => (
      error.envelope?.ok === false
      && error.envelope?.code === 'MAILBOX_NOT_FOUND'
      && error.envelope?.error?.code === 'MAILBOX_NOT_FOUND'
      && error.envelope?.error?.message === '邮箱连接不存在或未在 Relay 中配置'
      && error.envelope?.disposition === 'not_dispatched'
      && error.envelope?.invocation_id === 'inv-err-1'
    ),
  );
});

test('an unavailable Framework produces a repair result without calling the local mailbox owner', async () => {
  const service = createRelayConnectionService({ packageRoot: root, loadHost: async () => { throw new Error('missing module'); } });
  await assert.rejects(service.invoke('connections', {}), error => error.envelope.code === 'framework_connection_host_unavailable' && error.envelope.disposition === 'not_dispatched');
});

test('unknown invocation is returned once with its identity and is never retried', async () => {
  let count = 0;
  const service = createRelayConnectionService({ packageRoot: root, loadHost: async () => ({ activateInstalledPackageConnectionContext: () => ({ connect: {
    async invoke() { count++; throw Object.assign(new Error('unknown'), { details: { reason_code: 'provider_outcome_unknown', disposition: 'unknown_after_dispatch', invocation_id: 'exact-invocation' } }); },
  } }) }) });
  await assert.rejects(service.invoke('connection_reply', { connection_id: 'mail', storage_ref: 'email-store://message', request_id: 'same-request' }), error => error.envelope.invocation_id === 'exact-invocation' && error.envelope.disposition === 'unknown_after_dispatch');
  assert.equal(count, 1);
});

test('socket loss and timeout preserve uncertainty for dispatched connection writes', async (t) => {
  const sandbox = await mkdtemp('/tmp/relay-opld-loss-');
  const runDir = join(sandbox, 'run');
  await mkdir(runDir);
  const socketPath = join(runDir, 'opld.sock');
  const sockets = new Set();
  const writes = [];
  const server = net.createServer((socket) => {
    sockets.add(socket);
    socket.once('close', () => sockets.delete(socket));
    socket.setEncoding('utf8');
    let buffer = '';
    socket.on('data', (chunk) => {
      buffer += chunk;
      let end;
      while ((end = buffer.indexOf('\n')) !== -1) {
        const request = JSON.parse(buffer.slice(0, end));
        buffer = buffer.slice(end + 1);
        if (request.method === 'daemon.handshake' || request.method === 'daemon.health') {
          socket.write(JSON.stringify({ jsonrpc: '2.0', id: request.id, result: { ok: true, session_id: 'demo' } }) + '\n');
          continue;
        }
        writes.push(request);
        if (request.method === 'connect.requestAccess') socket.destroy();
      }
    });
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
  const service = createRelayConnectionService({
    packageRoot: root,
    loadHost: () => loadOpldConnectionHost({ stateDir: sandbox, rpcTimeoutMs: 100 }),
  });
  t.after(async () => {
    service.dispose();
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
    await rm(sandbox, { recursive: true, force: true });
  });

  const accessRequest = { connection_id: 'mail', capability_ids: ['mailbox.read'] };
  await assert.rejects(service.invoke('connection_request', accessRequest),
    (error) => error.envelope?.disposition === 'not_dispatched');
  assert.equal(writes.length, 0);

  await writeFile(join(runDir, 'opld.session.json'), JSON.stringify({ socket_path: socketPath, token: 'test' }));
  await assert.rejects(service.invoke('connection_request', accessRequest),
    (error) => error.envelope?.disposition === 'unknown_after_dispatch' && error.envelope.connection_id === 'mail');
  await assert.rejects(service.invoke('connection_reply', {
    connection_id: 'mail', storage_ref: 'email-store://message', request_id: 'draft-request',
  }), (error) => error.envelope?.disposition === 'unknown_after_dispatch' && error.envelope.request_id === 'draft-request');
  assert.deepEqual(writes.map(({ method, params }) => [method, params.input?.request_id]), [
    ['connect.requestAccess', undefined],
    ['connect.invoke', 'draft-request'],
  ]);
});

test('loadOpldConnectionHost uses one persistent handshake session, forwards session-bound token, and propagates opld errors', async () => {
  const sandbox = await mkdtemp('/tmp/relay-opld-ctx-');
  const runDir = join(sandbox, 'run');
  await mkdir(runDir, { recursive: true });
  const sockPath = join(runDir, 'opld.sock');
  const expectedToken = 'test-daemon-secret-token';

  let handshakeCount = 0;
  let lastSessionId = null;
  let observedClientKinds = [];
  const sockets = new Set();

  const server = net.createServer((socket) => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    let buf = '';
    let sessionId = null;
    let clientKind = null;
    socket.setEncoding('utf8');
    socket.on('data', (chunk) => {
      buf += chunk;
      let idx;
      while ((idx = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        const req = JSON.parse(line);
        if (req.method === 'daemon.handshake') {
          handshakeCount++;
          assert.equal(req.params.token, expectedToken);
          clientKind = req.params.client_kind;
          observedClientKinds.push(clientKind);
          sessionId = `sess_${handshakeCount}`;
          lastSessionId = sessionId;
          socket.write(JSON.stringify({ jsonrpc: '2.0', id: req.id, result: { ok: true, session_id: sessionId, client_kind: clientKind } }) + '\n');
          continue;
        }
        if (req.method === 'daemon.health') {
          socket.write(JSON.stringify({ jsonrpc: '2.0', id: req.id, result: { ok: true } }) + '\n');
          continue;
        }
        if (req.method === 'connect.list') {
          // Emit a server-push notification before the RPC response to verify event tolerance.
          socket.write(JSON.stringify({ jsonrpc: '2.0', method: 'event.connect.changed', params: { kind: 'ping' } }) + '\n');
          socket.write(JSON.stringify({ jsonrpc: '2.0', id: req.id, result: { connections: [{ connection_id: 'conn-1', name: 'Demo', provider_type: 'opl-relay.mailbox' }] } }) + '\n');
          continue;
        }
        if (req.method === 'connect.requestAccess') {
          if (!req.params.consumer_bind_token || req.params.session_id !== sessionId) {
            socket.write(JSON.stringify({ jsonrpc: '2.0', id: req.id, error: { code: -32001, message: 'BIND_TOKEN_SESSION_MISMATCH', data: { code: 'BIND_TOKEN_SESSION_MISMATCH', reason_code: 'bind_token_session_mismatch', disposition: 'not_dispatched' } } }) + '\n');
            continue;
          }
          socket.write(JSON.stringify({ jsonrpc: '2.0', id: req.id, result: { request_id: 'req-1', status: 'requested' } }) + '\n');
          continue;
        }
        if (req.method === 'connect.invoke') {
          if (!req.params.consumer_bind_token) {
            socket.write(JSON.stringify({ jsonrpc: '2.0', id: req.id, error: { code: -32003, message: `TRUSTED_HOST_REQUIRED: client_kind=${clientKind}`, data: { code: 'TRUSTED_HOST_REQUIRED', reason_code: 'trusted_host_required', disposition: 'not_dispatched' } } }) + '\n');
            continue;
          }
          if (req.params.consumer_bind_token !== `bind:${sessionId}` || req.params.session_id !== sessionId) {
            socket.write(JSON.stringify({ jsonrpc: '2.0', id: req.id, error: { code: -32001, message: 'BIND_TOKEN_SESSION_MISMATCH', data: { code: 'BIND_TOKEN_SESSION_MISMATCH', reason_code: 'bind_token_session_mismatch', disposition: 'not_dispatched' } } }) + '\n');
            continue;
          }
          socket.write(JSON.stringify({
            jsonrpc: '2.0',
            id: req.id,
            result: {
              invocation_id: 'inv-live-1',
              disposition: 'completed',
              result: {
                ok: true,
                disposition: 'completed',
                result: { messages: [{ storage_ref: 'email-store://demo/1', subject: 'Hello' }], source: 'relay-local-cache' },
              },
            },
          }) + '\n');
        }
      }
    });
  });

  await new Promise((resolve) => server.listen(sockPath, resolve));
  await writeFile(join(runDir, 'opld.session.json'), JSON.stringify({ socket_path: sockPath, token: expectedToken }), 'utf8');

  const prevStateDir = process.env.OPL_STATE_DIR;
  const prevBindToken = process.env.OPL_CONSUMER_BIND_TOKEN;
  const prevClientKind = process.env.OPL_CLIENT_KIND;
  process.env.OPL_STATE_DIR = sandbox;
  delete process.env.OPL_CONSUMER_BIND_TOKEN;
  delete process.env.OPL_CLIENT_KIND;

  let service;
  try {
    service = createRelayConnectionService({ packageRoot: root, loadHost: () => loadOpldConnectionHost() });

    // 1. Default client_kind remains 'external' (never spoofed) and fails closed without bind token.
    const listed = await service.invoke('connections', {});
    assert.equal(listed.connections.length, 1);
    assert.equal(handshakeCount, 1);
    assert.deepEqual(observedClientKinds, ['external']);

    await assert.rejects(
      service.invoke('connection_search', { connection_id: 'conn-1', query: 'hi' }),
      (error) => error.envelope?.ok === false && error.envelope?.code === 'TRUSTED_HOST_REQUIRED' && error.envelope?.disposition === 'not_dispatched',
    );
    assert.equal(handshakeCount, 1);

    // 2. Session-matched bind token succeeds across requestAccess + invoke on the SAME persistent handshake.
    process.env.OPL_CONSUMER_BIND_TOKEN = `bind:${lastSessionId}`;
    const reqRes = await service.invoke('connection_request', { connection_id: 'conn-1', capability_ids: ['mailbox.search'] });
    assert.equal(reqRes.request.request_id, 'req-1');
    const searchRes = await service.invoke('connection_search', { connection_id: 'conn-1', query: 'hi' });
    assert.equal(searchRes.ok, true);
    assert.equal(searchRes.invocation_id, 'inv-live-1');
    assert.equal(searchRes.source, 'relay-local-cache');
    assert.equal(searchRes.messages.length, 1);
    assert.equal(handshakeCount, 1);

    // 3. After server drops the socket, client reconnects cleanly with a new handshake.
    for (const s of sockets) s.destroy();
    await new Promise((resolve) => setTimeout(resolve, 20));
    // Old token now mismatches new session_id -> fails closed with BIND_TOKEN_SESSION_MISMATCH.
    await assert.rejects(
      service.invoke('connection_search', { connection_id: 'conn-1', query: 'hi' }),
      (error) => error.envelope?.ok === false && error.envelope?.code === 'BIND_TOKEN_SESSION_MISMATCH',
    );
    assert.equal(handshakeCount, 2);
  } finally {
    service?.dispose();
    for (const s of sockets) s.destroy();
    await new Promise((resolve) => server.close(resolve));
    if (prevStateDir === undefined) delete process.env.OPL_STATE_DIR;
    else process.env.OPL_STATE_DIR = prevStateDir;
    if (prevBindToken === undefined) delete process.env.OPL_CONSUMER_BIND_TOKEN;
    else process.env.OPL_CONSUMER_BIND_TOKEN = prevBindToken;
    if (prevClientKind === undefined) delete process.env.OPL_CLIENT_KIND;
    else process.env.OPL_CLIENT_KIND = prevClientKind;
    await rm(sandbox, { recursive: true, force: true });
  }
});
