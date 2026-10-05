// ===== 小助手共享组件（背诵/复习共用，对话历史互通并持久化） =====
window.AssistantPanel = (function () {
  var cfg = { aiSettingsUrl: '/settings/#ai-models', getCurrentWord: null };
  var sending = false;

  function el(id) { return document.getElementById(id); }
  function panel() { return el('assistantPanel'); }
  function isOpen() { var p = panel(); return !!p && !p.classList.contains('hidden'); }

  function init(options) {
    cfg = Object.assign(cfg, options || {});
    var input = el('assistantInput');
    if (input) {
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          sendFromInput();
        }
      });
    }
  }

  function toggle() { if (isOpen()) close(); else open(); }

  function open() {
    var p = panel();
    if (!p) return;
    p.classList.remove('hidden');
    loadHistory();
    var input = el('assistantInput');
    if (input) input.focus();
  }

  function close() {
    var p = panel();
    if (p) p.classList.add('hidden');
  }

  function setBody(html) {
    var body = el('assistantBody');
    if (body) body.innerHTML = html;
  }

  // 每次打开都从服务端拉取，保证历史最新（三个模式互通）
  function loadHistory() {
    setBody('<div class="assistant-empty">加载对话记录…</div>');
    VOCAB_API.get('/api/assistant/').then(function (res) {
      renderMessages((res && res.messages) || []);
    }).catch(function () {
      setBody('<div class="assistant-empty">加载对话记录失败</div>');
    });
  }

  function renderMessages(msgs) {
    if (!msgs || !msgs.length) {
      setBody('<div class="assistant-empty">你好，我是你的智能助手！<br>随便问我什么都可以：单词考法、学习规划、日常聊天、编程问题…</div>');
      return;
    }
    var html = '';
    msgs.forEach(function (m) { html += bubbleHtml(m.role, m.content, m.created_at); });
    setBody(html);
    scrollBottom();
  }

  function bubbleHtml(role, content, time) {
    var cls = role === 'user' ? 'user' : 'ai';
    var body = role === 'user' ? escapeHtml(content) : renderMarkdown(content);
    return '<div class="assistant-msg md-compact ' + cls + '">' + body +
      (time ? '<span class="assistant-msg-time">' + time + '</span>' : '') + '</div>';
  }

  // ===== 轻量 Markdown 渲染（仅用于 AI 回答；先转义再排版，天然防 XSS） =====
  function renderMarkdown(src) {
    if (!src) return '';
    // keepGt=true：保留 > 以便识别引用块（> 在文本中无害，不需要转义）
    var lines = escapeHtml(src, true).split('\n');
    var html = '';
    var i = 0;
    var para = [];

    function flushPara() {
      if (para.length) { html += '<p>' + para.map(inline).join('<br>') + '</p>'; para = []; }
    }
    function inline(s) {
      s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, t, u) {
        return /^(https?:\/\/)/.test(u) ? '<a href="' + u + '" target="_blank" rel="noopener">' + t + '</a>' : m;
      });
      s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
      s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
      return s;
    }

    while (i < lines.length) {
      var line = lines[i];
      // 围栏代码块
      if (/^\s*```/.test(line)) {
        flushPara();
        var buf = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
        html += '<pre><code>' + buf.join('\n') + '</code></pre>';
        i++;
        continue;
      }
      // 标题
      var h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flushPara();
        html += '<h' + h[1].length + '>' + inline(h[2]) + '</h' + h[1].length + '>';
        i++;
        continue;
      }
      // 分隔线
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
        flushPara();
        html += '<hr>';
        i++;
        continue;
      }
      // 引用
      if (/^\s*>\s?/.test(line)) {
        flushPara();
        var q = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) { q.push(inline(lines[i].replace(/^\s*>\s?/, ''))); i++; }
        html += '<blockquote>' + q.join('<br>') + '</blockquote>';
        continue;
      }
      // 无序列表（含缩进续行）
      if (/^\s*[-*]\s+/.test(line)) {
        flushPara();
        var ul = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          var li = inline(lines[i].replace(/^\s*[-*]\s+/, ''));
          i++;
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*[-*]\s+/.test(lines[i]) && !/^\s*\d+\.\s+/.test(lines[i])) {
            li += '<br>' + inline(lines[i].trim());
            i++;
          }
          ul.push('<li>' + li + '</li>');
        }
        html += '<ul>' + ul.join('') + '</ul>';
        continue;
      }
      // 有序列表（含缩进续行）
      if (/^\s*\d+\.\s+/.test(line)) {
        flushPara();
        var ol = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          var oli = inline(lines[i].replace(/^\s*\d+\.\s+/, ''));
          i++;
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*[-*]\s+/.test(lines[i]) && !/^\s*\d+\.\s+/.test(lines[i])) {
            oli += '<br>' + inline(lines[i].trim());
            i++;
          }
          ol.push('<li>' + oli + '</li>');
        }
        html += '<ol>' + ol.join('') + '</ol>';
        continue;
      }
      // 简单表格（下一行为分隔行时开始收集）
      if (line.indexOf('|') >= 0 && lines[i + 1] && /^\s*\|?[\s:|-]+\|[\s:|-]*\s*$/.test(lines[i + 1])) {
        flushPara();
        var rows = [];
        while (i < lines.length && lines[i] && lines[i].indexOf('|') >= 0) {
          var row = lines[i].trim().replace(/^\||\|$/g, '');
          if (!/^[\s:|-]+$/.test(row.replace(/\|/g, ''))) {
            rows.push(row.split('|').map(function (c) { return c.trim(); }));
          }
          i++;
        }
        if (rows.length) {
          var tbl = '<table><thead><tr>';
          rows[0].forEach(function (h2) { tbl += '<th>' + inline(h2) + '</th>'; });
          tbl += '</tr></thead><tbody>';
          for (var r = 1; r < rows.length; r++) {
            tbl += '<tr>';
            rows[r].forEach(function (c) { tbl += '<td>' + inline(c) + '</td>'; });
            tbl += '</tr>';
          }
          tbl += '</tbody></table>';
          html += tbl;
        }
        continue;
      }
      // 空行
      if (/^\s*$/.test(line)) { flushPara(); i++; continue; }
      para.push(line);
      i++;
    }
    flushPara();
    return html;
  }

  function escapeHtml(s, keepGt) {
    var t = String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/"/g, '&quot;');
    if (!keepGt) t = t.replace(/>/g, '&gt;');
    return t;
  }

  function scrollBottom() {
    var body = el('assistantBody');
    if (body) body.scrollTop = body.scrollHeight;
  }

  function appendMsg(role, content, time) {
    var body = el('assistantBody');
    if (!body) return;
    var empty = body.querySelector('.assistant-empty');
    if (empty) empty.remove();
    body.insertAdjacentHTML('beforeend', bubbleHtml(role, content, time));
    scrollBottom();
  }

  function getWordId() {
    if (typeof cfg.getCurrentWord === 'function') {
      var w = cfg.getCurrentWord();
      return w && w.id ? w.id : null;
    }
    return null;
  }

  function sendFromInput() {
    var input = el('assistantInput');
    if (!input) return;
    var text = input.value.trim();
    if (!text) return;
    input.value = '';
    send(text);
  }

  function send(text) {
    if (sending) return;
    sending = true;
    setSending(true);
    appendMsg('user', text, null);
    VOCAB_API.post('/api/assistant/', { message: text, word_id: getWordId() }).then(function (res) {
      if (res.success) {
        appendMsg('assistant', res.assistant_message.content, res.assistant_message.created_at);
      } else {
        appendMsg('assistant', '⚠️ ' + (res.error || '出错了'));
        if (res.error && /模型/.test(res.error)) showToast('请到设置页配置并启用 AI 模型', 'error');
      }
    }).catch(function () {
      appendMsg('assistant', '⚠️ 网络错误，请稍后重试');
    }).finally(function () {
      sending = false;
      setSending(false);
      var input = el('assistantInput');
      if (input) input.focus();
    });
  }

  function setSending(on) {
    var btn = el('assistantSendBtn');
    if (!btn) return;
    btn.disabled = on;
    btn.innerHTML = on
      ? '<i class="ph ph-circle-notch" style="animation: spin 1s linear infinite;"></i>'
      : '<i class="ph ph-paper-plane-tilt"></i>';
  }

  // 一键询问当前单词的考法
  function askCurrentWord() {
    var w = (typeof cfg.getCurrentWord === 'function') ? cfg.getCurrentWord() : null;
    if (!w || !w.word) { showToast('请先选择一个单词', 'error'); return; }
    send('请讲解单词 "' + w.word + '" 的考法、常见搭配和记忆技巧');
  }

  async function clearHistory() {
    if (!await appConfirm('确定要清空小助手的全部聊天记录吗？')) return;
    VOCAB_API.del('/api/assistant/').then(function (res) {
      if (res.success) {
        renderMessages([]);
        showToast('对话记录已清空', 'success');
      } else {
        showToast(res.error || '清空失败', 'error');
      }
    }).catch(function () { showToast('请求失败', 'error'); });
  }

  return {
    init: init, toggle: toggle, open: open, close: close, isOpen: isOpen,
    sendFromInput: sendFromInput, askCurrentWord: askCurrentWord,
    clearHistory: clearHistory,
  };
})();
