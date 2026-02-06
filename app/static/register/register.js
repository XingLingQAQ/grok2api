/**
 * 注册管理页面 JavaScript
 */

let apiKey = '';
let enabled = false;
let running = false;
let eventSource = null;

// 分页状态
let currentPage = 1;
let pageSize = 50;
let totalResults = 0;
let results = [];

// 代理状态
let proxyEnabled = false;
let proxyList = [];

// 初始化
async function init() {
  apiKey = await ensureApiKey();
  if (apiKey === null) return;
  await loadStatus();
  await loadResults();
  await loadProxyStatus();

  // 自动注册面板：仅在注册功能启用时显示
  const autoPanel = document.getElementById('auto-register-panel');
  if (autoPanel) {
    autoPanel.style.display = enabled ? '' : 'none';
  }
  if (enabled) {
    await loadAutoRegisterConfig();
  }
}

document.addEventListener('DOMContentLoaded', init);

// 加载状态
async function loadStatus() {
  try {
    const res = await fetch('/api/v1/admin/register/status', {
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) throw new Error('Failed to load status');
    const data = await res.json();

    enabled = data.enabled;
    running = data.running;

    updateStatusBanner();
    updateStats(data.stats);
    updateButtons();

    if (running) {
      connectSSE();
    }
  } catch (e) {
    console.error('Load status error:', e);
    showToast('加载状态失败', 'error');
  }
}

// 更新状态横幅
function updateStatusBanner() {
  const banner = document.getElementById('status-banner');
  const text = document.getElementById('status-text');

  if (!enabled) {
    banner.className = 'status-banner status-disabled';
    text.textContent = '注册功能未启用，请在配置中设置 register.enabled = true';
  } else if (running) {
    banner.className = 'status-banner status-running';
    text.textContent = '注册任务进行中...';
  } else {
    banner.className = 'status-banner status-enabled';
    text.textContent = '注册功能已启用，可以开始注册';
  }
}

// 更新统计
function updateStats(stats) {
  if (!stats) return;
  document.getElementById('stat-total').textContent = stats.total || 0;
  document.getElementById('stat-success').textContent = stats.success || 0;
  document.getElementById('stat-failed').textContent = stats.failed || 0;
}

// 更新按钮状态
function updateButtons() {
  const btnStart = document.getElementById('btn-start');
  const btnStop = document.getElementById('btn-stop');
  const inputCount = document.getElementById('input-count');
  const inputConcurrent = document.getElementById('input-concurrent');
  const inputMode = document.getElementById('input-mode');

  btnStart.disabled = !enabled || running;
  btnStop.disabled = !running;
  inputCount.disabled = running;
  inputConcurrent.disabled = running;
  inputMode.disabled = running;

  // 进度条
  const progressContainer = document.getElementById('progress-container');
  if (running) {
    progressContainer.classList.remove('hidden');
  } else {
    progressContainer.classList.add('hidden');
  }
}

// 开始注册
async function startRegister() {
  const count = parseInt(document.getElementById('input-count').value) || 1;
  const concurrent = parseInt(document.getElementById('input-concurrent').value) || 8;
  const mode = document.getElementById('input-mode').value || 'normal';
  const proxySelect = document.getElementById('proxy-select');
  const proxy = proxyEnabled ? proxySelect.value : '';

  if (count < 1 || count > 20000) {
    showToast('注册数量必须在 1-20000 之间', 'error');
    return;
  }

  try {
    const res = await fetch('/api/v1/admin/register/start', {
      method: 'POST',
      headers: {
        ...buildAuthHeaders(apiKey),
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ count, concurrent, mode, proxy })
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Start failed');
    }

    running = true;
    updateStatusBanner();
    updateButtons();
    connectSSE();
    showToast('注册任务已启动', 'success');
  } catch (e) {
    console.error('Start error:', e);
    showToast(e.message || '启动失败', 'error');
  }
}

// 停止注册
async function stopRegister() {
  try {
    const res = await fetch('/api/v1/admin/register/stop', {
      method: 'POST',
      headers: buildAuthHeaders(apiKey)
    });

    if (!res.ok) throw new Error('Stop failed');

    running = false;
    disconnectSSE();
    updateStatusBanner();
    updateButtons();
    showToast('注册任务已停止', 'success');
  } catch (e) {
    console.error('Stop error:', e);
    showToast('停止失败', 'error');
  }
}

// 清空结果
async function clearResults() {
  if (!confirm('确定要清空所有注册结果吗？')) return;

  try {
    const res = await fetch('/api/v1/admin/register/clear', {
      method: 'POST',
      headers: buildAuthHeaders(apiKey)
    });

    if (!res.ok) throw new Error('Clear failed');

    results = [];
    totalResults = 0;
    currentPage = 1;
    renderResults();
    document.getElementById('stat-results').textContent = '0';
    showToast('结果已清空', 'success');
  } catch (e) {
    console.error('Clear error:', e);
    showToast('清空失败', 'error');
  }
}

// 加载结果
async function loadResults() {
  try {
    const res = await fetch(`/api/v1/admin/register/results?page=${currentPage}&page_size=${pageSize}`, {
      headers: buildAuthHeaders(apiKey)
    });

    if (!res.ok) throw new Error('Load results failed');

    const data = await res.json();
    results = data.results || [];
    totalResults = data.total || 0;

    document.getElementById('stat-results').textContent = totalResults;
    renderResults();
  } catch (e) {
    console.error('Load results error:', e);
  }
}

// 渲染结果表格
function renderResults() {
  const tbody = document.getElementById('results-table-body');
  const emptyState = document.getElementById('empty-state');

  if (results.length === 0) {
    tbody.innerHTML = '';
    emptyState.classList.remove('hidden');
    updatePagination();
    return;
  }

  emptyState.classList.add('hidden');

  const startIndex = (currentPage - 1) * pageSize;
  tbody.innerHTML = results.map((r, i) => `
    <tr>
      <td>${startIndex + i + 1}</td>
      <td class="text-left font-mono text-xs">${escapeHtml(r.email || '-')}</td>
      <td class="font-mono text-xs">${escapeHtml(r.password || '-')}</td>
      <td class="token-cell" title="${escapeHtml(r.sso_token || '')}">${r.sso_token ? r.sso_token.substring(0, 20) + '...' : '-'}</td>
      <td><span class="status-badge ${r.success ? 'status-success' : 'status-failed'}">${r.success ? '成功' : '失败'}</span></td>
      <td class="error-cell" title="${escapeHtml(r.error || '')}">${escapeHtml(r.error || '-')}</td>
      <td class="time-cell">${formatTime(r.created_at)}</td>
    </tr>
  `).join('');

  updatePagination();
}

// 更新分页
function updatePagination() {
  const totalPages = Math.ceil(totalResults / pageSize) || 1;
  document.getElementById('pagination-info').textContent = `第 ${currentPage} / ${totalPages} 页 · 共 ${totalResults} 条`;
  document.getElementById('page-prev').disabled = currentPage <= 1;
  document.getElementById('page-next').disabled = currentPage >= totalPages;
}

// 分页操作
function goPrevPage() {
  if (currentPage > 1) {
    currentPage--;
    loadResults();
  }
}

function goNextPage() {
  const totalPages = Math.ceil(totalResults / pageSize);
  if (currentPage < totalPages) {
    currentPage++;
    loadResults();
  }
}

function changePageSize() {
  pageSize = parseInt(document.getElementById('page-size').value);
  currentPage = 1;
  loadResults();
}

// 从 Bearer token 中提取实际的 api_key
function extractApiKey() {
  if (!apiKey) return '';
  return apiKey.startsWith('Bearer ') ? apiKey.slice(7) : apiKey;
}

// 导出结果
function exportResults(format) {
  const key = extractApiKey();
  const url = `/api/v1/admin/register/export?format=${format}&api_key=${key}`;
  window.open(url, '_blank');
}

// SSE 连接
function connectSSE() {
  if (eventSource) return;

  const key = extractApiKey();
  const url = `/api/v1/admin/register/stream?api_key=${key}`;
  eventSource = new EventSource(url);

  eventSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      handleSSEEvent(data);
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  };

  eventSource.onerror = () => {
    console.warn('SSE connection error');
    disconnectSSE();
    setTimeout(() => {
      if (running) connectSSE();
    }, 3000);
  };
}

function disconnectSSE() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

// 处理 SSE 事件
function handleSSEEvent(data) {
  if (data.type === 'snapshot') {
    running = data.running;
    updateStats(data.stats);
    document.getElementById('stat-results').textContent = data.results_count || 0;
    updateStatusBanner();
    updateButtons();
    return;
  }

  if (data.event === 'task_started') {
    running = true;
    updateStatusBanner();
    updateButtons();
    return;
  }

  if (data.event === 'task_completed' || data.event === 'task_stopped') {
    running = false;
    disconnectSSE();
    updateStatusBanner();
    updateButtons();
    loadResults();
    showToast('注册任务已完成', 'success');
    return;
  }

  if (data.event === 'register_result') {
    const stats = data.data?.stats;
    if (stats) {
      updateStats(stats);
      updateProgress(stats);
    }
    totalResults++;
    document.getElementById('stat-results').textContent = totalResults;

    // 如果在第一页，追加新结果
    if (currentPage === 1 && results.length < pageSize) {
      results.push(data.data?.result);
      renderResults();
    }
    return;
  }

  if (data.type === 'done') {
    running = false;
    disconnectSSE();
    updateStatusBanner();
    updateButtons();
    return;
  }
}

// 更新进度条
function updateProgress(stats) {
  if (!stats) return;
  const total = stats.total || 1;
  const done = (stats.success || 0) + (stats.failed || 0);
  const percent = Math.round((done / total) * 100);

  document.getElementById('progress-fill').style.width = `${percent}%`;
  document.getElementById('progress-text').textContent = `${done} / ${total} (${percent}%)`;
}

// ==================== 代理池功能 ====================

// 加载代理状态
async function loadProxyStatus() {
  try {
    const res = await fetch('/api/v1/admin/proxy/status', {
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) return;
    const data = await res.json();
    updateProxyStats(data);
    await loadProxyList();
  } catch (e) {
    console.error('Load proxy status error:', e);
  }
}

// 更新代理统计
function updateProxyStats(data) {
  const statsEl = document.getElementById('proxy-stats');
  if (statsEl) {
    statsEl.textContent = `总计 ${data.total || 0} · 存活 ${data.alive || 0}`;
  }
}

// 更新代理进度条
function updateProxyProgress(progress, type) {
  const container = document.getElementById('proxy-progress-container');
  const fill = document.getElementById('proxy-progress-fill');
  const text = document.getElementById('proxy-progress-text');
  if (!container || !fill || !text) return;

  if (!progress) {
    container.classList.add('hidden');
    return;
  }

  container.classList.remove('hidden');
  const done = progress.done || 0;
  const total = progress.total || 1;
  const percent = Math.round((done / total) * 100);
  fill.style.width = `${percent}%`;

  if (type === 'fetch') {
    text.textContent = `抓取中: ${done}/${total} 源 · 新增 ${progress.new || 0} 个代理`;
  } else {
    text.textContent = `测活中: ${done}/${total} · 存活 ${progress.alive || 0}`;
  }
}

// 加载代理列表
async function loadProxyList() {
  try {
    const res = await fetch('/api/v1/admin/proxy/list?limit=100', {
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) return;
    const data = await res.json();
    proxyList = data.proxies || [];
    updateProxySelect();
  } catch (e) {
    console.error('Load proxy list error:', e);
  }
}

// 更新代理下拉框
function updateProxySelect() {
  const select = document.getElementById('proxy-select');
  if (!select) return;

  while (select.options.length > 2) {
    select.remove(2);
  }

  proxyList.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.proxy;
    opt.textContent = `${p.proxy} (${p.latency}ms)`;
    select.appendChild(opt);
  });
}

// 切换代理启用状态
function toggleProxy() {
  const checkbox = document.getElementById('proxy-enabled');
  const select = document.getElementById('proxy-select');
  proxyEnabled = checkbox.checked;
  select.disabled = !proxyEnabled;
}

// 抓取代理
async function fetchProxies() {
  const btn = document.getElementById('btn-fetch-proxy');
  btn.disabled = true;
  btn.innerHTML = '<svg class="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"></path></svg> 抓取中...';

  try {
    const res = await fetch('/api/v1/admin/proxy/fetch', {
      method: 'POST',
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) throw new Error('Fetch failed');
    showToast('开始抓取代理...', 'success');
    pollProxyStatus('fetch');
  } catch (e) {
    console.error('Fetch proxies error:', e);
    showToast('抓取失败', 'error');
    btn.disabled = false;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"></path></svg> 抓取代理';
  }
}

// 测活代理
async function checkProxies() {
  const btn = document.getElementById('btn-check-proxy');
  btn.disabled = true;
  btn.innerHTML = '<svg class="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"></path></svg> 测活中...';

  try {
    const res = await fetch('/api/v1/admin/proxy/check', {
      method: 'POST',
      headers: {
        ...buildAuthHeaders(apiKey),
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ concurrent: 100 })
    });
    if (!res.ok) throw new Error('Check failed');
    showToast('开始测活代理...', 'success');
    pollProxyStatus('check');
  } catch (e) {
    console.error('Check proxies error:', e);
    showToast('测活失败', 'error');
    btn.disabled = false;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg> 测活';
  }
}

// 轮询代理状态（带进度）
let proxyPollTimer = null;
let proxyPollType = null;
function pollProxyStatus(type) {
  if (proxyPollTimer) clearInterval(proxyPollTimer);
  proxyPollType = type || null;

  proxyPollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/v1/admin/proxy/status', {
        headers: buildAuthHeaders(apiKey)
      });
      if (!res.ok) return;
      const data = await res.json();
      updateProxyStats(data);

      // 更新进度条
      if (data.fetching && data.fetch_progress) {
        updateProxyProgress(data.fetch_progress, 'fetch');
      } else if (data.checking && data.check_progress) {
        updateProxyProgress(data.check_progress, 'check');
      }

      if (!data.fetching && !data.checking) {
        clearInterval(proxyPollTimer);
        proxyPollTimer = null;
        updateProxyProgress(null);
        await loadProxyList();
        resetProxyButtons();
        const msg = proxyPollType === 'fetch'
          ? `抓取完成，总计 ${data.total} 个代理`
          : `测活完成，存活 ${data.alive}/${data.total}`;
        showToast(msg, 'success');
        proxyPollType = null;
      }
    } catch (e) {
      console.error('Poll proxy status error:', e);
    }
  }, 1000);
}

// 重置代理按钮
function resetProxyButtons() {
  const btnFetch = document.getElementById('btn-fetch-proxy');
  const btnCheck = document.getElementById('btn-check-proxy');

  btnFetch.disabled = false;
  btnFetch.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"></path></svg> 抓取代理';

  btnCheck.disabled = false;
  btnCheck.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg> 测活';
}

// 清空代理
async function clearProxies() {
  if (!confirm('确定要清空代理池吗？')) return;

  try {
    const res = await fetch('/api/v1/admin/proxy/clear', {
      method: 'POST',
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) throw new Error('Clear failed');

    proxyList = [];
    updateProxySelect();
    updateProxyStats({ total: 0, alive: 0 });
    updateProxyProgress(null);
    showToast('代理池已清空', 'success');
  } catch (e) {
    console.error('Clear proxies error:', e);
    showToast('清空失败', 'error');
  }
}

// ==================== 自动注册功能 ====================

async function loadAutoRegisterConfig() {
  try {
    const res = await fetch('/api/v1/admin/config', {
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) return;
    const cfg = await res.json();
    const reg = cfg.register || {};

    const el = (id) => document.getElementById(id);
    if (el('auto-reg-enabled')) el('auto-reg-enabled').checked = !!reg.auto_register;
    if (el('auto-reg-mode')) el('auto-reg-mode').value = reg.auto_register_mode || 'normal';
    if (el('auto-reg-count')) el('auto-reg-count').value = reg.auto_register_count || 10;
    if (el('auto-reg-interval')) el('auto-reg-interval').value = reg.auto_register_interval || 0;
    if (el('auto-reg-token-threshold')) el('auto-reg-token-threshold').value = reg.auto_register_token_threshold || 0;
    if (el('auto-reg-chat-threshold')) el('auto-reg-chat-threshold').value = reg.auto_register_chat_threshold || 0;

    await loadAutoRegisterStatus();
  } catch (e) {
    console.error('Load auto register config error:', e);
  }
}

async function loadAutoRegisterStatus() {
  try {
    const res = await fetch('/api/v1/admin/register/auto/status', {
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) return;
    const data = await res.json();
    const statusEl = document.getElementById('auto-reg-status');
    const infoEl = document.getElementById('auto-reg-info');
    if (statusEl) {
      statusEl.textContent = data.running ? '运行中' : '未启用';
    }
    if (infoEl) {
      const parts = [];
      if (data.last_trigger) parts.push('上次: ' + formatTime(data.last_trigger));
      if (data.last_reason) parts.push('原因: ' + data.last_reason);
      if (data.next_trigger) parts.push('下次: ' + formatTime(data.next_trigger));
      infoEl.textContent = parts.join(' · ');
    }
  } catch (e) {
    console.error('Load auto register status error:', e);
  }
}

function toggleAutoRegister() {
  // UI only, actual save via saveAutoRegisterConfig
}

async function saveAutoRegisterConfig() {
  const btn = document.getElementById('btn-save-auto-reg');
  btn.disabled = true;

  try {
    // Load current config
    const res = await fetch('/api/v1/admin/config', {
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) throw new Error('Failed to load config');
    const cfg = await res.json();

    if (!cfg.register) cfg.register = {};
    cfg.register.auto_register = document.getElementById('auto-reg-enabled').checked;
    cfg.register.auto_register_mode = document.getElementById('auto-reg-mode').value;
    cfg.register.auto_register_count = parseInt(document.getElementById('auto-reg-count').value) || 10;
    cfg.register.auto_register_concurrent = 8;
    cfg.register.auto_register_interval = parseInt(document.getElementById('auto-reg-interval').value) || 0;
    cfg.register.auto_register_token_threshold = parseInt(document.getElementById('auto-reg-token-threshold').value) || 0;
    cfg.register.auto_register_chat_threshold = parseInt(document.getElementById('auto-reg-chat-threshold').value) || 0;

    const saveRes = await fetch('/api/v1/admin/config', {
      method: 'POST',
      headers: {
        ...buildAuthHeaders(apiKey),
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(cfg)
    });

    if (saveRes.ok) {
      showToast('自动注册配置已保存', 'success');
      await loadAutoRegisterStatus();
    } else {
      showToast('保存失败', 'error');
    }
  } catch (e) {
    console.error('Save auto register config error:', e);
    showToast('保存失败: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function triggerAutoRegister() {
  const btn = document.getElementById('btn-trigger-auto-reg');
  btn.disabled = true;

  try {
    const res = await fetch('/api/v1/admin/register/auto/trigger', {
      method: 'POST',
      headers: buildAuthHeaders(apiKey)
    });
    const data = await res.json();
    if (res.ok) {
      showToast('自动注册已触发', 'success');
      await loadAutoRegisterStatus();
    } else {
      showToast(data.detail || '触发失败', 'error');
    }
  } catch (e) {
    console.error('Trigger auto register error:', e);
    showToast('触发失败', 'error');
  } finally {
    btn.disabled = false;
  }
}

// ==================== 工具函数 ====================

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function formatTime(isoStr) {
  if (!isoStr) return '-';
  try {
    const d = new Date(isoStr);
    return d.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    });
  } catch {
    return isoStr;
  }
}

function showToast(message, type = 'info') {
  if (typeof window.showToast === 'function') {
    window.showToast(message, type);
  } else {
    console.log(`[${type}] ${message}`);
  }
}
