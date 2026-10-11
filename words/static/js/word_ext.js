// word_ext.js — 笔记 / AI 速记 共享组件（背诵、复习模式通用）
// 依赖：VOCAB_API、showToast（app.js 提供）
(function () {
  'use strict';

  var aiSettingsUrl = '/settings/#ai-models';
  var getCurrentWord = null; // () => 当前单词对象
  var noteWordId = null;
  var qmWordId = null;
  var initialized = false;

  function $(id) { return document.getElementById(id); }

  function toggleHidden(id, hidden) {
    var el = $(id);
    if (el) el.classList.toggle('hidden', !!hidden);
  }

  // textarea 随内容自动撑高（CSS max-height 兜底，超高时内部滚动），
  // 省去手动拖拽右下角；空内容回落到 CSS min-height
  function autoGrow(el) {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = el.scrollHeight + 'px';
  }

  function autoGrowAll() {
    autoGrow($('qmContent'));
    autoGrow($('noteContent'));
  }

  // 面板展开/内容变化后，若底部超出视口则最小幅度滚入视野
  function ensurePanelVisible() {
    var panel = $('wordExtPanel');
    if (!panel || panel.classList.contains('hidden')) return;
    var r = panel.getBoundingClientRect();
    if (r.bottom > window.innerHeight - 12) {
      panel.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }

  function cardEl() {
    return $('wordCard') || $('focusCard');
  }

  function openExtPanel(type) {
    var word = getCurrentWord ? getCurrentWord() : null;
    if (!word) return;
    var card = cardEl();
    if (card) card.classList.add('word-card-expanded');
    toggleHidden('wordExtPanel', false);
    var noteBtn = $('noteBtn'), qmBtn = $('quickMemoryBtn');
    if (type === 'note') {
      toggleHidden('notePanel', false);
      toggleHidden('qmPanel', true);
      loadNoteForPanel(word);
      if (noteBtn) { noteBtn.classList.add('active-note'); noteBtn.classList.remove('active-quick'); noteBtn.setAttribute('aria-pressed', 'true'); }
      if (qmBtn) { qmBtn.classList.remove('active-quick'); qmBtn.setAttribute('aria-pressed', 'false'); }
    } else {
      toggleHidden('qmPanel', false);
      toggleHidden('notePanel', true);
      loadQuickMemoryForPanel(word);
      if (qmBtn) { qmBtn.classList.add('active-quick'); qmBtn.classList.remove('active-note'); qmBtn.setAttribute('aria-pressed', 'true'); }
      if (noteBtn) { noteBtn.classList.remove('active-note'); noteBtn.setAttribute('aria-pressed', 'false'); }
    }
    var panel = $('wordExtPanel');
    if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    // 面板显示后按内容撑高（hidden 状态量不到高度）
    requestAnimationFrame(autoGrowAll);
  }

  function closeExtPanel() {
    var card = cardEl();
    if (card) card.classList.remove('word-card-expanded');
    toggleHidden('wordExtPanel', true);
    toggleHidden('notePanel', true);
    toggleHidden('qmPanel', true);
    var noteBtn = $('noteBtn'), qmBtn = $('quickMemoryBtn');
    if (noteBtn) { noteBtn.classList.remove('active-note'); noteBtn.setAttribute('aria-pressed', 'false'); }
    if (qmBtn) { qmBtn.classList.remove('active-quick'); qmBtn.setAttribute('aria-pressed', 'false'); }
  }

  function isOpen() {
    var el = $('wordExtPanel');
    return !!el && !el.classList.contains('hidden');
  }

  // ===== 笔记：记笔记 / 查看之前的笔记（保存到数据库） =====
  function loadNoteForPanel(word) {
    noteWordId = word.id;
    $('noteWordTitle').textContent = '· ' + (word.word || '');
    $('noteMeta').textContent = '加载中…';
    $('noteContent').value = '';
    autoGrow($('noteContent'));
    $('notePage').value = '';
    toggleHidden('noteDeleteBtn', true);

    VOCAB_API.get('/api/note/' + noteWordId + '/')
      .then(function (res) {
        if (res && res.success) {
          $('noteContent').value = res.content || '';
          autoGrow($('noteContent'));
          $('notePage').value = res.page_number || '';
          var meta = res.content ? '查看之前的笔记' : '暂无笔记，记录你的记忆方法吧';
          if (res.updated_at) meta += ' · 更新于 ' + res.updated_at;
          $('noteMeta').textContent = meta;
          if (res.content) toggleHidden('noteDeleteBtn', false);
        }
      })
      .catch(function () {
        $('noteMeta').textContent = '加载笔记失败';
      });
  }

  function saveNote() {
    if (!noteWordId) return;
    var content = $('noteContent').value;
    var pageNumber = $('notePage').value;
    VOCAB_API.post('/api/note/' + noteWordId + '/', {
      content: content,
      page_number: pageNumber === '' ? null : (parseInt(pageNumber, 10) || null),
    })
      .then(function (res) {
        if (res && res.success) {
          showToast('笔记已保存', 'success');
          toggleHidden('noteDeleteBtn', false);
          closeExtPanel();
        } else {
          showToast((res && res.error) || '保存失败', 'error');
        }
      })
      .catch(function () {
        showToast('保存失败', 'error');
      });
  }

  async function deleteNote() {
    if (!noteWordId) return;
    if (!await appConfirm('确定删除这条笔记吗？')) return;
    VOCAB_API.del('/api/note/' + noteWordId + '/')
      .then(function (res) {
        if (res && res.success) {
          showToast('笔记已删除', 'success');
          toggleHidden('noteDeleteBtn', true);
          $('noteContent').value = '';
          autoGrow($('noteContent'));
          $('notePage').value = '';
          $('noteMeta').textContent = '暂无笔记，记录你的记忆方法吧';
        }
      })
      .catch(function () {
        showToast('删除失败', 'error');
      });
  }

  // ===== 速记：AI 生成并缓存到数据库，下次直接读取不再调用 AI =====
  function checkAIModelConfigured() {
    return VOCAB_API.get('/api/ai-models/').then(function (res) {
      var models = (res && res.models) || [];
      return models.some(function (m) { return m.enabled; });
    }).catch(function () { return false; });
  }

  function loadQuickMemoryForPanel(word) {
    qmWordId = word.id;
    $('qmWordTitle').textContent = '· ' + (word.word || '');
    $('qmMeta').textContent = '加载中…';
    $('qmContent').value = '';
    autoGrow($('qmContent'));
    toggleHidden('qmGenerateBtn', true);
    toggleHidden('qmRegenBtn', true);
    toggleHidden('qmDeleteBtn', true);

    // 先从数据库读取：有则直接显示，无则提示生成
    VOCAB_API.get('/api/word/' + qmWordId + '/quick-memory/')
      .then(function (res) {
        if (res && res.success && res.content) {
          $('qmContent').value = res.content;
          autoGrow($('qmContent'));
          ensurePanelVisible();
          var meta = '已保存的速记（来自数据库，无需重新调用 AI）';
          if (res.updated_at) meta += ' · 更新于 ' + res.updated_at;
          $('qmMeta').textContent = meta;
          toggleHidden('qmRegenBtn', false);
          toggleHidden('qmDeleteBtn', false);
        } else {
          // 无速记：已配置 AI 模型则显示「生成速记」，否则引导去设置页
          checkAIModelConfigured().then(function (ok) {
            if (ok) {
              $('qmContent').value = '';
              autoGrow($('qmContent'));
              $('qmMeta').textContent = '还没有速记，点击「生成速记」让 AI 为你创作（结果保存到数据库，下次直接查看）';
              toggleHidden('qmGenerateBtn', false);
            } else {
              $('qmMeta').innerHTML = '尚未配置 AI 模型，<a onclick="openAddModel()" style="color: var(--c-green); cursor: pointer;">立即添加</a>，或到 <a href="' + aiSettingsUrl + '" style="color: var(--c-green);">设置 → AI 模型</a> 中管理';
            }
          });
        }
      })
      .catch(function () {
        $('qmMeta').textContent = '加载速记失败';
        toggleHidden('qmGenerateBtn', false);
      });
  }

  function saveQuickMemory() {
    if (!qmWordId) return;
    var content = $('qmContent').value;
    VOCAB_API.post('/api/word/' + qmWordId + '/quick-memory/', { content: content })
      .then(function (res) {
        if (res && res.success) {
          showToast('速记已保存', 'success');
          $('qmMeta').textContent = '已保存的速记（来自数据库，无需重新调用 AI）';
          toggleHidden('qmRegenBtn', false);
          toggleHidden('qmDeleteBtn', false);
          toggleHidden('qmGenerateBtn', true);
          closeExtPanel();
        } else {
          showToast((res && res.error) || '保存失败', 'error');
        }
      })
      .catch(function () {
        showToast('保存失败', 'error');
      });
  }

  async function deleteQuickMemory() {
    if (!qmWordId) return;
    if (!await appConfirm('确定删除这条速记吗？')) return;
    VOCAB_API.del('/api/word/' + qmWordId + '/quick-memory/')
      .then(function (res) {
        if (res && res.success) {
          showToast('速记已删除', 'success');
          $('qmContent').value = '';
          autoGrow($('qmContent'));
          $('qmMeta').textContent = '还没有速记，点击「生成速记」让 AI 为你创作';
          toggleHidden('qmGenerateBtn', false);
          toggleHidden('qmRegenBtn', true);
          toggleHidden('qmDeleteBtn', true);
        }
      })
      .catch(function () {
        showToast('删除失败', 'error');
      });
  }

  function generateQuickMemory(force) {
    if (!qmWordId) return;
    var btn = $('qmGenerateBtn');
    btn.disabled = true;
    var oldHtml = btn.innerHTML;
    btn.innerHTML = '<i class="ph ph-circle-notch" style="animation: spin 1s linear infinite;"></i> AI 生成中…';
    $('qmMeta').textContent = 'AI 正在创作速记，请稍候…';
    var deck = $('qmDeck');
    if (deck) deck.classList.add('ai-generating');

    // 模型配置由设置页统一管理（数据库），后端自动读取启用模型；未配置时后端返回提示
    VOCAB_API.post('/api/word/' + qmWordId + '/quick-memory/generate/', {
      force: !!force,
    })
      .then(function (res) {
        if (res && res.success) {
          $('qmContent').value = res.content || '';
          autoGrow($('qmContent'));
          ensurePanelVisible();
          $('qmMeta').textContent = res.cached
            ? '已使用数据库中的速记（无需重新调用 AI）'
            : 'AI 生成完成，已保存到数据库（下次直接查看，不再调用 AI）';
          toggleHidden('qmRegenBtn', false);
          toggleHidden('qmDeleteBtn', false);
          toggleHidden('qmGenerateBtn', true);
        } else {
          var err = (res && res.error) || '生成失败';
          if (err.indexOf('尚未配置 AI 模型') !== -1) {
            $('qmMeta').innerHTML = '尚未配置 AI 模型，<a onclick="openAddModel()" style="color: var(--c-green); cursor: pointer;">立即添加</a>，或到 <a href="' + aiSettingsUrl + '" style="color: var(--c-green);">设置 → AI 模型</a> 中管理';
          } else {
            $('qmMeta').textContent = err;
          }
          showToast(err, 'error');
        }
      })
      .catch(function (e) {
        var err = (e && e.error) || '生成失败，请检查模型配置';
        $('qmMeta').textContent = err;
        showToast(err, 'error');
      })
      .then(function () {
        btn.disabled = false;
        btn.innerHTML = oldHtml;
        if (deck) deck.classList.remove('ai-generating');
      });
  }

  // 初始化：绑定「笔记 / 速记」按钮，配置取当前词的函数
  function init(options) {
    if (initialized) return;
    initialized = true;
    options = options || {};
    getCurrentWord = options.getCurrentWord || function () { return null; };
    if (options.aiSettingsUrl) aiSettingsUrl = options.aiSettingsUrl;
    var noteBtn = $('noteBtn');
    var qmBtn = $('quickMemoryBtn');
    if (noteBtn) noteBtn.addEventListener('click', function () { openExtPanel('note'); });
    if (qmBtn) qmBtn.addEventListener('click', function () { openExtPanel('qm'); });

    // 手动编辑时随输入实时撑高；窗口尺寸变化（旋转/缩放）时重算
    ['qmContent', 'noteContent'].forEach(function (id) {
      var el = $(id);
      if (el) el.addEventListener('input', function () { autoGrow(el); });
    });
    var resizeTimer = null;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(autoGrowAll, 120);
    });
  }

  // 暴露给页面（内联 onclick 与键盘逻辑使用）
  window.WordExtPanel = {
    init: init,
    openExtPanel: openExtPanel,
    closeExtPanel: closeExtPanel,
    isOpen: isOpen,
    saveNote: saveNote,
    deleteNote: deleteNote,
    saveQuickMemory: saveQuickMemory,
    deleteQuickMemory: deleteQuickMemory,
    generateQuickMemory: generateQuickMemory,
  };
})();
