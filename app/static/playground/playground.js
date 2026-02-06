/**
 * Playground 页面 JavaScript
 */

let apiKey = '';
let messages = [];
let isStreaming = false;
let abortController = null;

async function init() {
  apiKey = await ensureApiKey();
  if (apiKey === null) return;
  await loadModels();
  setupInput();
}

document.addEventListener('DOMContentLoaded', init);

// 加载模型列表
async function loadModels() {
  try {
    const res = await fetch('/v1/models', { headers: buildAuthHeaders(apiKey) });
    if (!res.ok) return;
    const data = await res.json();
    const select = document.getElementById('model-select');
    select.innerHTML = '';
    (data.data || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.id;
      select.appendChild(opt);
    });
  } catch (e) {
    console.error('Load models error:', e);
  }
}

// 输入框自动高度 + 快捷键
function setupInput() {
  const textarea = document.getElementById('chat-input');
  textarea.addEventListener('input', () => {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px';
  });
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
}

// 发送消息
async function sendMessage() {
  if (isStreaming) {
    stopStream();
    return;
  }

  const textarea = document.getElementById('chat-input');
  const content = textarea.value.trim();
  if (!content) return;

  // 添加用户消息
  messages.push({ role: 'user', content });
  appendMessage('user', content);
  textarea.value = '';
  textarea.style.height = 'auto';
  updateStats();

  // 切换按钮为停止
  setStreamingState(true);

  const model = document.getElementById('model-select').value;
  const stream = document.getElementById('stream-toggle').checked;
  const startTime = Date.now();

  try {
    abortController = new AbortController();

    const res = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: {
        ...buildAuthHeaders(apiKey),
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ model, messages, stream }),
      signal: abortController.signal
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(err.error?.message || err.detail || `HTTP ${res.status}`);
    }

    if (stream) {
      await handleStream(res, startTime);
    } else {
      const data = await res.json();
      const reply = data.choices?.[0]?.message?.content || '';
      messages.push({ role: 'assistant', content: reply });
      appendMessage('assistant', reply);
      const duration = ((Date.now() - startTime) / 1000).toFixed(1);
      const usage = data.usage;
      updateStats(usage, duration);
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      showToast('已停止生成', 'info');
    } else {
      showToast(e.message, 'error');
      appendMessage('assistant', `Error: ${e.message}`);
    }
  } finally {
    setStreamingState(false);
    abortController = null;
  }
}

// 处理 SSE 流
async function handleStream(res, startTime) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let fullContent = '';
  let usage = null;
  const msgEl = appendMessage('assistant', '', true);
  const contentEl = msgEl.querySelector('.chat-msg-content');

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || !trimmed.startsWith('data: ')) continue;
        const payload = trimmed.slice(6);
        if (payload === '[DONE]') continue;

        try {
          const chunk = JSON.parse(payload);
          const delta = chunk.choices?.[0]?.delta?.content;
          if (delta) {
            fullContent += delta;
            contentEl.innerHTML = renderContent(fullContent);
            contentEl.classList.add('typing-cursor');
            scrollToBottom();
          }
          if (chunk.usage) usage = chunk.usage;
        } catch { /* skip malformed chunks */ }
      }
    }
  } finally {
    contentEl.classList.remove('typing-cursor');
    contentEl.innerHTML = renderContent(fullContent);
    if (fullContent) {
      messages.push({ role: 'assistant', content: fullContent });
    }
    const duration = ((Date.now() - startTime) / 1000).toFixed(1);
    updateStats(usage, duration);
    scrollToBottom();
  }
}

// 停止流
function stopStream() {
  if (abortController) abortController.abort();
}

// 切换流式状态
function setStreamingState(streaming) {
  isStreaming = streaming;
  const btn = document.getElementById('btn-send');
  const textarea = document.getElementById('chat-input');

  if (streaming) {
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"></rect></svg>`;
    btn.classList.add('stop');
    btn.title = '停止';
    textarea.disabled = true;
  } else {
    btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>`;
    btn.classList.remove('stop');
    btn.title = '发送';
    textarea.disabled = false;
    textarea.focus();
  }
}

// 添加消息到 UI
function appendMessage(role, content, isPlaceholder = false) {
  const empty = document.getElementById('chat-empty');
  if (empty) empty.style.display = 'none';

  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'chat-msg';

  const avatarLabel = role === 'user' ? 'U' : 'AI';
  const rendered = isPlaceholder ? '' : renderContent(content);

  div.innerHTML = `
    <div class="chat-msg-avatar ${role}">${avatarLabel}</div>
    <div class="chat-msg-body">
      <div class="chat-msg-role">${role === 'user' ? 'You' : 'Assistant'}</div>
      <div class="chat-msg-content">${rendered}</div>
    </div>
  `;

  container.appendChild(div);
  scrollToBottom();
  return div;
}

// 简易 Markdown 渲染
function renderContent(text) {
  if (!text) return '';
  let html = escapeHtml(text);
  // 代码块
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre><code>${code.trim()}</code></pre>`;
  });
  // 行内代码
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  // 粗体
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  return html;
}

// 清空对话
function clearChat() {
  messages = [];
  const container = document.getElementById('chat-messages');
  container.innerHTML = `
    <div class="chat-empty" id="chat-empty">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--accents-3)" stroke-width="1.5">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
      </svg>
      <p>输入消息开始对话</p>
    </div>
  `;
  document.getElementById('stat-messages').textContent = '0';
  document.getElementById('stat-tokens').textContent = '-';
  document.getElementById('stat-duration').textContent = '-';
}

// 更新统计
function updateStats(usage, duration) {
  document.getElementById('stat-messages').textContent = messages.length;
  if (usage) {
    const total = usage.total_tokens || ((usage.prompt_tokens || 0) + (usage.completion_tokens || 0));
    document.getElementById('stat-tokens').textContent = `${total} (${usage.prompt_tokens || 0}+${usage.completion_tokens || 0})`;
  }
  if (duration) {
    document.getElementById('stat-duration').textContent = `${duration}s`;
  }
}

function scrollToBottom() {
  const container = document.getElementById('chat-messages');
  container.scrollTop = container.scrollHeight;
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showToast(message, type = 'info') {
  if (typeof window.showToast === 'function') {
    window.showToast(message, type);
  } else {
    console.log(`[${type}] ${message}`);
  }
}
