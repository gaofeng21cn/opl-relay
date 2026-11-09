import assert from 'node:assert/strict';
import test from 'node:test';
import { createRelayConnectionService } from '../connection-context.mjs';

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
          return { invocation_id: 'inv-1', disposition: 'completed', result: { messages: [] } };
        },
      } };
    },
  }) });
  return { service, calls, starts: () => starts, revoke: () => { authorized = false; } };
}

test('Agent and App actions share one installed consumer and fixed mailbox capabilities', async () => {
  const f = fixture();
  assert.deepEqual((await f.service.invoke('connections', {})).connections.map(x => x.connection_id), ['mail']);
  await f.service.invoke('connection_search', { connection_id: 'mail', query: 'review' });
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
