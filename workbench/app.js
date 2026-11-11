import { App, applyDocumentTheme, applyHostStyleVariables } from '@modelcontextprotocol/ext-apps';
import DOMPurify from 'dompurify';

const $ = (id) => document.getElementById(id);
const element = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const button = (text, handler, className) => {
  const node = element('button', text, className);
  node.type = 'button';
  node.onclick = () => run(handler);
  return node;
};
const iconPaths = {
  reply: ['m9 7-5 5 5 5', 'M4 12h10a6 6 0 0 1 6 6', 'm5 7-5 5 5 5'],
  source: ['M14 3H5v18h14V8l-5-5Z', 'M14 3v5h5M8 13h8M8 17h6'],
  back: ['m10 5-7 7 7 7', 'M3 12h18'],
  attachment: ['m8 13 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9a6 6 0 0 1 8 8l-9 9'],
  archive: ['M4 7h16v13H4z', 'M3 7h18M8 11h8'],
  folder: ['M3 6h7l2 2h9v11H3z'],
  trash: ['M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5'],
  pending: ['M5 6h14M5 12h14M5 18h8M3 6h.01M3 12h.01M3 18h.01'],
  more: ['M5 12h.01M12 12h.01M19 12h.01'],
};
function actionButton(text, handler, iconName, className) {
  const node = button(text, handler, className);
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('class', 'icon');
  icon.setAttribute('aria-hidden', 'true');
  for (const shape of iconPaths[iconName]) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', shape); icon.append(path);
  }
  node.prepend(icon);
  return node;
}
function iconButton(label, handler, iconName, className = '') {
  const node = button('', handler, `icon-button ${className}`.trim());
  node.setAttribute('aria-label', label);
  node.title = label;
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('class', 'icon');
  icon.setAttribute('aria-hidden', 'true');
  for (const shape of iconPaths[iconName]) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', shape); icon.append(path);
  }
  node.append(icon);
  return node;
}
const state = { account: '', mode: 'inbox', selected: null, edit: null, dirty: false, attachments: [], request: 0 };
const initialFocus = Object.fromEntries(new URLSearchParams(location.search));
if (initialFocus.revision) initialFocus.revision = Number(initialFocus.revision);
let selection = 0;
let bridge;
let capability;
let operationSequence = 0;
const embedded = window.self !== window.top;
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
function applyHostContext(context = {}) {
  applyDocumentTheme(context.theme || (systemTheme.matches ? 'dark' : 'light'));
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
  const root = document.documentElement;
  root.dataset.embedded = String(embedded);
  root.dataset.displayMode = context.displayMode || 'inline';
  if (context.containerDimensions) {
    const height = context.containerDimensions.height ?? context.containerDimensions.maxHeight;
    if (Number.isFinite(height) && height > 0) root.style.setProperty('--host-max-height', `${height}px`);
    else root.style.removeProperty('--host-max-height');
  }
  if (context.safeAreaInsets) {
    for (const side of ['top', 'right', 'bottom', 'left']) root.style.setProperty(`--safe-${side}`, `${Math.max(0, context.safeAreaInsets[side] || 0)}px`);
  }
  const fullscreen = $('fullscreen');
  fullscreen.hidden = embedded && !context.availableDisplayModes?.includes('fullscreen');
  fullscreen.title = context.displayMode === 'fullscreen' ? '退出全屏' : '全屏';
  fullscreen.setAttribute('aria-label', fullscreen.title);
}
applyHostContext();
systemTheme.addEventListener('change', () => { if (!embedded) applyHostContext(); });

function updateContext(identity) {
  if (bridge) bridge.updateModelContext({ content: [{ type: 'text', text: JSON.stringify({ account_id: state.account, ...identity }) }] }).catch(() => {});
}

function operation(text, status = 'idle') {
  const node = $('operation-status');
  node.textContent = text;
  node.dataset.state = status;
}
if (window.self === window.top) {
  capability = location.hash.slice(1);
  try {
    capability ||= sessionStorage.getItem('relay-workbench-capability');
    if (capability) sessionStorage.setItem('relay-workbench-capability', capability);
  } catch {}
  if (capability) history.replaceState(null, '', location.pathname);
}

function notice(text, error = false) {
  operationSequence++;
  operation(text, error ? 'error' : 'complete');
  $('notice').textContent = text;
  $('notice').className = `notice${error ? ' error' : ''}`;
  $('notice').hidden = false;
  clearTimeout(notice.timer);
  notice.timer = setTimeout(() => { $('notice').hidden = true; }, error ? 18000 : 5000);
}

async function run(action) {
  try { return await action(); }
  catch (error) { notice(error.message || '操作未完成，请先核对当前状态', true); }
}

async function api(action, input = {}) {
  const sequence = ++operationSequence;
  const labels = { save: '正在保存编辑…', publish: '正在保存并核对邮箱草稿…', send: '正在发送并核对结果…', sync: '正在同步邮件…', upload: '正在保存附件…', reply: '正在准备回复…' };
  operation(labels[action] || '正在读取…', 'running');
  try {
    let result;
    if (bridge) {
      const response = await bridge.callServerTool({ name: `relay_${action}`, arguments: input });
      if (response.isError) throw new Error(response.content?.find((part) => part.type === 'text')?.text || '操作未完成');
      result = response.structuredContent;
    } else {
      const response = await fetch(`/api/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${capability}` }, body: JSON.stringify(input),
      });
      result = await response.json();
      if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error : result.error?.message || '操作未完成');
    }
    if (result?.ok === false) throw new Error(typeof result.error === 'string' ? result.error : result.error?.message || '操作未完成');
    if (sequence === operationSequence) operation('已读取当前状态');
    return result;
  } catch (error) {
    if (sequence === operationSequence) operation(error.message || '操作未完成，请核对当前状态', 'error');
    throw error;
  }
}

async function preserveEdits() {
  if (!state.dirty) return true;
  if (preserveEdits.pending) return false;
  preserveEdits.pending = true;
  try {
    return await new Promise((resolve) => {
      const dialog = element('dialog');
      const title = element('h2', '保留当前修改？'); title.id = `unsaved-${crypto.randomUUID()}`;
      dialog.setAttribute('aria-labelledby', title.id);
      const actions = element('div', undefined, 'dialog-actions');
      let discard = false;
      actions.append(button('继续编辑', () => dialog.close(), 'primary'), button('放弃修改并离开', () => { discard = true; dialog.close(); }));
      dialog.append(title, element('p', '当前回复还有未保存的正文或附件。'), actions);
      dialog.addEventListener('close', () => { dialog.remove(); resolve(discard); }, { once: true });
      document.body.append(dialog); dialog.showModal();
    });
  } finally { preserveEdits.pending = false; }
}

function setDirty(value) {
  state.dirty = value;
  const status = $('edit-status');
  if (!status || state.edit?.state !== 'editing') return;
  const text = value ? '有未保存修改' : '本地编辑';
  if (status.textContent !== text) status.textContent = text;
  status.classList.toggle('unsaved', value);
}

function header(container, label, value) {
  if (!value) return;
  const row = element('div', undefined, 'header-row');
  row.append(element('span', label, 'header-label'), element('span', Array.isArray(value) ? value.join(', ') : value));
  container.append(row);
}

const formatDate = (value) => {
  if (!value) return '';
  const date = new Date(value);
  const now = new Date();
  const time = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  return date.toDateString() === now.toDateString()
    ? time
    : `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${time}`;
};
const fullDate = (value) => value ? new Date(value).toLocaleString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) : '';
const bytes = (value) => value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : `${Math.ceil(value / 1024)} KB`;

async function loadList(focus = {}) {
  if (!state.account) return;
  const request = ++state.request;
  const list = $('message-list');
  list.setAttribute('aria-busy', 'true');
  list.replaceChildren(element('div', '正在读取…', 'empty-state'));
  const titles = { inbox: '收件箱', sent: '已发送', drafts: '草稿', history: '历史邮件' };
  $('list-title').textContent = titles[state.mode];
  $('folder-label').textContent = state.mode === 'inbox' ? '收件箱 · INBOX' : state.mode === 'sent' ? '已发送 · SENT' : titles[state.mode];
  for (const name of Object.keys(titles)) {
    $(name).classList.toggle('active', name === state.mode);
    if (name === state.mode) $(name).setAttribute('aria-current', 'page');
    else $(name).removeAttribute('aria-current');
  }
  let result;
  try {
    result = state.mode === 'drafts'
      ? await api('drafts', { account_id: state.account })
      : await api('messages', { account_id: state.account, query: $('search').value, scope: state.mode === 'history' ? 'history' : 'active', folder: state.mode === 'inbox' ? 'inbox' : state.mode === 'sent' ? 'sent' : '', limit: 100 });
  } catch (error) {
    if (request === state.request) {
      const empty = element('div', undefined, 'empty-state');
      empty.append(element('p', '邮件读取未完成'), button('重新读取', loadList));
      list.replaceChildren(empty);
    }
    throw error;
  } finally {
    if (request === state.request) list.setAttribute('aria-busy', 'false');
  }
  if (request !== state.request) return;
  const items = result.messages || result.drafts || [];
  $('count').textContent = items.length === result.limit ? `${items.length}+` : items.length;
  $('freshness').textContent = result.freshness || '草稿已读取';
  $('unread-summary').textContent = result.freshness || '当前快照';
  $('message-list').replaceChildren();
  if (!items.length) $('message-list').append(element('div', '当前范围没有邮件', 'empty-state'));
  for (const item of items) {
    const row = button('', () => select(item), 'message-row');
    row.dataset.ref = item.storage_ref || item.id;
    const top = element('div', undefined, 'row-top');
    const from = item.from || (item.state === 'editing' ? '正在编辑' : '已保存草稿');
    const sender = element('span', from.replace(/\s*<[^>]+>\s*$/, '').replace(/^"(.*)"$/, '$1') || from, 'sender');
    sender.title = from;
    const date = element('time', formatDate(item.date || item.updated_at));
    date.title = fullDate(item.date || item.updated_at);
    top.append(sender, date);
    row.setAttribute('aria-pressed', 'false');
    row.setAttribute('aria-label', `${from}：${item.subject || '回复草稿'}`);
    row.append(top, element('div', item.subject || '回复草稿', 'row-subject'));
    if (item.attachments?.length) row.append(element('div', `${item.attachments.length} 个附件`, 'row-footer'));
    $('message-list').append(row);
  }
  if (!focus.storage_ref && !focus.draft_id && !focus.request_id && !state.edit && !state.selected && items[0]) await select(items[0]);
  return items;
}

async function select(item) {
  if (!await preserveEdits()) return;
  const current = ++selection;
  const accountId = state.account;
  state.dirty = false;
  document.querySelector('.workspace').classList.remove('view-source');
  document.querySelector('.workspace').classList.add('reading');
  document.querySelectorAll('.message-row').forEach((row) => {
    const active = row.dataset.ref === (item.storage_ref || item.id);
    row.classList.toggle('active', active); row.setAttribute('aria-pressed', String(active));
  });
  if (!item.storage_ref) {
    if (item.state === 'server') {
      const result = await api('inspect', { account_id: accountId, request_id: item.server_request_id });
      if (current === selection) showServer(result.server_draft);
      return;
    }
    const result = await api('draft', { account_id: accountId, draft_id: item.id });
    if (current !== selection) return;
    return compose(result.draft);
  }
  const result = await api('read', { account_id: accountId, storage_ref: item.storage_ref });
  if (current !== selection) return;
  state.selected = result.message;
  $('composer').hidden = true;
  state.edit = null;
  renderMessage(result.message);
  updateContext({ storage_ref: result.message.storage_ref, subject: result.message.subject });
}

function renderMessage(message) {
  const toolbar = $('reader-toolbar');
  toolbar.replaceChildren();
  const target = $('reader-content');
  target.replaceChildren();
  const heading = element('div', undefined, 'reader-heading');
  heading.append(element('h2', message.subject), element('span', '原邮件', 'badge'));
  const headers = element('div', undefined, 'headers');
  for (const name of ['From', 'To', 'Cc', 'Bcc', 'Date']) header(headers, { From: '发件人', To: '收件人', Cc: '抄送', Bcc: '密送', Date: '时间' }[name], name === 'Date' ? fullDate(message.date) : message.headers[name]);
  const body = element('pre', message.body_text, 'message-body');
  const attachments = element('div', undefined, 'attachments');
  for (const attachment of message.attachments || []) {
    attachments.append(button(`${attachment.name} · ${bytes(attachment.size)}`, () => preview(message, attachment), 'attachment'));
  }
  if (state.edit) toolbar.append(actionButton('返回草稿', () => {
    document.querySelector('.workspace').classList.remove('view-source');
    $('reply-body').focus();
  }, 'back', 'primary'));
  else toolbar.append(actionButton('回复', async () => {
    if (!await preserveEdits()) return;
    const accountId = state.account;
    const current = ++selection;
    const request_id = `reply-${crypto.randomUUID()}`;
    const result = await api('reply', { account_id: accountId, storage_ref: message.storage_ref, request_id });
    if (current === selection) compose(result.draft);
  }, 'reply', 'primary'));
  const controls = element('div', undefined, 'reader-actions');
  controls.append(button('返回列表', async () => {
    if (!await preserveEdits()) return;
    state.dirty = false; state.edit = null; state.selected = null; $('composer').hidden = true;
    showEmptyReader();
    document.querySelector('.workspace').classList.remove('reading', 'view-source');
    document.querySelector('.message-row.active')?.focus();
  }));
  if (message.body_html) controls.append(button('查看原始排版', () => {
    const frame = element('iframe', undefined, 'html-message');
    frame.title = '原邮件排版';
    frame.setAttribute('sandbox', '');
    const safe = DOMPurify.sanitize(message.body_html, { FORBID_TAGS: ['img', 'svg', 'math', 'form', 'input', 'button', 'iframe', 'object', 'embed', 'style', 'link'] });
    frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><body style="font-family:system-ui;padding:16px">${safe}</body>`;
    body.replaceWith(frame);
  }, 'quiet'));
  target.append(heading, headers, body, attachments, controls);
}

function showEmptyReader() {
  $('reader-toolbar').replaceChildren();
  const empty = element('div', undefined, 'empty-state');
  const mark = element('span', undefined, 'empty-icon');
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('aria-hidden', 'true');
  for (const shape of ['M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v11a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 17.5z', 'm5 7 7 5 7-5']) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', shape); icon.append(path);
  }
  mark.append(icon);
  empty.append(mark, element('h2', '把上下文放在一起'), element('p', '选择一封邮件，阅读完整往来；\n回复所有人、核对附件，再从容发送。'));
  $('reader-content').replaceChildren(empty);
}

function compose(draft) {
  state.edit = draft;
  state.attachments = [...draft.attachments];
  state.dirty = false;
  const composer = $('composer');
  document.querySelector('.workspace').classList.remove('view-source');
  document.querySelector('.workspace').classList.add('reading');
  composer.hidden = false;
  composer.replaceChildren();
  const top = element('div', undefined, 'composer-top');
  const tools = element('div', undefined, 'composer-tools');
  const status = element('span', { editing: '本地编辑', publishing: '待核对保存结果', published: '已提交邮箱' }[draft.state], 'edit-status');
  status.id = 'edit-status'; status.setAttribute('role', 'status');
  tools.append(status, actionButton('查看原邮件', showSource, 'source', 'source-toggle quiet'));
  top.append(element('h2', '回复所有人'), tools);
  const headers = element('div', undefined, 'headers');
  header(headers, '发件人', draft.from_address);
  header(headers, '收件人', draft.to);
  header(headers, '抄送', draft.cc);
  header(headers, '主题', draft.subject);
  const label = element('label', '本次回复正文');
  label.htmlFor = 'reply-body';
  const editor = element('textarea', undefined, 'reply-editor');
  editor.id = 'reply-body'; editor.value = draft.body;
  editor.placeholder = '写下本次回复，原签名和引用历史将保留在下方。';
  editor.readOnly = draft.state !== 'editing';
  editor.oninput = () => setDirty(true);
  const signature = element('div', draft.signature || '请先配置批准的签名模板', 'signature');
  const attachments = element('div', undefined, 'attachments');
  attachments.id = 'edit-attachments';
  const actions = element('div', undefined, 'composer-actions');
  const picker = element('input'); picker.type = 'file'; picker.multiple = true; picker.hidden = true;
  picker.onchange = () => run(async () => {
    const draftId = state.edit.id;
    for (const file of picker.files) {
      if (file.size > 10 * 1024 * 1024) throw new Error('单个附件不能超过10 MiB');
      const content = new Uint8Array(await file.arrayBuffer());
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', content))).map((value) => value.toString(16).padStart(2, '0')).join('');
      let binary = ''; for (const value of content) binary += String.fromCharCode(value);
      const result = await api('upload', { name: file.name, data: btoa(binary), sha256: hash });
      if (state.edit?.id !== draftId) return;
      if (!state.attachments.some((item) => item.id === result.attachment.id)) state.attachments.push(result.attachment);
    }
    setDirty(true); renderEditAttachments(); picker.value = '';
  });
  if (draft.state === 'editing') {
    const save = button('保存编辑', saveEdit); save.id = 'save-edit';
    actions.append(actionButton('添加附件', () => picker.click(), 'attachment', 'quiet'), save, button('保存到邮箱草稿箱', publish, 'primary'));
  }
  else actions.append(button('核对邮箱草稿', async () => showServer((await api('inspect', { account_id: state.account, request_id: draft.server_request_id })).server_draft), 'primary'));
  const quoted = element('div', undefined, 'quoted');
  const details = element('details'); details.append(element('summary', '原邮件上下文（保留）'), element('pre', draft.quote)); quoted.append(details);
  composer.append(top, headers, label, editor, signature, attachments, picker, quoted, actions);
  renderEditAttachments();
  updateContext({ draft_id: draft.id, revision: draft.revision, state: draft.state });
}

async function showSource() {
  const draft = state.edit;
  const accountId = state.account;
  let source = state.selected;
  if (source?.storage_ref !== draft.source_ref) source = (await api('read', { account_id: accountId, storage_ref: draft.source_ref })).message;
  if (state.edit?.id !== draft.id || state.account !== accountId) return;
  state.selected = source;
  renderMessage(source);
  document.querySelector('.workspace').classList.add('reading', 'view-source');
}

function renderEditAttachments() {
  const target = $('edit-attachments'); target.replaceChildren();
  for (const item of state.attachments) {
    const card = element('span', undefined, 'attachment');
    card.append(button(`${item.name} · ${bytes(item.size)}`, async () => showAttachment((await api('uploaded_attachment', { attachment_id: item.id })).attachment), 'quiet'));
    if (state.edit.state === 'editing') card.append(button('移除', () => { state.attachments = state.attachments.filter((other) => other.id !== item.id); setDirty(true); renderEditAttachments(); }, 'quiet'));
    target.append(card);
  }
}

async function saveEdit() {
  const draft = state.edit;
  const body = $('reply-body').value;
  const attachment_ids = state.attachments.map((item) => item.id);
  const save = $('save-edit');
  if (save?.disabled) return;
  if (save) { save.disabled = true; save.textContent = '保存中…'; save.setAttribute('aria-busy', 'true'); }
  try {
    const result = await api('save', { account_id: state.account, draft_id: draft.id, revision: draft.revision, body, attachment_ids });
    if (state.edit?.id !== draft.id) return;
    state.edit = result.draft;
    setDirty($('reply-body').value !== body || JSON.stringify(state.attachments.map((item) => item.id)) !== JSON.stringify(attachment_ids));
    notice('编辑已保存，尚未发送');
    if (!state.dirty) $('edit-status').textContent = '编辑已保存';
    updateContext({ draft_id: result.draft.id, revision: result.draft.revision, state: result.draft.state });
  } finally {
    if (save) { save.disabled = false; save.textContent = '保存编辑'; save.removeAttribute('aria-busy'); }
  }
}

async function publish() {
  const editingId = state.edit.id;
  const accountId = state.account;
  if (state.dirty) await saveEdit();
  if (state.edit?.id !== editingId || state.account !== accountId) return;
  if (state.dirty) throw new Error('保存期间正文发生变化，请再次保存后审核');
  const draft = state.edit;
  $('reply-body').readOnly = true;
  document.querySelectorAll('#composer button').forEach((node) => { node.disabled = true; });
  notice('正在保存并核对邮箱草稿…');
  try {
    const result = await api('publish', { account_id: accountId, draft_id: draft.id, revision: draft.revision });
    if (state.edit?.id !== draft.id || state.account !== accountId) return;
    state.edit.state = 'published';
    compose(state.edit);
    showServer(result.server_draft);
    notice('已保存到邮箱草稿箱，尚未发送');
  } catch (error) {
    if (state.edit?.id === draft.id && state.account === accountId) {
      try { compose((await api('draft', { account_id: accountId, draft_id: draft.id })).draft); }
      catch { state.edit.state = 'publishing'; compose(state.edit); }
    }
    throw error;
  }
}

function showServer(draft) {
  const accountId = state.account;
  updateContext({ request_id: draft.request_id, state: draft.state });
  if (draft.state !== 'draft') {
    notice(draft.state === 'sent' ? '邮件已发送，不会重复发送' : '发送结果尚待核对，请勿重试发送', draft.state !== 'sent');
    return;
  }
  const dialog = element('dialog');
  const title = element('h2', '审核邮箱草稿'); title.id = `review-${crypto.randomUUID()}`;
  dialog.setAttribute('aria-labelledby', title.id); dialog.append(title);
  const headers = element('div', undefined, 'headers');
  for (const name of ['From', 'To', 'Cc', 'Bcc', 'Subject']) header(headers, { From: '发件人', To: '收件人', Cc: '抄送', Bcc: '密送', Subject: '主题' }[name], draft.headers[name]);
  dialog.append(headers, element('div', draft.body_text, 'review-body'));
  const attachments = element('div', undefined, 'attachments');
  for (const item of draft.attachments || []) attachments.append(element('span', `${item.name} · ${bytes(item.size)}`, 'attachment'));
  if (!draft.attachments?.length) attachments.append(element('span', '无附件', 'row-footer'));
  const actions = element('div', undefined, 'dialog-actions');
  const send = button('确认发送', async () => {
    send.disabled = true;
    try {
      const result = await api('send', { account_id: accountId, request_id: draft.request_id, approval: draft.approval_fingerprint, confirmed: true });
      dialog.close(); dialog.remove();
      notice(result.delivery.state === 'sent' ? '邮件已发送，已核对发送回执' : '发送结果尚待核对，请勿重复发送', result.delivery.state !== 'sent');
    } catch (error) { send.textContent = '请关闭并重新核对状态'; throw error; }
  }, 'primary');
  actions.append(button('关闭', () => { dialog.close(); dialog.remove(); }), send);
  dialog.append(attachments, actions); document.body.append(dialog); dialog.showModal();
}

async function preview(message, attachment) {
  const result = await api('attachment', { account_id: state.account, storage_ref: message.storage_ref, index: attachment.index });
  showAttachment(result.attachment);
}

function showAttachment(item) {
  const binary = atob(item.data); const content = Uint8Array.from(binary, (value) => value.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([content], { type: item.type }));
  const dialog = element('dialog');
  const title = element('h2', item.name); title.id = `attachment-${crypto.randomUUID()}`;
  dialog.setAttribute('aria-labelledby', title.id); dialog.append(title);
  dialog.addEventListener('close', () => { dialog.remove(); URL.revokeObjectURL(url); }, { once: true });
  if (['image/jpeg', 'image/png', 'image/webp', 'image/gif'].includes(item.type)) {
    const image = element('img', undefined, 'preview-image'); image.src = url; image.alt = item.name; dialog.append(image);
  } else if (item.type === 'application/pdf') {
    const frame = element('iframe', undefined, 'preview-pdf'); frame.src = url; frame.title = item.name; dialog.append(frame);
  }
  const download = element('a', '下载附件'); download.href = url; download.download = item.name;
  const controls = element('div', undefined, 'dialog-actions');
  controls.append(download, button('关闭', () => dialog.close()));
  dialog.append(controls); document.body.append(dialog); dialog.showModal();
}

async function boot(focus = {}) {
  focus = (await api('focus', focus)).focus;
  const result = await api('accounts');
  $('account').replaceChildren(...result.accounts.map((item) => {
    const option = element('option', item.email); option.value = item.account_id; return option;
  }));
  if (focus.account_id) $('account').value = focus.account_id;
  state.account = $('account').value;
  if (!state.account) { $('freshness').textContent = '尚未配置邮箱'; notice('请先通过 OPL Relay 配置邮箱'); return; }
  await loadList(focus);
  if (focus.storage_ref) await select({ storage_ref: focus.storage_ref });
  if (focus.draft_id) {
    const draft = (await api('draft', { account_id: state.account, draft_id: focus.draft_id })).draft;
    compose(draft);
    if (draft.revision !== focus.revision) notice('打开期间草稿已更新，现显示 owner 的最新版本，请重新核对。');
  }
  if (focus.request_id) showServer((await api('inspect', { account_id: state.account, request_id: focus.request_id })).server_draft);
}

async function showConnections() {
  const dialog = element('dialog');
  const title = element('h2', '已连接邮箱'); title.id = 'connections-title';
  dialog.setAttribute('aria-labelledby', title.id);
  const status = element('p', '正在读取连接…'); status.setAttribute('role', 'status');
  const selector = element('select'); selector.setAttribute('aria-label', '选择已连接邮箱');
  const query = element('input'); query.type = 'search'; query.placeholder = '搜索此连接的邮件'; query.setAttribute('aria-label', '搜索已连接邮箱');
  const results = element('div');
  const attempt = async (action) => { try { await action(); } catch (error) { status.textContent = error.message; } };
  const search = button('查找邮件', () => attempt(async () => {
    status.textContent = '正在读取…';
    const value = await api('connection_search', { connection_id: selector.value, query: query.value, limit: 25 });
    results.replaceChildren();
    for (const item of value.messages || []) results.append(button(item.subject || item.storage_ref, () => attempt(async () => {
      const read = await api('connection_read', { connection_id: selector.value, storage_ref: item.storage_ref });
      results.replaceChildren(element('h3', read.message.subject), element('pre', read.message.body_text, 'message-body'));
      status.textContent = '已读取当前授权范围内的原邮件。';
      updateContext({ connection_id: selector.value, storage_ref: item.storage_ref, invocation_id: read.invocation_id });
    })));
    status.textContent = `已读取 ${(value.messages || []).length} 封邮件。`;
  }));
  const request = button('请求读取授权', () => attempt(async () => {
    const value = await api('connection_request', { connection_id: selector.value, capability_ids: ['mailbox.search', 'mailbox.read'] });
    status.textContent = `请求已登记（${value.request.request_id}），请在 OPL 的资源与连接中审核；批准后可直接查找邮件。`;
  }));
  search.disabled = request.disabled = true;
  const actions = element('div', undefined, 'dialog-actions'); actions.append(search, request, button('关闭', () => dialog.close()));
  dialog.append(title, element('p', '复用 OPL 中已有的邮箱连接。每次读取均按当前授权核验。'), selector, query, status, results, actions);
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  document.body.append(dialog); dialog.showModal();
  await attempt(async () => {
    const value = await api('connections');
    selector.replaceChildren(...value.connections.map(item => { const option = element('option', item.name); option.value = item.connection_id; return option; }));
    search.disabled = request.disabled = !value.connections.length;
    status.textContent = value.connections.length ? '已有授权可直接查找邮件；缺少授权时再提交请求。' : '请先在 OPL 的资源与连接中添加 Relay 邮箱连接。';
  });
}
$('connections').onclick = () => run(showConnections);

$('account').onchange = () => run(async () => {
  if (!await preserveEdits()) { $('account').value = state.account; return; }
  selection++;
  state.account = $('account').value; state.dirty = false; state.edit = null; state.selected = null;
  $('composer').hidden = true;
  showEmptyReader();
  document.querySelector('.workspace').classList.remove('reading', 'view-source');
  await loadList();
});
for (const mode of ['inbox', 'sent', 'drafts', 'history']) $(mode).onclick = () => run(async () => {
  if (!await preserveEdits()) return;
  selection++;
  state.mode = mode; state.dirty = false; state.edit = null; state.selected = null; $('search').value = ''; $('composer').hidden = true; showEmptyReader(); document.querySelector('.workspace').classList.remove('reading', 'view-source'); await loadList();
});
$('search-form').onsubmit = (event) => { event.preventDefault(); run(loadList); };
$('sync').onclick = () => run(async () => {
  $('sync').disabled = true; $('sync').setAttribute('aria-busy', 'true'); $('sync').title = '同步中…';
  try { const result = await api('sync', { account_id: state.account }); if (!result.ok) throw new Error('同步未完整结束，请核对覆盖范围'); await loadList(); notice('同步已完成'); }
  finally { $('sync').disabled = false; $('sync').removeAttribute('aria-busy'); $('sync').title = '同步邮件'; }
});
window.addEventListener('beforeunload', (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
$('fullscreen').onclick = () => run(async () => {
  if (bridge) await bridge.requestDisplayMode({ mode: bridge.getHostContext()?.displayMode === 'fullscreen' ? 'inline' : 'fullscreen' });
  else if (document.fullscreenElement) await document.exitFullscreen();
  else await document.documentElement.requestFullscreen();
});

if (capability && window.self === window.top) run(() => boot(initialFocus));
else {
  bridge = new App({ name: 'OPL Relay Workbench', version: '0.1.0' });
  bridge.ontoolresult = (result) => run(async () => { if (await preserveEdits()) return boot(result.structuredContent?.focus || {}); });
  bridge.ontoolinput = () => {};
  bridge.onhostcontextchanged = () => applyHostContext(bridge.getHostContext());
  run(async () => { await bridge.connect(); applyHostContext(bridge.getHostContext()); });
}
