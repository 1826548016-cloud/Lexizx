// word_fav.js — 单词收藏共享组件（背诵、复习模式通用）
// 依赖：VOCAB_API、showToast（app.js 提供）
// 后端：POST /api/favorite/<word_id>/ 切换收藏，返回 { success, is_favorite }
(function () {
  'use strict';

  var getCurrentWord = null;
  var initialized = false;

  function $(id) { return document.getElementById(id); }

  // 按当前词的收藏状态刷新星标按钮
  function update(word) {
    var btn = $('favBtn');
    if (!btn) return;
    var on = !!(word && word.is_favorite);
    btn.classList.toggle('active-fav', on);
    var icon = btn.querySelector('i');
    if (icon) icon.className = on ? 'ph ph-star-fill' : 'ph ph-star';
    var label = btn.querySelector('span');
    if (label) label.textContent = on ? '已收藏' : '收藏';
    btn.title = on ? '取消收藏' : '收藏单词（加入收藏词汇）';
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  function toggle() {
    var word = getCurrentWord ? getCurrentWord() : null;
    if (!word) return;
    var btn = $('favBtn');
    if (btn) btn.disabled = true;
    // 乐观更新：立即反馈，失败回滚
    var prev = !!word.is_favorite;
    word.is_favorite = !prev;
    update(word);

    VOCAB_API.post('/api/favorite/' + word.id + '/', {})
      .then(function (res) {
        if (res && res.success) {
          word.is_favorite = !!res.is_favorite;
          update(word);
          showToast(res.is_favorite ? '已加入收藏词汇' : '已取消收藏', 'success');
        } else {
          throw new Error((res && res.error) || '操作失败');
        }
      })
      .catch(function (e) {
        word.is_favorite = prev;
        update(word);
        showToast((e && e.message) || '操作失败，请重试', 'error');
      })
      .then(function () {
        if (btn) btn.disabled = false;
      });
  }

  function init(options) {
    if (initialized) return;
    initialized = true;
    options = options || {};
    getCurrentWord = options.getCurrentWord || function () { return null; };
    var btn = $('favBtn');
    if (btn) btn.addEventListener('click', toggle);
  }

  window.WordFavorite = {
    init: init,
    update: update,
    toggle: toggle,
  };
})();
