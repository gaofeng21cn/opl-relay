import { dirname, resolve } from 'node:path';
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
  return Object.assign(new Error(message), { envelope: { ok: false, error: message, code: reason, disposition: 'not_dispatched', recovery: { action: 'repair_framework_link', command: 'opl packages link-framework --agent-root <installed-opl-relay> --json' } } });
}

// Only the domain service owns this closure. Neither MCP nor browser input can
// supply the installed Package identity, root, authorization service or loader.
export function createRelayConnectionService({ packageRoot, loadHost = () => import('opl-framework/connection-authorization-host') }) {
  let context;
  let loading;
  async function current() {
    if (context) return context;
    loading ??= (async () => {
      let host;
      try { host = await loadHost(); }
      catch { throw unavailable('framework_connection_host_unavailable', '连接基座不可用，请在 OPL 中修复 Relay 的 Framework 连接。'); }
      if (typeof host.activateInstalledPackageConnectionContext !== 'function') {
        throw unavailable('framework_connection_host_incompatible', '请更新 Framework 后重新打开 Relay 工作台。');
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
        if (action === 'connections') return { ok: true, connections: (await connect.listConnections()).filter(item => item.connection_type === 'opl-relay') };
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
