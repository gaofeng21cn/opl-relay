import { AppBridge, PostMessageTransport } from '@modelcontextprotocol/ext-apps/app-bridge';

const callTool = async (request) => {
  const response = await fetch('/tool', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) });
  if (!response.ok) throw new Error('Controlled host tool call failed');
  return response.json();
};
const frame = document.querySelector('iframe');
const bridge = new AppBridge(null, { name: 'Relay SDK qualification host', version: '1.0.0' }, { serverTools: {}, updateModelContext: {} });
bridge.oncalltool = callTool;
bridge.onupdatemodelcontext = async () => ({});
bridge.oninitialized = async () => {
  await bridge.sendToolInput({ arguments: { account_id: 'demo' } });
  await bridge.sendToolResult(await callTool({ name: 'open_workbench', arguments: { account_id: 'demo' } }));
  document.body.dataset.initialized = 'true';
};
await bridge.connect(new PostMessageTransport(frame.contentWindow, frame.contentWindow));
frame.src = '/view';
