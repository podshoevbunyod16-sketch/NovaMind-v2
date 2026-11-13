/* ═══════════════════════════════════════════════════════════════════
   NovaMind · SIGNATURE — квитанции и совет моделей
   ───────────────────────────────────────────────────────────────────
   window.NovaReceipts.open(id)  — карточка проверяемой квитанции;
   window.NovaCouncil.open(text) — совет моделей по вопросу.

   Файл самодостаточен: ни jQuery, ни сборки, ни внешних шрифтов.
   Используется и в чате, и на странице /trust.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const state = { overlay: null, modal: null, lastFocus: null, attached: false };

  // ── утилиты ──────────────────────────────────────────────────
  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function shortHash(hash, size) {
    const text = String(hash || '');
    const n = size || 12;
    if (!text) return '—';
    return text.length <= n * 2 ? text : text.slice(0, n) + '…' + text.slice(-6);
  }

  function fmtDate(seconds) {
    const date = new Date((Number(seconds) || 0) * 1000);
    if (!Number.isFinite(date.getTime())) return '—';
    return date.toLocaleString('ru-RU', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
    });
  }

  function fmtTokens(count) {
    const n = Number(count) || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(1) + ' млн';
    if (n >= 1000) return (n / 1000).toFixed(1) + ' тыс.';
    return String(n);
  }

  async function api(url, options) {
    const response = await fetch(url, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, options || {}));
    let data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || ('HTTP ' + response.status));
    }
    return data;
  }

  function copy(text) {
    const area = document.createElement('textarea');
    area.value = String(text || '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    area.remove();
    if (!ok && navigator.clipboard) { navigator.clipboard.writeText(String(text || '')).catch(() => {}); }
  }

  // ── модальное окно ───────────────────────────────────────────
  function ensureOverlay() {
    if (state.overlay) return state.overlay;
    const overlay = document.createElement('div');
    overlay.className = 'sig-overlay';
    overlay.setAttribute('hidden', '');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.innerHTML = '<div class="sig-modal" tabindex="-1"></div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) close();
    });
    state.overlay = overlay;
    state.modal = overlay.querySelector('.sig-modal');
    return overlay;
  }

  function openModal(html, wide) {
    const overlay = ensureOverlay();
    state.lastFocus = document.activeElement;
    state.modal.className = 'sig-modal' + (wide ? ' sig-wide' : '');
    state.modal.innerHTML = html;
    overlay.removeAttribute('hidden');
    document.body.style.overflow = 'hidden';
    state.modal.focus({ preventScroll: true });
    const closeBtn = state.modal.querySelector('[data-sig-close]');
    if (closeBtn) closeBtn.addEventListener('click', close);
    return state.modal;
  }

  function close() {
    if (!state.overlay) return;
    state.overlay.setAttribute('hidden', '');
    state.modal.innerHTML = '';
    document.body.style.overflow = '';
    if (state.lastFocus && state.lastFocus.focus) {
      try { state.lastFocus.focus({ preventScroll: true }); } catch (_) {}
    }
  }

  function head(title, sub) {
    return '<div class="sig-head">'
      + '<div><h2>' + title + '</h2>'
      + (sub ? '<div class="sig-sub">' + sub + '</div>' : '')
      + '</div>'
      + '<button class="sig-close" type="button" data-sig-close aria-label="Закрыть">✕</button>'
      + '</div>';
  }

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.overlay && !state.overlay.hasAttribute('hidden')) close();
  });

  // ═════════════════════════════════════════════════════════════
  // NOVA RECEIPTS
  // ═════════════════════════════════════════════════════════════
  const KIND_LABEL = { chat: 'Ответ чата', agent: 'Работа агента', council: 'Совет моделей' };

  async function openReceipt(id) {
    if (!id) return;
    const modal = openModal(
      head('🧾 Квитанция #' + esc(id), 'Проверяемая запись NovaMind')
      + '<div class="council-loading"><div class="council-skeleton"></div>'
      + '<p>Проверяю хэш и подпись…</p></div>');
    try {
      const data = await api('/api/receipts/' + encodeURIComponent(id));
      modal.innerHTML = renderReceipt(data.receipt);
      const closeBtn = modal.querySelector('[data-sig-close]');
      if (closeBtn) closeBtn.addEventListener('click', close);
      const copyBtn = modal.querySelector('[data-receipt-copy]');
      if (copyBtn) copyBtn.addEventListener('click', () => {
        copy(JSON.stringify(data.receipt, null, 2));
        copyBtn.textContent = 'Скопировано ✓';
        setTimeout(() => { copyBtn.textContent = 'Скопировать JSON'; }, 1500);
      });
      const verifyBtn = modal.querySelector('[data-receipt-verify]');
      if (verifyBtn) verifyBtn.addEventListener('click', async () => {
        verifyBtn.disabled = true;
        try {
          const check = await api('/api/receipts/' + encodeURIComponent(id));
          const verdict = check.receipt.verification || {};
          modal.querySelector('.receipt-verdict').outerHTML = verdictBadge(verdict);
          verifyBtn.textContent = verdict.ok ? 'Цепочка цела ✓' : 'Есть поломка';
        } catch (error) {
          verifyBtn.textContent = 'Не удалось';
        } finally {
          verifyBtn.disabled = false;
        }
      });
    } catch (error) {
      modal.innerHTML = head('🧾 Квитанция #' + esc(id), 'Nova Receipts')
        + '<p class="sig-note">Не удалось загрузить квитанцию: ' + esc(error.message) + '</p>'
        + '<div class="sig-actions"><button class="glass-btn" type="button" data-sig-close>Закрыть</button></div>';
      const closeBtn = modal.querySelector('[data-sig-close]');
      if (closeBtn) closeBtn.addEventListener('click', close);
    }
  }

  function verdictBadge(verification) {
    const ok = verification && verification.ok;
    return '<div class="receipt-verdict lg-chip ' + (ok ? 'ok' : 'err') + '">'
      + (ok ? '✚ Цепочка проверена' : '✕ Цепочка повреждена: '
        + esc((verification && verification.reasons || []).join(', ')))
      + '</div>';
  }

  function renderReceipt(receipt) {
    if (!receipt) return head('🧾 Квитанция', 'Nova Receipts') + '<p class="sig-note">Пусто</p>';
    const tools = Array.isArray(receipt.tools) ? receipt.tools : [];
    const sources = Array.isArray(receipt.sources) ? receipt.sources : [];
    const versions = receipt.prompt_versions && typeof receipt.prompt_versions === 'object'
      ? Object.entries(receipt.prompt_versions).slice(0, 4) : [];
    const meta = receipt.meta && typeof receipt.meta === 'object' ? receipt.meta : {};
    const metaRows = Object.entries(meta)
      .filter(([, value]) => value !== null && value !== '' && typeof value !== 'object')
      .slice(0, 6);

    return head('🧾 Квитанция #' + esc(receipt.id),
      esc(KIND_LABEL[receipt.kind] || receipt.kind) + ' · ' + esc(fmtDate(receipt.created_at)))
      + '<div class="sig-grid two">'
      + row('Модель', receipt.model || '—')
      + row('Провайдер', receipt.provider || '—')
      + row('Токенов', fmtTokens(receipt.tokens))
      + row('Стоимость', receipt.cost_usd > 0 ? '$' + Number(receipt.cost_usd).toFixed(4) : 'бесплатно')
      + (tools.length ? row('Инструменты', tools.slice(0, 6).join(', ')) : '')
      + (sources.length ? row('Источников', String(sources.length)) : '')
      + '</div>'
      + metaRows.map(([key, value]) => row(key, String(value))).join('')
      + '<div class="chain-visual">'
      + '<div class="chain-node"><strong>Предыдущая</strong><div class="hash">'
      + esc(receipt.prev_hash || '—') + '</div></div>'
      + '<div class="chain-node"><strong>Эта запись</strong><div class="hash">'
      + esc(receipt.hash || '—') + '</div></div>'
      + '<div class="chain-node"><strong>Подпись HMAC-SHA256</strong><div class="hash">'
      + esc(shortHash(receipt.signature, 32)) + '</div></div>'
      + '</div>'
      + '<div class="sig-grid">' + verdictBadge(receipt.verification) + '</div>'
      + '<div class="sig-grid two" style="margin-top:10px">'
      + row('Вход · SHA-256', shortHash(receipt.input_sha256, 18))
      + row('Выход · SHA-256', shortHash(receipt.output_sha256, 18))
      + row('Символов', (receipt.input_chars || 0) + ' → ' + (receipt.output_chars || 0))
      + row('Алгоритм', receipt.algorithm || 'sha256+hmac-sha256/v1')
      + '</div>'
      + (versions.length ? '<p class="sig-note">Промпты: '
        + versions.map(([name, ver]) => esc(name) + ' → ' + esc(ver)).join(' · ') + '</p>' : '')
      + '<p class="sig-note">Квитанция не хранит текст ответа. Она подтверждает '
      + 'SHA-256 содержимого и место записи в цепочке: измените или удалите запись — '
      + 'проверка это покажет.</p>'
      + '<div class="sig-actions">'
      + '<button class="glass-btn is-sm" type="button" data-receipt-copy>Скопировать JSON</button>'
      + '<button class="glass-btn is-sm is-primary" type="button" data-receipt-verify>Проверить снова</button>'
      + '</div>';
  }

  function row(key, value) {
    return '<div class="sig-row"><span class="k">' + esc(key) + '</span>'
      + '<span class="v">' + esc(value) + '</span></div>';
  }

  // ═════════════════════════════════════════════════════════════
  // NOVA COUNCIL
  // ═════════════════════════════════════════════════════════════
  async function openCouncil(question) {
    const prefill = String(question || '').trim();
    if (!prefill) return renderCouncilAsk('');
    renderCouncilLoading(prefill);
    try {
      const data = await api('/api/council/ask', {
        method: 'POST',
        body: JSON.stringify({ question: prefill }),
      });
      renderCouncilResult(data, prefill);
    } catch (error) {
      renderCouncilError(error.message, prefill);
    }
  }

  function renderCouncilAsk(prefill) {
    const modal = openModal(
      head('⚖️ Совет моделей',
        'Несколько моделей отвечают независимо — видно, где они согласны, а где нет')
      + '<div class="council-ask">'
      + '<textarea id="councilQuestion" maxlength="4000" '
      + 'placeholder="Задайте вопрос, на который нужен не один взгляд…">' + esc(prefill) + '</textarea>'
      + '<div class="row"><button class="glass-btn is-primary" type="button" data-council-go>'
      + 'Собрать совет</button></div></div>'
      + '<p class="sig-note">Совет тратит несколько запросов вместо одного, поэтому у него '
      + 'отдельный лимит. Ответы кешируются только в этом окне и не сохраняются в чат.</p>',
      true);
    const input = modal.querySelector('#councilQuestion');
    const go = modal.querySelector('[data-council-go]');
    if (input) setTimeout(() => input.focus({ preventScroll: true }), 60);
    const submit = () => {
      const text = (input && input.value || '').trim();
      if (!text) { if (input) input.focus(); return; }
      renderCouncilLoading(text);
      api('/api/council/ask', { method: 'POST', body: JSON.stringify({ question: text }) })
        .then((data) => renderCouncilResult(data, text))
        .catch((error) => renderCouncilError(error.message, text));
    };
    if (go) go.addEventListener('click', submit);
    if (input) input.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') submit();
    });
  }

  function renderCouncilLoading(question) {
    const modal = openModal(
      head('⚖️ Совет собирается', 'Модели думают параллельно')
      + '<div class="council-loading">'
      + '<div class="council-skeleton"></div><div class="council-skeleton"></div>'
      + '<div class="council-skeleton"></div>'
      + '<p>Вопрос: ' + esc(String(question).slice(0, 180)) + '</p>'
      + '<p>Обычно это 5–20 секунд. Окно можно закрыть — запрос уже отправлен.</p>'
      + '</div>', true);
    return modal;
  }

  function renderCouncilError(message, question) {
    const modal = openModal(
      head('⚖️ Совет не собрался', 'Nova Council')
      + '<p class="sig-note">' + esc(message) + '</p>'
      + '<div class="sig-actions">'
      + '<button class="glass-btn is-sm" type="button" data-council-retry>Повторить</button>'
      + '<a class="glass-btn is-sm is-primary" href="/settings">Настроить модели</a>'
      + '</div>', true);
    const retry = modal.querySelector('[data-council-retry]');
    if (retry) retry.addEventListener('click', () => openCouncil(question));
  }

  function renderCouncilResult(data, question) {
    const seats = Array.isArray(data.seats) ? data.seats : [];
    const consensus = Array.isArray(data.consensus) ? data.consensus : [];
    const disagreements = Array.isArray(data.disagreements) ? data.disagreements : [];
    const agreement = typeof data.agreement === 'number' ? Math.round(data.agreement) : null;
    const receipt = data.receipt || {};

    const consensusHtml = consensus.length
      ? '<div class="council-section-title">Согласны все или большинство</div>'
        + '<ul class="council-points">'
        + consensus.map((item) => '<li>' + esc(item.text)
          + '<span class="who">поддержали ' + esc(item.supported_by) + ' из '
          + esc(seats.filter((seat) => seat.status === 'ok').length) + '</span></li>').join('')
        + '</ul>'
      : '';

    const disagreeHtml = disagreements.length
      ? '<div class="council-section-title">Расходятся</div>'
        + '<ul class="council-points disagree">'
        + disagreements.map((group) => group.points.map((point) => '<li>' + esc(point)
          + '<span class="who">только ' + esc(group.lens) + '</span></li>').join('')).join('')
        + '</ul>'
      : '';

    const seatsHtml = seats.map((seat) => {
      const status = seat.status === 'ok' ? 'ok' : 'error';
      const body = status === 'ok' ? seat.answer : (seat.error || 'Модель не ответила');
      return '<details class="council-seat" data-status="' + status + '"'
        + (status === 'ok' ? '' : ' open') + '>'
        + '<summary><span class="seat-ico">' + esc(seat.icon || '•') + '</span>'
        + '<span class="seat-lens">' + esc(seat.lens_name || 'Участник') + '</span>'
        + '<span class="seat-model">' + esc((seat.provider || '') + (seat.model ? ' · ' + seat.model : ''))
        + '</span></summary>'
        + '<div class="seat-body">' + esc(body) + '</div></details>';
    }).join('');

    const modal = openModal(
      head('⚖️ Совет моделей', esc(String(question).slice(0, 160)))
      + '<div class="council-verdict">'
      + '<div class="agreement-ring" style="--value:' + (agreement === null ? 0 : agreement) + '">'
      + '<span>' + (agreement === null ? '—' : agreement + '%') + '</span></div>'
      + '<div class="council-verdict-txt"><h3>' + esc(data.verdict_label || 'Результат') + '</h3>'
      + '<p>' + esc(data.recommendation || '') + '</p></div></div>'
      + consensusHtml + disagreeHtml
      + '<div class="council-section-title">Позиции участников</div>'
      + '<div class="council-seats">' + seatsHtml + '</div>'
      + '<div class="sig-actions">'
      + (receipt.id ? '<button class="glass-btn is-sm" type="button" data-council-receipt>'
        + '🧾 Квитанция #' + esc(receipt.id) + '</button>' : '')
      + '<button class="glass-btn is-sm" type="button" data-council-again>Новый вопрос</button>'
      + '</div>'
      + '<p class="sig-note">Согласие считается по смыслу предложений: два тезиса совпадают, '
      + 'если пересечение значимых слов ≥ 50%. Это метрика близости формулировок, а не '
      + 'гарантия истины — спорные пункты стоит проверить.</p>', true);

    const receiptBtn = modal.querySelector('[data-council-receipt]');
    if (receiptBtn) receiptBtn.addEventListener('click', () => openReceipt(receipt.id));
    const again = modal.querySelector('[data-council-again]');
    if (again) again.addEventListener('click', () => renderCouncilAsk(''));
  }

  // ═════════════════════════════════════════════════════════════
  // ПУБЛИЧНЫЙ ИНТЕРФЕЙС
  // ═════════════════════════════════════════════════════════════
  window.NovaReceipts = { open: openReceipt, close: close };
  window.NovaCouncil = { open: openCouncil, close: close };

  // Делегирование: любые элементы с data-receipt-id / data-council-ask
  // работают без отдельных обработчиков на странице.
  document.addEventListener('click', (event) => {
    const receiptTrigger = event.target.closest('[data-receipt-id]');
    if (receiptTrigger) {
      event.preventDefault();
      openReceipt(receiptTrigger.getAttribute('data-receipt-id'));
      return;
    }
    const councilTrigger = event.target.closest('[data-council-ask]');
    if (councilTrigger) {
      event.preventDefault();
      openCouncil(councilTrigger.getAttribute('data-council-ask') || '');
    }
  });
})();
