import { readFile } from 'node:fs/promises';
import net from 'node:net';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { z } from 'zod';

const capabilities = ['mailbox.search', 'mailbox.read', 'draft.create'];
const connection = { connection_id: z.string().min(1) };
const source = { ...connection, storage_ref: z.string().startsWith('email-store://') };
export const connectionActions = {
  connections: { schema: z.object({}).strict(), read: true, title: '读取已连接邮箱' },
  connection_request: { schema: z.object({ ...connection, capability_ids: z.array(z.enum(capabilities)).min(1).max(3) }).strict(), read: false, title: '请求邮箱使用授权' },
  connection_search: { schema: z.object({ ...connection, query: z.string().max(5000).optional(), scope: z.enum(['active', 'history']).optional(), folder: z.string().optional(), limit: z.number().int().min(1).max(100).optional() }).strict(), read: true, title: '查找已授权邮箱邮件' },
  connection_read: { schema: z.object(source).strict(), read: true, title: '读取已授权邮件' },
  connection_reply: { schema: z.object({ ...source, body: z.string().max(200000).optional(), request_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,70}$/) }).strict(), read: false, title: '建立已授权邮件回复草稿' },
};
const operationCapability = { connection_search: 'mailbox.search', connection_read: 'mailbox.read', connection_reply: 'draft.create' };

function unavailable(reason, message) {
  return Object.assign(new Error(message), {
    envelope: {
      ok: false,
      error: message,
      code: reason,
      disposition: 'not_dispatched',
      recovery: {
        action: 'repair_opld_link',
        command: 'opl pack link <installed-opl-relay> --json',
      },
    },
  });
}

async function callOpldRpc(method, params) {
  const stateDir = process.env.OPL_STATE_DIR || join(homedir(), '.opl', 'state');
  const sessionPath = join(stateDir, 'run', 'opld.session.json');
  const session = JSON.parse(await readFile(sessionPath, 'utf8'));
  const endpoint = session.socket_path || session.pipe_path;
  if (!endpoint || !session.token) {
    throw new Error('Invalid opld session descriptor.');
  }

  return new Promise((resolvePromise, rejectPromise) => {
    const socket = net.createConnection(endpoint);
    let buffer = '';
    let nextId = 1;
    let handshakeId = null;
    let callId = null;
    let settled = false;

    const finish = (err, value) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      if (err) rejectPromise(err);
      else resolvePromise(value);
    };

    const send = (rpcMethod, rpcParams) => {
      const id = nextId++;
      socket.write(JSON.stringify({ jsonrpc: '2.0', id, method: rpcMethod, params: rpcParams }) + '\n');
      return id;
    };

    socket.setEncoding('utf8');
    socket.setTimeout(10_000, () => finish(new Error('opld RPC timed out')));
    socket.on('error', (err) => finish(err));
    socket.on('data', (chunk) => {
      buffer += chunk;
      let idx;
      while ((idx = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        if (msg.id === handshakeId) {
          if (msg.error) {
            finish(Object.assign(new Error(msg.error.message || 'opld handshake failed'), { rpcError: msg.error }));
            return;
          }
          callId = send(method, params);
        } else if (msg.id === callId) {
          if (msg.error) {
            const data = msg.error.data || {};
            finish(Object.assign(new Error(msg.error.message || 'opld RPC error'), {
              details: {
                reason_code: data.reason_code || data.code || 'rpc_error',
                disposition: data.disposition || 'not_dispatched',
                invocation_id: data.invocation_id || null,
              },
            }));
            return;
          }
          finish(null, msg.result);
        }
      }
    });
    socket.on('connect', () => {
      handshakeId = send('daemon.handshake', {
        token: session.token,
        client_kind: process.env.OPL_CLIENT_KIND || 'external',
        api_version: '2.0',
      });
    });
  });
}

export async function loadOpldConnectionHost() {
  await callOpldRpc('daemon.health', {});
  return {
    activateInstalledPackageConnectionContext({ packageId }) {
      const bindToken = process.env.OPL_CONSUMER_BIND_TOKEN || undefined;
      return {
        dispose() {},
        connect: {
          async listConnections() {
            const res = await callOpldRpc('connect.list', { provider_type: 'opl-relay.mailbox' });
            return Array.isArray(res?.connections) ? res.connections : [];
          },
          async requestAccess(connectionId, capabilityIds) {
            return callOpldRpc('connect.requestAccess', {
              consumer_package_id: packageId,
              ...(bindToken ? { consumer_bind_token: bindToken } : {}),
              connection_id: connectionId,
              capability_ids: capabilityIds,
            });
          },
          async invoke(connectionId, capabilityId, input) {
            return callOpldRpc('connect.invoke', {
              consumer_package_id: packageId,
              ...(bindToken ? { consumer_bind_token: bindToken } : {}),
              connection_id: connectionId,
              capability_id: capabilityId,
              input,
            });
          },
        },
      };
    },
  };
}

// Only the domain service owns this closure. Neither MCP nor browser input can
// supply the installed Package identity, root, authorization service or loader.
export function createRelayConnectionService({ packageRoot, loadHost = loadOpldConnectionHost }) {
  let context;
  let loading;
  async function current() {
    if (context) return context;
    loading ??= (async () => {
      let host;
      try { host = await loadHost(); }
      catch { throw unavailable('framework_connection_host_unavailable', '连接守护进程不可用，请确认 opld 已启动并重新执行 opl pack link。'); }
      if (typeof host.activateInstalledPackageConnectionContext !== 'function') {
        throw unavailable('framework_connection_host_incompatible', '请更新 OPL Base 后重新打开 Relay 工作台。');
      }
      context = host.activateInstalledPackageConnectionContext({ packageRoot, packageId: 'opl-relay' });
      return context;
    })();
    try { return await loading; } finally { loading = undefined; }
  }
  return Object.freeze({
    async invoke(action, input) {
      const entry = connectionActions[action];
      if (!entry) throw unavailable('connection_action_invalid', '不支持的邮箱连接操作');
      const parsed = entry.schema.parse(input);
      try {
        const { connect } = await current();
        if (action === 'connections') {
          const items = await connect.listConnections();
          return {
            ok: true,
            connections: items.filter((item) => {
              const kind = item.connection_type ?? item.provider_type;
              return kind === 'opl-relay' || kind === 'opl-relay.mailbox';
            }),
          };
        }
        const { connection_id, ...payload } = parsed;
        if (action === 'connection_request') return { ok: true, request: await connect.requestAccess(connection_id, payload.capability_ids) };
        const invocation = await connect.invoke(connection_id, operationCapability[action], payload);
        return { ...invocation.result, ok: true, connection_id, invocation_id: invocation.invocation_id, disposition: invocation.disposition };
      } catch (error) {
        if (error.envelope) throw error;
        if (error.details?.reason_code) {
          const message = '邮箱连接调用未完成，请核对授权或读取同一对象的当前状态。';
          throw Object.assign(new Error(message), { envelope: { ok: false, error: message, code: error.details.reason_code, disposition: error.details.disposition || 'not_dispatched', invocation_id: error.details.invocation_id || null } });
        }
        throw error;
      }
    },
    dispose() { context?.dispose(); context = undefined; },
  });
}

const service = createRelayConnectionService({ packageRoot: resolve(dirname(fileURLToPath(import.meta.url)), '..') });
export const invokeConnectionAction = (action, input) => service.invoke(action, input);
