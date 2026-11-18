/* Image choices belong to this browser, not to the server-wide text model. */
(() => {
  const panel = document.getElementById('chatImageOptions');
  const toggle = document.getElementById('imageOptionsToggle');
  const select = document.getElementById('chatImageModel');
  const note = document.getElementById('imageModelNote');
  const key = 'nova_chat_image_model';
  let selection = null;
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved?.model && saved?.provider) selection = { provider: saved.provider, model: saved.model };
  } catch (_) { /* Auto on first use. */ }

  async function open(opened) {
    panel.hidden = !opened;
    toggle.setAttribute('aria-expanded', String(opened));
    if (!opened) return;
    select.disabled = true;
    note.textContent = 'Загружаю модели…';
    try {
      const response = await fetch('/api/media/agent-image-models');
      if (!response.ok) throw new Error('Не удалось загрузить модели');
      const { models } = await response.json();
      select.replaceChildren(new Option('Авто', ''));
      for (const item of models) {
        const value = JSON.stringify({ provider: item.provider, model: item.model });
        const label = [item.name, item.provider_name, item.pricing_label, item.can_edit && 'редактирование'].filter(Boolean).join(' · ');
        select.add(new Option(label, value));
      }
      const value = selection && JSON.stringify(selection);
      // Keep a stale pin visible: never switch a user's explicit choice silently.
      if (value && !Array.from(select.options).some((o) => o.value === value)) {
        select.add(new Option(`${selection.model} · недоступна`, value));
      }
      select.value = value || '';
      note.textContent = models.length ? 'Выбор применяется только к изображениям.' : 'Нет доступных моделей изображений. Проверьте настройки провайдера.';
    } catch (error) {
      // The user can still clear the pin without a live catalog.
      select.replaceChildren(new Option('Авто', ''));
      if (selection) select.add(new Option(selection.model, JSON.stringify(selection), true, true));
      note.textContent = error.message || 'Не удалось загрузить модели';
    } finally { select.disabled = false; }
  }

  select.addEventListener('change', () => {
    selection = select.value ? JSON.parse(select.value) : null;
    if (selection) localStorage.setItem(key, select.value);
    else localStorage.removeItem(key);
  });
  toggle.addEventListener('click', () => open(panel.hidden));

  function compose(source, file) {
    if (isTyping) return;
    const edit = Boolean(source || file);
    // Replacing the target is not the same as toggling the old mode off.
    clearInputMode(true);
    activateMode({ prefix: edit ? 'Отредактируй изображение: ' : 'Нарисуй ',
      label: edit ? `✏️ ${file?.name || source}` : '🎨 Новое изображение',
      placeholder: edit ? 'Что изменить на картинке?' : 'Опишите изображение…',
      imageTask: true, imageSource: source, imageFile: file });
    if (!autoSearchEnabled()) showNotification('Для генерации включите «Инструменты».', 'info');
  }

  document.getElementById('editImageFile').addEventListener('click', () => {
    pickMediaFile('image/png,image/jpeg,image/webp', (file) => {
      if (file.size > 8 * 1024 * 1024) return showNotification('Изображение больше 8 МБ.', 'warn');
      compose('', file);
      open(false);
    });
  });
  chatContainer.addEventListener('click', (event) => {
    const button = event.target.closest('[data-edit-image]');
    if (button && !isTyping) { compose(button.dataset.editImage, null); open(true); }
  });
  window.ChatImages = { selection: () => selection, start: () => { compose('', null); open(true); } };
})();
