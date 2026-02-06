/**
 * 代理池管理页面 JavaScript
 */

let apiKey = '';
let currentPage = 1;
let pageSize = 50;
let totalProxies = 0;
let pollTimer = null;
let pollType = null;

// 按钮原始 HTML（用于恢复）
const BTN_FETCH_HTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"></path></svg> 抓取代理`;
const BTN_CHECK_HTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg> 测活`;
const SPINNER_SVG = `<svg class="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"></path></svg>`;

async function init() {
  apiKey = await ensureApiKey();
  if (apiKey === null) return;
  await Promise.all([loadStatus(), loadSchedulerStatus(), loadProxies()]);
}

document.addEventListener('DOMContentLoaded', init);

// 加载代理池状态 → 更新统计卡片
async function loadStatus() {
  try {
    const res = await fetch('/api/v1/admin/proxy/status', {
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) return;
    const data = await res.json();

    const total = data.total || 0;
    const alive = data.alive || 0;
    const unchecked = data.unchecked || 0;
    const checked = total - unchecked;
    const rate = checked > 0 ? ((alive / checked) * 100).toFixed(1) : '0';

    document.getElementById('stat-total').textContent = total;
    document.getElementById('stat-alive').textContent = alive;
    document.getElementById('stat-unchecked').textContent = unchecked;
    document.getElementById('stat-rate').textContent = `${rate}%`;

    // 如果页面加载时已在运行，自动启动轮询
    if (data.fetching) {
      setBtnLoading('btn-fetch', '抓取中...');
      document.getElementById('btn-check').disabled = true;
      if (data.fetch_progress) updateProgress(data.fetch_progress, 'fetch');
      if (!pollTimer) startPoll('fetch');
    } else if (data.checking) {
      setBtnLoading('btn-check', '测活中...');
      document.getElementById('btn-fetch').disabled = true;
      if (data.check_progress) updateProgress(data.check_progress, 'check');
      if (!pollTimer) startPoll('check');
    }
  } catch (e) {
    console.error('Load status error:', e);
  }
}

// 加载调度器状态
async function loadSchedulerStatus() {
  try {
    const res = await fetch('/api/v1/admin/proxy/scheduler/status', {
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) return;
    const data = await res.json();

    const dot = document.getElementById('scheduler-dot');
    const text = document.getElementById('scheduler-running-text');
    if (data.running) {
      dot.className = 'inline-block w-2 h-2 rounded-full bg-green-500 mr-1';
      text.textContent = '运行中';
      text.className = 'text-green-600';
    } else {
      dot.className = 'inline-block w-2 h-2 rounded-full bg-gray-400 mr-1';
      text.textContent = '已停止';
      text.className = 'text-[var(--accents-4)]';
    }

    document.getElementById('scheduler-last-fetch').textContent = formatTime(data.last_fetch);
    document.getElementById('scheduler-last-check').textContent = formatTime(data.last_check);
  } catch (e) {
    console.error('Load scheduler status error:', e);
  }
}

// 加载代理列表
async function loadProxies() {
  try {
    const res = await fetch(`/api/v1/admin/proxy/list?page=${currentPage}&page_size=${pageSize}`, {
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) throw new Error('Load proxies failed');
    const data = await res.json();

    totalProxies = data.total || 0;
    currentPage = data.page || 1;
    renderTable(data.proxies || []);
    updatePagination();
  } catch (e) {
    console.error('Load proxies error:', e);
  }
}

// 渲染表格
function renderTable(proxies) {
  const tbody = document.getElementById('proxy-table-body');
  const emptyState = document.getElementById('empty-state');

  if (proxies.length === 0) {
    tbody.innerHTML = '';
    emptyState.classList.remove('hidden');
    return;
  }

  emptyState.classList.add('hidden');
  const startIndex = (currentPage - 1) * pageSize;

  tbody.innerHTML = proxies.map((p, i) => {
    const statusClass = p.alive ? 'status-success' : 'status-failed';
    const statusText = p.alive ? '存活' : '失效';
    const latency = p.latency > 0 ? `${p.latency}ms` : '-';
    const source = truncateSource(p.source || 'manual');
    const escaped = escapeHtml(p.proxy);

    return `<tr>
      <td>${startIndex + i + 1}</td>
      <td class="text-left font-mono text-xs">${escaped}</td>
      <td><span class="status-badge ${statusClass}">${statusText}</span></td>
      <td class="text-xs">${latency}</td>
      <td>${p.fail_count || 0}</td>
      <td class="text-left text-xs text-[var(--accents-4)]" title="${escapeHtml(p.source || '')}">${source}</td>
      <td class="time-cell">${formatTime(p.last_check)}</td>
      <td>
        <button onclick="removeProxy('${escaped}')" class="text-red-500 hover:text-red-700 text-xs">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
          </svg>
        </button>
      </td>
    </tr>`;
  }).join('');
}

// 分页
function updatePagination() {
  const totalPages = Math.ceil(totalProxies / pageSize) || 1;
  document.getElementById('pagination-info').textContent = `第 ${currentPage} / ${totalPages} 页 · 共 ${totalProxies} 条`;
  document.getElementById('page-prev').disabled = currentPage <= 1;
  document.getElementById('page-next').disabled = currentPage >= totalPages;
}

function goPrevPage() {
  if (currentPage > 1) { currentPage--; loadProxies(); }
}

function goNextPage() {
  const totalPages = Math.ceil(totalProxies / pageSize);
  if (currentPage < totalPages) { currentPage++; loadProxies(); }
}

function changePageSize() {
  pageSize = parseInt(document.getElementById('page-size').value);
  currentPage = 1;
  loadProxies();
}

// 设置按钮 loading 状态
function setBtnLoading(btnId, text) {
  const btn = document.getElementById(btnId);
  btn.disabled = true;
  btn.innerHTML = `${SPINNER_SVG} ${text}`;
}

// 抓取代理
async function fetchProxies() {
  setBtnLoading('btn-fetch', '抓取中...');
  document.getElementById('btn-check').disabled = true;

  try {
    const res = await fetch('/api/v1/admin/proxy/fetch', {
      method: 'POST',
      headers: buildAuthHeaders(apiKey)
    });
    if (!res.ok) throw new Error('Fetch failed');
    showToast('开始抓取代理...', 'success');
    startPoll('fetch');
  } catch (e) {
    showToast('抓取失败', 'error');
    resetButtons();
  }
}

// 测活代理
async function checkProxies() {
  setBtnLoading('btn-check', '测活中...');
  document.getElementById('btn-fetch').disabled = true;

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
    startPoll('check');
  } catch (e) {
    showToast('测活失败', 'error');
    resetButtons();
  }
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
    showToast('代理池已清空', 'success');
    await Promise.all([loadStatus(), loadProxies()]);
  } catch (e) {
    showToast('清空失败', 'error');
  }
}

// 手动添加代理
async function addProxy() {
  const input = document.getElementById('input-proxy');
  const proxy = input.value.trim();
  if (!proxy) { showToast('请输入代理地址', 'error'); return; }

  try {
    const res = await fetch('/api/v1/admin/proxy/add', {
      method: 'POST',
      headers: { ...buildAuthHeaders(apiKey), 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxy })
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || '添加失败');
    }
    const data = await res.json();
    showToast(data.message || '代理已添加', 'success');
    input.value = '';
    await Promise.all([loadStatus(), loadProxies()]);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

// 删除代理
async function removeProxy(proxy) {
  if (!confirm(`确定要删除 ${proxy} 吗？`)) return;

  try {
    const res = await fetch('/api/v1/admin/proxy/remove', {
      method: 'POST',
      headers: { ...buildAuthHeaders(apiKey), 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxy })
    });
    if (!res.ok) throw new Error('Remove failed');
    showToast('代理已删除', 'success');
    await Promise.all([loadStatus(), loadProxies()]);
  } catch (e) {
    showToast('删除失败', 'error');
  }
}

// 轮询进度
function startPoll(type) {
  if (pollTimer) clearInterval(pollTimer);
  pollType = type;

  pollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/v1/admin/proxy/status', {
        headers: buildAuthHeaders(apiKey)
      });
      if (!res.ok) return;
      const data = await res.json();

      // 更新统计
      const total = data.total || 0;
      const alive = data.alive || 0;
      const unchecked = data.unchecked || 0;
      const checked = total - unchecked;
      document.getElementById('stat-total').textContent = total;
      document.getElementById('stat-alive').textContent = alive;
      document.getElementById('stat-unchecked').textContent = unchecked;
      document.getElementById('stat-rate').textContent = checked > 0 ? ((alive / checked) * 100).toFixed(1) + '%' : '0%';

      // 更新进度条
      if (data.fetching && data.fetch_progress) {
        updateProgress(data.fetch_progress, 'fetch');
      } else if (data.checking && data.check_progress) {
        updateProgress(data.check_progress, 'check');
      }

      // 完成
      if (!data.fetching && !data.checking) {
        clearInterval(pollTimer);
        pollTimer = null;
        hideProgress();
        resetButtons();
        await Promise.all([loadProxies(), loadSchedulerStatus()]);
        const msg = pollType === 'fetch'
          ? `抓取完成，总计 ${data.total} 个代理`
          : `测活完成，存活 ${data.alive}/${data.total}`;
        showToast(msg, 'success');
        pollType = null;
      }
    } catch (e) {
      console.error('Poll error:', e);
    }
  }, 1000);
}

function updateProgress(progress, type) {
  const container = document.getElementById('proxy-progress-container');
  const fill = document.getElementById('proxy-progress-fill');
  const text = document.getElementById('proxy-progress-text');

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

function hideProgress() {
  document.getElementById('proxy-progress-container').classList.add('hidden');
  document.getElementById('proxy-progress-fill').style.width = '0%';
}

function resetButtons() {
  const btnFetch = document.getElementById('btn-fetch');
  const btnCheck = document.getElementById('btn-check');
  btnFetch.disabled = false;
  btnFetch.innerHTML = BTN_FETCH_HTML;
  btnCheck.disabled = false;
  btnCheck.innerHTML = BTN_CHECK_HTML;
}

// 工具函数
function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatTime(isoStr) {
  if (!isoStr) return '-';
  try {
    const d = new Date(isoStr);
    return d.toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  } catch { return isoStr; }
}

function truncateSource(source) {
  if (!source) return '-';
  if (source === 'manual') return '手动';
  const match = source.match(/github\.com\/([^/]+)/);
  return match ? match[1] : (source.length > 20 ? source.substring(0, 20) + '...' : source);
}

function showToast(message, type = 'info') {
  if (typeof window.showToast === 'function') {
    window.showToast(message, type);
  } else {
    console.log(`[${type}] ${message}`);
  }
}
