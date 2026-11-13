/* ═══════════════════════════════════════════════════════════════════
   NovaMind · Trust Center — клиент страницы /trust
   Читает уже существующие API и рисует: сводку, цепочку квитанций,
   состояние готовности и историю советов моделей.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const THEMES = ['aurora', 'midnight', 'sunset', 'light'];

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtTokens(count) {
    const n = Number(count) || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(1) + ' млн';
    if (n >= 1000) return (n / 1000).toFixed(1) + ' тыс.';
    return String(n);
  }

  function fmtCost(value) {
    const n = Number(value) || 0;
    if (n <= 0) return '0';
    if (n < 0.01) return '<$0.01';
    return '$' + n.toFixed(2);
  }

  function fmtDate(seconds) {
    const date = new Date((Number(seconds) || 0) * 1000);
    if (!Number.isFinite(date.getTime()) || !seconds) return '—';
    return date.toLocaleString('ru-RU', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
    });
  }

  function shortHash(hash, size) {
    const text = String(hash || '');
    const n = size || 14;
    return text.length <= n + 8 ? (text || '—') : text.slice(0, n) + '…' + text.slice(-6);
  }

  async function getJSON(url) {
    const response = await fetch(url, { headers: { 'Accept': 'application/json' } });
    let data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }

  // ── тема ────────────────────────────────────────────────────────
  function initTheme() {
    let saved = 'aurora';
    try { saved = localStorage.getItem('nova_theme') || 'aurora'; } catch (_) {}
    document.documentElement.setAttribute('data-glass-theme', saved);
    const button = $('btnTheme');
    if (!button) return;
    button.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-glass-theme') || 'aurora';
      const next = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
      document.documentElement.setAttribute('data-glass-theme', next);
      try { localStorage.setItem('nova_theme', next); } catch (_) {}
    });
  }

  // ── состояние окружения ─────────────────────────────────────────
  async function loadEnvironment() {
    const chip = $('envChip');
    try {
      const info = await getJSON('/version');
      if (chip) {
        chip.textContent = (info.production ? 'production' : info.env || 'dev')
          + ' · v' + (info.version || '—');
        chip.className = 'lg-chip ' + (info.ok ? 'ok' : 'warn');
        chip.title = (info.build ? 'сборка ' + info.build + ' · ' : '')
          + 'Python ' + (info.python || '?');
      }
      const versionChip = $('versionChip');
      if (versionChip) {
        versionChip.textContent = 'NovaMind v' + (info.version || '—')
          + ' · ' + (info.features || []).join(' · ');
      }
    } catch (_) {
      if (chip) { chip.textContent = 'offline'; chip.className = 'lg-chip warn'; }
    }

    try {
      const ready = await getJSON('/readyz');
      const readyEl = $('statReady');
      const sub = $('statReadySub');
      if (readyEl) {
        readyEl.textContent = ready.ready ? 'готов' : 'нет';
        readyEl.style.color = ready.ready ? 'var(--ok)' : 'var(--err)';
      }
      if (sub) {
        const checks = ready.checks || {};
        const okCount = Object.values(checks).filter((item) => item && item.ok).length;
        sub.textContent = okCount + '/' + Object.keys(checks).length + ' проверок';
      }
    } catch (_) {
      const readyEl = $('statReady');
      if (readyEl) { readyEl.textContent = '?'; }
    }
  }

  // ── квитанции ───────────────────────────────────────────────────
  const KIND = { chat: 'Ответ чата', agent: 'Агент', council: 'Совет' };

  async function loadReceipts() {
    const list = $('receiptList');
    try {
      const data = await getJSON('/api/receipts?limit=60');
      const items = data.items || [];
      const stats = data.stats || {};
      setStats(stats);
      if (!items.length) {
        // Пустая цепочка: показываем, как это выглядит, честно помечая пример.
        list.innerHTML = demoChainHtml();
        return;
      }
      list.removeAttribute('data-empty-shown');
      list.innerHTML = items.map(receiptHtml).join('');
    } catch (error) {
      if (list) {
        list.innerHTML = '<div class="sig-note">Квитанции недоступны: ' + esc(error.message) + '</div>';
      }
    }
  }

  function setStats(stats) {
    const count = $('statReceipts');
    if (count) count.textContent = fmtTokens(stats.total || 0);
    const tokens = $('statTokens');
    if (tokens) tokens.textContent = stats.tokens ? fmtTokens(stats.tokens) : '0';
    const cost = $('statCost');
    if (cost) {
      cost.textContent = fmtCost(stats.cost_usd);
      cost.style.color = stats.cost_usd > 0 ? 'var(--warn)' : 'var(--ok)';
    }
    const head = $('headChip');
    if (head && stats.head_hash) head.textContent = 'голова ' + shortHash(stats.head_hash, 10);
  }

  function demoChainHtml() {
    const sample = [
      { id: '1', kind: 'Ответ чата', hash: '9f2c41d7a8e05b3c…1d7e', prev: '000000000000' },
      { id: '2', kind: 'Агент', hash: 'c71b9a30f4d2e68a…8a02', prev: '9f2c41d7a8e0' },
      { id: '3', kind: 'Совет', hash: '4e8d2fb6a1c79035…c4f1', prev: 'c71b9a30f4d2' },
    ];
    return '<div class="trust-demo-note">Так будет выглядеть ваша цепочка. '
      + 'Задайте первый вопрос в чате — и квитанции появятся здесь автоматически.</div>'
      + sample.map((item) => '<div class="receipt-item is-demo">'
        + '<span class="receipt-item-top">'
        + '<span class="id">#' + item.id + '</span>'
        + '<span class="kind">' + item.kind + '</span>'
        + '<span class="who-demo">пример</span></span>'
        + '<span class="receipt-item-hash">' + item.hash + ' ← ' + item.prev + '</span>'
        + '</div>').join('');
  }

  function receiptHtml(item) {
    const bits = [];
    if (item.model) bits.push(esc(item.model));
    if (item.tokens) bits.push(fmtTokens(item.tokens) + ' токенов');
    if (item.cost_usd > 0) bits.push(fmtCost(item.cost_usd));
    if (item.meta && item.meta.agreement !== undefined && item.meta.agreement !== null) {
      bits.push('согласие ' + item.meta.agreement + '%');
    }
    return '<button type="button" class="receipt-item" data-receipt-id="' + esc(item.id) + '">'
      + '<span class="receipt-item-top">'
      + '<span class="id">#' + esc(item.id) + '</span>'
      + '<span class="kind">' + esc(KIND[item.kind] || item.kind) + '</span>'
      + '<span class="when">' + esc(fmtDate(item.created_at)) + '</span>'
      + '</span>'
      + '<span class="receipt-item-hash">' + esc(item.hash)
      + ' ← ' + esc(shortHash(item.prev_hash, 18)) + '</span>'
      + (bits.length ? '<span class="receipt-item-meta">' + bits.join('<span>·</span>') + '</span>' : '')
      + '</button>';
  }

  async function verifyChain(showAlert) {
    const badge = $('chainBadge');
    if (badge) {
      badge.dataset.state = 'checking';
      badge.innerHTML = '<span class="lg-dot pulse"></span><span>Проверяю '
        + 'цепочку…</span>';
    }
    const button = $('btnVerify');
    if (button) button.disabled = true;
    try {
      const data = await getJSON('/api/receipts/verify');
      const report = data.report || {};
      if (badge) {
        badge.dataset.state = report.ok ? 'ok' : 'bad';
        badge.innerHTML = '<span class="lg-dot ' + (report.ok ? 'ok' : 'err') + '"></span>'
          + '<span>' + (report.ok
            ? 'Цепочка цела: ' + report.checked + ' квитанций, голова '
              + shortHash(report.head_hash, 12)
            : 'Цепочка повреждена: ' + report.broken_count + ' из ' + report.checked
              + ' · первая поломка #' + (report.broken[0] ? report.broken[0].id : '?'))
          + '</span>';
      }
      const mine = report.mine || {};
      setStats(mine.total !== undefined ? mine : {});
      if (!report.ok && showAlert !== false && window.NovaReceipts) {
        window.NovaReceipts.open(report.broken[0] && report.broken[0].id);
      }
    } catch (error) {
      if (badge) {
        badge.dataset.state = 'bad';
        badge.innerHTML = '<span class="lg-dot err"></span><span>Проверка не удалась: '
          + esc(error.message) + '</span>';
      }
    } finally {
      if (button) button.disabled = false;
    }
  }

  // ── совет ───────────────────────────────────────────────────────
  async function loadCouncilHistory() {
    const box = $('councilHistory');
    try {
      const data = await getJSON('/api/council/history');
      const items = data.items || [];
      if (!items.length) {
        box.innerHTML = '<div class="council-item is-demo">'
          + '<div class="council-item-q">Стоит ли переписывать сервис на async?</div>'
          + '<div class="council-item-foot">'
          + '<span class="agreement-bar"><span style="width:64%"></span></span>'
          + '<span class="agreement-value">64%</span>'
          + '<span>3 места · пример</span></div></div>';
        return;
      }
      box.innerHTML = items.map((item) => {
        const value = typeof item.agreement === 'number' ? Math.round(item.agreement) : 0;
        return '<div class="council-item">'
          + '<div class="council-item-q">' + esc(item.question || 'Без текста') + '</div>'
          + '<div class="council-item-foot">'
          + '<span class="agreement-bar"><span style="width:' + value + '%"></span></span>'
          + '<span class="agreement-value">' + (item.agreement === null ? '—' : value + '%') + '</span>'
          + '<span>' + esc(item.seats) + ' мест · ' + esc(fmtDate(item.created_at)) + '</span>'
          + (item.receipt_id ? '<button class="glass-btn is-sm is-ghost" type="button" '
            + 'data-receipt-id="' + esc(item.receipt_id) + '">🧾</button>' : '')
          + '</div></div>';
      }).join('');
    } catch (_) {
      box.innerHTML = '';
    }
  }

  // ── инициализация ───────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    loadEnvironment();
    loadReceipts();
    loadCouncilHistory();

    const btnVerify = $('btnVerify');
    if (btnVerify) btnVerify.addEventListener('click', () => verifyChain(true));
    const btnCouncil = $('btnCouncil');
    if (btnCouncil && window.NovaCouncil) {
      btnCouncil.addEventListener('click', () => window.NovaCouncil.open(''));
    }
    const btnCouncil2 = $('btnCouncil2');
    if (btnCouncil2 && window.NovaCouncil) {
      btnCouncil2.addEventListener('click', () => window.NovaCouncil.open(''));
    }

    // Первая проверка — сразу, но не блокируя отрисовку.
    setTimeout(() => verifyChain(false), 350);
    setInterval(loadReceipts, 20000);
  });
})();
