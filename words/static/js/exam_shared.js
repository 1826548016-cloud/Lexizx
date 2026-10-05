/* ============================================================
   考研写作工坊：页面间共用的前端工具函数
   原先 escapeHtml 在 8 个模板里各写一份、renderMarkdown 在 2 个模板里各写一份，
   此处统一为一份，供工坊各页直接调用（base.html 里全局引入）。
   依赖：无（纯函数）
   ============================================================ */
(function (window) {
  'use strict';

  // ===== HTML 转义：所有拼接进 innerHTML 的动态文本都必须先过这一层 =====
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // ===== 轻量 Markdown 渲染（先转义再排版，避免 XSS）=====
  // 支持：标题 / 段落 / 粗体 / 斜体 / 行内代码 / 代码块 / 引用 / 有序无序列表 /
  //       分割线 / 链接 / 表格
  function renderMarkdown(src) {
    if (!src) return '';
    var lines = escapeHtml(src).split('\n');
    var html = '';
    var i = 0;
    var para = [];

    function flushPara() {
      if (para.length) {
        html += '<p>' + para.map(inline).join('\n') + '</p>';
        para = [];
      }
    }

    function inline(s) {
      s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, t, u) {
        return /^(https?:\/\/)/.test(u)
          ? '<a href="' + u + '" target="_blank" rel="noopener">' + t + '</a>'
          : m;
      });
      s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
      s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
      return s;
    }

    // 表格辅助
    function isPipeRow(s) {
      return /^\s*\|?.*\|.*\|\s*$/.test(s) && s.indexOf('|') !== -1;
    }
    function isSepRow(s) {
      var stripped = s.trim().replace(/^\|/, '').replace(/\|$/, '');
      if (!stripped) return false;
      var parts = stripped.split('|');
      if (parts.length < 2) return false;
      return parts.every(function (p) { return /^\s*:?-{3,}:?\s*$/.test(p); });
    }
    function splitRow(s) {
      var t = s.trim();
      if (t.charAt(0) === '|') t = t.slice(1);
      if (t.charAt(t.length - 1) === '|') t = t.slice(0, -1);
      return t.split('|').map(function (c) { return c.trim(); });
    }

    while (i < lines.length) {
      var line = lines[i];

      if (/^\s*```/.test(line)) {
        flushPara();
        var buf = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
        html += '<pre><code>' + buf.join('\n') + '</code></pre>';
        i++;
        continue;
      }

      var h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flushPara();
        html += '<h' + h[1].length + '>' + inline(h[2]) + '</h' + h[1].length + '>';
        i++;
        continue;
      }

      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flushPara(); html += '<hr>'; i++; continue; }

      if (/^\s*>\s?/.test(line)) {
        flushPara();
        var q = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          q.push(inline(lines[i].replace(/^\s*>\s?/, '')));
          i++;
        }
        html += '<blockquote>' + q.join('<br>') + '</blockquote>';
        continue;
      }

      if (/^\s*[-*+]\s+/.test(line)) {
        flushPara();
        var ul = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          ul.push('<li>' + inline(lines[i].replace(/^\s*[-*+]\s+/, '')) + '</li>');
          i++;
        }
        html += '<ul>' + ul.join('') + '</ul>';
        continue;
      }

      if (/^\s*\d+[.)]\s+/.test(line)) {
        flushPara();
        var ol = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
          ol.push('<li>' + inline(lines[i].replace(/^\s*\d+[.)]\s+/, '')) + '</li>');
          i++;
        }
        html += '<ol>' + ol.join('') + '</ol>';
        continue;
      }

      if (isPipeRow(line) && i + 1 < lines.length && isSepRow(lines[i + 1])) {
        flushPara();
        var header = splitRow(line);
        i += 2; // 跳表头行与分隔行
        var bodyRows = [];
        while (i < lines.length && isPipeRow(lines[i])) { bodyRows.push(splitRow(lines[i])); i++; }
        html += '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
        for (var hi = 0; hi < header.length; hi++) html += '<th>' + inline(header[hi]) + '</th>';
        html += '</tr></thead><tbody>';
        for (var bi = 0; bi < bodyRows.length; bi++) {
          html += '<tr>';
          var ci = 0;
          for (; ci < bodyRows[bi].length; ci++) html += '<td>' + inline(bodyRows[bi][ci]) + '</td>';
          while (ci < header.length) { html += '<td></td>'; ci++; }
          html += '</tr>';
        }
        html += '</tbody></table></div>';
        continue;
      }

      para.push(line);
      i++;
    }

    flushPara();
    return html;
  }

  // ===== 复制到剪贴板（带 execCommand 回退）=====
  function copyText(text) {
    if (!text) {
      if (window.showToast) window.showToast('暂无内容可复制', 'error');
      return;
    }
    function done() { if (window.showToast) window.showToast('已复制', 'success'); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fallback);
    } else {
      fallback();
    }
    function fallback() {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
        done();
      } catch (e) {
        if (window.showToast) window.showToast('复制失败，请手动选择复制', 'error');
      }
    }
  }

  // ===== 批改结果渲染：三个模式（作文 / 考研翻译 / 六级翻译）共用 =====
  // dims: [[key, label], ...]；max: 满分
  function renderScoreGrid(scores, dims, max) {
    scores = scores || {};
    var html = '<div class="ws-scores">';
    dims.forEach(function (d) {
      var v = Number(scores[d[0]]) || 0;
      var pct = max ? Math.max(0, Math.min(100, Math.round(v / max * 100))) : 0;
      html += '<div class="ws-score">'
        + '<div class="ws-score-value">' + v + '</div>'
        + '<div class="ws-score-label">' + escapeHtml(d[1]) + '</div>'
        + '<div class="ws-score-bar"><i style="width:' + pct + '%"></i></div>'
        + '</div>';
    });
    return html + '</div>';
  }

  function renderTotal(total, max) {
    if (!total && total !== 0) return '';
    return '<div class="ws-total">'
      + '<span class="ws-total-label">总分</span>'
      + '<span class="ws-total-value">' + escapeHtml(total) + '</span>'
      + '<span class="ws-total-max">/ ' + escapeHtml(max) + '</span>'
      + '</div>';
  }

  // 错误修正：原文 → 修正（+ 原因）
  function renderErrors(errors) {
    if (!errors || !errors.length) return '';
    var html = '<div class="ws-section">'
      + '<div class="ws-section-title"><i class="ph ph-warning-circle" style="color:var(--c-red);"></i> 错误修正</div>'
      + '<ul class="ws-error-list">';
    errors.forEach(function (e) {
      html += '<li>'
        + '<span class="ws-wrong">' + escapeHtml(e.original || '') + '</span>'
        + '<span class="ws-arrow">→</span>'
        + '<span class="ws-right">' + escapeHtml(e.corrected || '') + '</span>'
        + (e.reason ? '<span class="ws-why">（' + escapeHtml(e.reason) + '）</span>' : '')
        + '</li>';
    });
    return html + '</ul></div>';
  }

  // 高级替换表达：新表达（替换 旧表达）+ 说明
  function renderExpressions(list) {
    if (!list || !list.length) return '';
    var html = '<div class="ws-section">'
      + '<div class="ws-section-title"><i class="ph ph-arrow-up-right" style="color:var(--c-green);"></i> 高级替换表达</div>'
      + '<ul class="ws-expr-list">';
    list.forEach(function (e) {
      html += '<li>'
        + '<span class="ws-expr-new">' + escapeHtml(e.advanced || '') + '</span>'
        + (e.original ? ' <span class="ws-expr-old">（替换 ' + escapeHtml(e.original) + '）</span>' : '')
        + (e.explain ? '<div class="ws-expr-note">' + escapeHtml(e.explain) + '</div>' : '')
        + '</li>';
    });
    return html + '</ul></div>';
  }

  // 改进建议
  function renderSuggestions(list) {
    if (!list || !list.length) return '';
    var html = '<div class="ws-section">'
      + '<div class="ws-section-title"><i class="ph ph-lightbulb" style="color:var(--c-amber);"></i> 改进建议</div><ul>';
    list.forEach(function (s) { html += '<li>' + escapeHtml(s) + '</li>'; });
    return html + '</ul></div>';
  }

  // 参考译文 / 范文
  function renderModelText(label, text, opts) {
    if (!text) return '';
    opts = opts || {};
    return '<div class="ws-section">'
      + '<div class="ws-section-title"><i class="ph ph-book-open"></i> ' + escapeHtml(label) + '</div>'
      + '<div class="model-box"' + (opts.preWrap === false ? '' : ' style="white-space: pre-wrap;"') + '>'
      + (opts.markdown ? renderMarkdown(text) : escapeHtml(text))
      + '</div></div>';
  }

  // 总评
  function renderComments(comments) {
    if (!comments) return '';
    return '<div class="ws-section">'
      + '<div class="ws-section-title"><i class="ph ph-chat-centered-text"></i> 总评</div>'
      + '<div>' + renderMarkdown(comments) + '</div></div>';
  }

  // ===== 主题色读取：供 ECharts 等 JS 绘图使用 =====
  // CSS 变量在 JS 里读不到值，必须经 getComputedStyle 取实际计算值。
  // 这样图表配色自动跟随全部 6 套主题，无需在 JS 里按明暗写死两份色值。
  function themeColor(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement)
        .getPropertyValue(name).trim();
      return v || (fallback || '');
    } catch (e) {
      return fallback || '';
    }
  }

  // 取某个主题色的 "r,g,b" 三元组，供 rgba() 拼半透明渐变使用
  // （CSS 变量是 hex，JS 里要拼 rgba 需要拆成通道）
  function themeColorRgb(name, fallback) {
    var v = themeColor(name, fallback);
    if (!v) return '';
    v = v.trim();
    var m;
    // #rgb / #rrggbb
    if (v.charAt(0) === '#') {
      var h = v.slice(1);
      if (h.length === 3) {
        h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
      }
      if (h.length >= 6) {
        m = [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
        if (m.every(function (x) { return !isNaN(x); })) return m.join(',');
      }
      return '';
    }
    // rgb() / rgba()
    m = v.match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/);
    if (m) return [m[1], m[2], m[3]].join(',');
    return '';
  }

  // 一次取一组，避免重复触发样式计算
  function themeColors() {
    return {
      green: themeColor('--c-green'),
      red: themeColor('--c-red'),
      blue: themeColor('--c-blue'),
      purple: themeColor('--c-purple'),
      amber: themeColor('--c-amber'),
      text: themeColor('--c-text'),
      text2: themeColor('--c-text-2'),
      text3: themeColor('--c-text-3'),
      surface: themeColor('--c-surface'),
      border: themeColor('--c-border'),
      borderLight: themeColor('--c-border-light'),
      redRgb: themeColorRgb('--c-red'),
      greenRgb: themeColorRgb('--c-green'),
      purpleRgb: themeColorRgb('--c-purple'),
      blueRgb: themeColorRgb('--c-blue'),
      amberRgb: themeColorRgb('--c-amber')
    };
  }

  window.escapeHtml = escapeHtml;
  window.renderMarkdown = renderMarkdown;
  window.copyText = copyText;
  window.themeColor = themeColor;
  window.themeColorRgb = themeColorRgb;
  window.themeColors = themeColors;
  window.WS = {
    escapeHtml: escapeHtml,
    renderMarkdown: renderMarkdown,
    copyText: copyText,
    themeColor: themeColor,
    themeColorRgb: themeColorRgb,
    themeColors: themeColors,
    renderScoreGrid: renderScoreGrid,
    renderTotal: renderTotal,
    renderErrors: renderErrors,
    renderExpressions: renderExpressions,
    renderSuggestions: renderSuggestions,
    renderModelText: renderModelText,
    renderComments: renderComments
  };
})(window);
