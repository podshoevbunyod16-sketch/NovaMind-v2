(() => {
  const panel = document.getElementById('agentWorkspace');
  const list = document.getElementById('workspaceFiles');
  const preview = document.getElementById('workspacePreview');
  const zip = document.getElementById('workspaceZip');
  let selectedChat = '';
  let openVersion = 0;
  const terminalForm = document.getElementById('workspaceTerminalForm');
  const terminalLog = document.getElementById('workspaceTerminalLog');
  const query = () => `?chat_id=${encodeURIComponent(selectedChat)}`;
  const status = (message) => {
    const node = document.getElementById('workspaceGitStatus');
    if (node) node.textContent = message;
  };
  const getChatId = async () => {
    try {
      const store = typeof historyLoad === 'function' ? historyLoad() : null;
      const chat = store?.chats?.find((item) => item.id === store.activeId);
      if (chat?.serverId) return chat.serverId;
      if (store?.resetServer || chat) return '';  // An unbound new chat must not reuse the previous session.
      const response = await fetch('/api/history');
      const data = await response.json();
      return data.chat_id || '';
    } catch (_) { return ''; }
  };
  window.toggleWorkspace = async (open) => {
    panel?.classList.toggle('open', Boolean(open));
    panel?.setAttribute('aria-hidden', String(!open));
    const version = ++openVersion;
    selectedChat = '';
    terminalLog.textContent = '';
    preview.hidden = true;
    zip.removeAttribute('href');
    if (!open) return;
    window.showWorkspaceTab('files');
    const chatId = await getChatId();
    if (version !== openVersion) return;
    selectedChat = chatId;
    if (!selectedChat) { list.textContent = 'Создайте или откройте чат.'; return; }
    const query = `?chat_id=${encodeURIComponent(selectedChat)}`;
    zip.href = `/api/agent/workspace/zip${query}`;
    list.textContent = 'Загружаю…';
    try {
      const response = await fetch(`/api/agent/workspace/tree${query}`);
      const data = await response.json();
      if (version !== openVersion) return;
      if (!response.ok) throw new Error(data.error || 'Не удалось загрузить файлы');
      list.replaceChildren();
      if (!data.files?.length) { list.textContent = 'В папке пока нет файлов.'; return; }
      for (const item of data.files) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'workspace-file';
        button.textContent = `${item.type === 'directory' ? '📁' : '📄'} ${item.path}`;
        button.disabled = item.type !== 'file';
        button.addEventListener('click', async () => {
          const file = await fetch(`/api/agent/workspace/file${query}&path=${encodeURIComponent(item.path)}`);
          const body = await file.json();
          preview.hidden = false;
          preview.textContent = file.ok ? body.content : (body.error || 'Ошибка чтения');
        });
        list.append(button);
      }
    } catch (error) { list.textContent = error.message || 'Ошибка загрузки workspace.'; }
  };
  window.showWorkspaceTab = (tab) => {
    const files = document.getElementById('workspaceTabFiles');
    const diff = document.getElementById('workspaceTabDiff');
    if (files) files.hidden = tab !== 'files';
    if (diff) diff.hidden = tab !== 'diff';
    document.getElementById('workspaceTabTerminal').hidden = tab !== 'terminal';
    if (tab === 'terminal') {
      const available = selectedChat && window.Linux?.hasLinux();
      terminalForm.querySelectorAll('input, button').forEach((el) => { el.disabled = !available; });
      if (!available) terminalLog.textContent = selectedChat
        ? 'Терминал недоступен. Проверьте вход и TERMINAL_ENABLED.' : 'Сначала откройте чат.';
    }
    if (tab === 'diff') window.refreshWorkspaceDiff();
  };
  terminalForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const version = openVersion;
    const command = document.getElementById('workspaceCommand').value.trim();
    if (!selectedChat || !command || !window.Linux?.hasLinux() || terminalForm.dataset.busy) return;
    terminalForm.dataset.busy = '1';
    terminalLog.textContent += `$ ${command}\n`;
    try {
      const response = await fetch('/api/terminal/run', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command, chat_id: selectedChat }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (version === openVersion) terminalLog.textContent += [data.stdout, data.stderr, `Код: ${data.code}\n`].filter(Boolean).join('\n');
    } catch (error) {
      if (version === openVersion) terminalLog.textContent += `${error.message}\n`;
    } finally {
      delete terminalForm.dataset.busy;
      terminalLog.textContent = terminalLog.textContent.slice(-30000);
    }
  });
  window.refreshWorkspaceDiff = async () => {
    if (!selectedChat) selectedChat = await getChatId();
    try {
      const response = await fetch(`/api/agent/github/diff${query()}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Нет рабочей копии GitHub');
      document.getElementById('workspaceDiff').textContent = data.diff || 'Рабочее дерево чистое.';
      status(`Ветка ${data.branch}${data.status ? ` · ${data.status}` : ' · чисто'}`);
    } catch (error) { status(error.message || 'Копия репозитория пока не выбрана.'); }
  };
  document.getElementById('githubCloneForm')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!selectedChat) selectedChat = await getChatId();
    const repository = document.getElementById('githubRepository').value.trim();
    const runId = (window.crypto?.randomUUID?.() || `run-${Date.now()}`).replace(/[^A-Za-z0-9_-]/g, '');
    status('Клонирую репозиторий…');
    try {
      const response = await fetch(`/api/agent/github/clone${query()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repository, run_id: runId }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Не удалось склонировать репозиторий');
      status(`Копия готова в ветке ${data.branch}`);
      await window.refreshWorkspaceDiff();
    } catch (error) { status(error.message || 'Ошибка клонирования'); }
  });
  window.commitWorkspaceChanges = async () => {
    const message = window.prompt('Сообщение коммита', 'Изменения NovaMind');
    if (message === null) return;
    try {
      const response = await fetch(`/api/agent/github/commit${query()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Коммит не создан');
      status(`Коммит ${data.commit.slice(0, 10)} в ${data.branch}`);
      await window.refreshWorkspaceDiff();
    } catch (error) { status(error.message || 'Ошибка коммита'); }
  };
  window.publishWorkspaceChanges = async () => {
    if (!window.confirm('Отправить ветку novamind в GitHub и создать Pull Request?')) return;
    try {
      const response = await fetch(`/api/agent/github/publish${query()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true, base: 'main' }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Публикация запрещена');
      status(data.pull_request || data.pull_request_error || `Отправлена ветка ${data.branch}`);
    } catch (error) { status(error.message || 'Ошибка публикации'); }
  };
})();
