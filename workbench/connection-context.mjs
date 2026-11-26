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

function markUnavailable(error, message) {
  return Object.assign(error instanceof Error ? error : new Error(message || String(error)), {
    opldUnavailable: true,
  });
}

function createOpldSessionClient(options = {}) {
  const rpcTimeoutMs = options.rpcTimeoutMs ?? 10_000;
  let socket = null;
  let buffer = '';
  let nextId = 1;
  let sessionId = null;
  let handshakeResult = null;
  let connectPromise = null;
  let closed = false;
  const pending = new Map();

  const updateRef = () => {
    if (!socket || socket.destroyed) return;
    if (pending.size > 0) socket.ref?.();
    else socket.unref?.();
  };

  const resetConnection = (err) => {
    const activeSocket = socket;
    socket = null;
    buffer = '';
    sessionId = null;
    handshakeResult = null;
    connectPromise = null;
    if (activeSocket && !activeSocket.destroyed) {
      activeSocket.destroy();
    }
    const reason = err || new Error('opld connection closed');
    for (const entry of pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(markUnavailable(reason));
    }
    pending.clear();
  };

  const sendRaw = (targetSocket, rpcMethod, rpcParams) => {
    const id = nextId++;
    targetSocket.write(JSON.stringify({ jsonrpc: '2.0', id, method: rpcMethod, params: rpcParams }) + '\n');
    return id;
  };

  const ensureConnected = async () => {
    if (closed) {
      throw markUnavailable(new Error('opld session client has been closed'));
    }
    if (socket && !socket.destroyed && handshakeResult) {
      return socket;
    }
    if (connectPromise) {
      return connectPromise;
    }

    connectPromise = (async () => {
      const stateDir = options.stateDir || process.env.OPL_STATE_DIR || join(homedir(), '.opl', 'state');
      const sessionPath = join(stateDir, 'run', 'opld.session.json');
      let session;
      try {
        session = JSON.parse(await readFile(sessionPath, 'utf8'));
      } catch (err) {
        throw markUnavailable(err, 'Unable to read opld session descriptor.');
      }
      const endpoint = session.socket_path || session.pipe_path;
      if (!endpoint || !session.token) {
        throw markUnavailable(new Error('Invalid opld session descriptor.'));
      }

      return new Promise((resolvePromise, rejectPromise) => {
        const candidate = net.createConnection(endpoint);
        let handshakeId = null;
        let handshakeSettled = false;

        const failHandshake = (err) => {
          if (handshakeSettled) return;
          handshakeSettled = true;
          clearTimeout(handshakeTimer);
          if (socket === candidate) socket = null;
          connectPromise = null;
          if (!candidate.destroyed) candidate.destroy();
          rejectPromise(err);
        };

        const handshakeTimer = setTimeout(() => {
          failHandshake(markUnavailable(new Error('opld handshake timed out')));
        }, rpcTimeoutMs);
        handshakeTimer.unref?.();

        candidate.setEncoding('utf8');
        candidate.on('error', (err) => {
          if (!handshakeSettled) {
            failHandshake(markUnavailable(err));
            return;
          }
          resetConnection(err);
        });
        candidate.on('close', () => {
          if (!handshakeSettled) {
            failHandshake(markUnavailable(new Error('opld socket closed during handshake')));
            return;
          }
          if (socket === candidate) {
            resetConnection(new Error('opld socket closed'));
          }
        });
        candidate.on('data', (chunk) => {
          buffer += chunk;
          let idx;
          while ((idx = buffer.indexOf('\n')) !== -1) {
            const line = buffer.slice(0, idx).trim();
            buffer = buffer.slice(idx + 1);
            if (!line) continue;
            let msg;
            try { msg = JSON.parse(line); } catch { continue; }
            if (msg.id === undefined || msg.id === null) continue;

            if (!handshakeSettled && msg.id === handshakeId) {
              clearTimeout(handshakeTimer);
              if (msg.error) {
                const data = msg.error.data || {};
                failHandshake(Object.assign(new Error(msg.error.message || 'opld handshake failed'), {
                  rpcError: msg.error,
                  details: {
                    code: data.code || data.reason_code || 'handshake_failed',
                    reason_code: data.reason_code || data.code || 'handshake_failed',
                    disposition: data.disposition || 'not_dispatched',
                  },
                }));
                return;
              }
              handshakeSettled = true;
              socket = candidate;
              handshakeResult = msg.result || {};
              sessionId = typeof handshakeResult.session_id === 'string' ? handshakeResult.session_id : null;
              connectPromise = null;
              updateRef();
              resolvePromise(candidate);
              continue;
            }

            const entry = pending.get(msg.id);
            if (!entry) continue;
            pending.delete(msg.id);
            clearTimeout(entry.timer);
            updateRef();
            if (msg.error) {
              const data = msg.error.data || {};
              entry.reject(Object.assign(new Error(msg.error.message || 'opld RPC error'), {
                rpcError: msg.error,
                details: {
                  code: data.code || data.reason_code || 'rpc_error',
                  reason_code: data.reason_code || data.code || 'rpc_error',
                  disposition: data.disposition || 'not_dispatched',
                  invocation_id: data.invocation_id || null,
                },
              }));
            } else {
              entry.resolve(msg.result);
            }
          }
        });
        candidate.on('connect', () => {
          handshakeId = sendRaw(candidate, 'daemon.handshake', {
            token: session.token,
            client_kind: options.clientKind || process.env.OPL_CLIENT_KIND || 'external',
            api_version: '2.0',
            consumer_package_id: options.consumerPackageId || 'opl-relay',
          });
        });
      });
    })();

    try {
      return await connectPromise;
    } finally {
      connectPromise = null;
    }
  };

  return {
    get sessionId() { return sessionId; },
    get handshakeResult() { return handshakeResult; },
    async call(method, params) {
      const activeSocket = await ensureConnected();
      return new Promise((resolvePromise, rejectPromise) => {
        const id = sendRaw(activeSocket, method, params);
        const timer = setTimeout(() => {
          pending.delete(id);
          updateRef();
          rejectPromise(new Error(`opld RPC '${method}' timed out`));
        }, rpcTimeoutMs);
        timer.unref?.();
        pending.set(id, { resolve: resolvePromise, reject: rejectPromise, timer });
        updateRef();
      });
    },
    close() {
      closed = true;
      resetConnection(new Error('opld session client closed'));
    },
  };
}

export async function loadOpldConnectionHost(options = {}) {
  const client = createOpldSessionClient(options);
  try {
    await client.call('daemon.health', {});
  } catch (err) {
    client.close();
    throw err;
  }

  return {
    get sessionId() { return client.sessionId; },
    async close() { client.close(); },
    activateInstalledPackageConnectionContext({ packageId, bindToken, getBindToken } = {}) {
      const resolveBindToken = () => {
        if (typeof getBindToken === 'function') {
          const dynamic = getBindToken(client.sessionId);
          if (typeof dynamic === 'string' && dynamic.trim()) return dynamic.trim();
        }
        if (typeof bindToken === 'string' && bindToken.trim()) return bindToken.trim();
        const envToken = process.env.OPL_CONSUMER_BIND_TOKEN;
        return typeof envToken === 'string' && envToken.trim() ? envToken.trim() : undefined;
      };
      return {
        get sessionId() { return client.sessionId; },
        dispose() { client.close(); },
        connect: {
          async listConnections() {
            const res = await client.call('connect.list', { provider_type: 'opl-relay.mailbox' });
            return Array.isArray(res?.connections) ? res.connections : [];
          },
          async requestAccess(connectionId, capabilityIds) {
            const token = resolveBindToken();
            return client.call('connect.requestAccess', {
              consumer_package_id: packageId,
              ...(token ? { consumer_bind_token: token } : {}),
              ...(client.sessionId ? { session_id: client.sessionId } : {}),
              connection_id: connectionId,
              capability_ids: capabilityIds,
            });
          },
          async invoke(connectionId, capabilityId, input) {
            const token = resolveBindToken();
            return client.call('connect.invoke', {
              consumer_package_id: packageId,
              ...(token ? { consumer_bind_token: token } : {}),
              ...(client.sessionId ? { session_id: client.sessionId } : {}),
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
        const rawResult = invocation?.result;
        if (rawResult && typeof rawResult === 'object' && !Array.isArray(rawResult) && rawResult.ok === false) {
          const code = rawResult.error?.code || 'PROVIDER_INVOCATION_FAILED';
          const message = rawResult.error?.message || '邮箱连接调用未完成，请核对授权或读取同一对象的当前状态。';
          const disposition = rawResult.disposition || invocation.disposition || 'not_dispatched';
          const invocationId = invocation.invocation_id || null;
          throw Object.assign(new Error(message), {
            code,
            disposition,
            details: { reason_code: code, disposition, invocation_id: invocationId },
            envelope: {
              ok: false,
              code,
              error: { code, message },
              message,
              disposition,
              connection_id,
              invocation_id: invocationId,
            },
          });
        }
        const unwrapped =
          rawResult && typeof rawResult === 'object' && !Array.isArray(rawResult)
          && 'result' in rawResult && rawResult.result && typeof rawResult.result === 'object' && !Array.isArray(rawResult.result)
            ? rawResult.result
            : (rawResult && typeof rawResult === 'object' && !Array.isArray(rawResult) ? rawResult : {});
        return {
          ...unwrapped,
          ok: true,
          connection_id,
          invocation_id: invocation.invocation_id,
          disposition: rawResult?.disposition || invocation.disposition,
        };
      } catch (error) {
        if (error.envelope) throw error;
        if (error.opldUnavailable) {
          context?.dispose?.();
          context = undefined;
          throw unavailable('framework_connection_host_unavailable', '连接守护进程不可用，请确认 opld 已启动并重新执行 opl pack link。');
        }
        if (error.details?.code || error.details?.reason_code) {
          const code = error.details.code || error.details.reason_code;
          const reasonCode = error.details.reason_code || code;
          const message = error.message || '邮箱连接调用未完成，请核对授权或读取同一对象的当前状态。';
          const disposition = error.details.disposition || 'not_dispatched';
          const invocationId = error.details.invocation_id || null;
          throw Object.assign(new Error(message), {
            code,
            details: error.details,
            envelope: {
              ok: false,
              error: { code, message },
              code,
              reason_code: reasonCode,
              disposition,
              invocation_id: invocationId,
            },
          });
        }
        throw error;
      }
    },
    dispose() { context?.dispose(); context = undefined; },
  });
}

const service = createRelayConnectionService({ packageRoot: resolve(dirname(fileURLToPath(import.meta.url)), '..') });
export const invokeConnectionAction = (action, input) => service.invoke(action, input);
