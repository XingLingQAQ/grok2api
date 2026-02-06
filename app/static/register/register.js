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

// 初始化
document.addEventListener('DOMContentLoaded', async () => {
  apiKey = localStorage.getItem('api_key') || '';
  if (!apiKey) {
    window.location.href = '/admin';
    return;
  }
  await loadStatus();
  await loadResults();
});

// 加载状态
async function loadStatus() {
  try {
    const res = await fetch('/api/v1/admin/register/status', {
      headers: { 'Authorization': `Bearer ${apiKey}` }
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

  btnStart.disabled = !enabled || running;
  btnStop.disabled = !running;
  inputCount.disabled = running;
  inputConcurrent.disabled = running;

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

  if (count < 1 || count > 100) {
    showToast('注册数量必须在 1-100 之间', 'error');
    return;
  }

  try {
    const res = await fetch('/api/v1/admin/register/start', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ count, concurrent })
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
      headers: { 'Authorization': `Bearer ${apiKey}` }
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
      headers: { 'Authorization': `Bearer ${apiKey}` }
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
      headers: { 'Authorization': `Bearer ${apiKey}` }
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

// 导出结果
function exportResults(format) {
  const url = `/api/v1/admin/register/export?format=${format}&api_key=${apiKey}`;
  window.open(url, '_blank');
}

// SSE 连接
function connectSSE() {
  if (eventSource) return;

  const url = `/api/v1/admin/register/stream?api_key=${apiKey}`;
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

// 工具函数
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
