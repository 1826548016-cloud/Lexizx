// ===== Font Size =====
function initFontSize() {
  var size = localStorage.getItem('vocab-font-size') || 'medium';
  document.documentElement.setAttribute('data-font-size', size);
}

// ===== 全局自定义弹窗（替代系统原生黑框 alert/confirm，居中且跟随主题）=====
var AppDialog = (function () {
  var CSS = ''
    + '.app-dialog-mask{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;'
    + 'background:rgba(30,25,15,.38);backdrop-filter:blur(2px);animation:appDlgFade .18s ease;}'
    + '.app-dialog{width:min(440px,calc(100vw - 48px));max-height:70vh;overflow:auto;border-radius:var(--radius-lg);'
    + 'background:var(--c-surface);box-shadow:var(--shadow-lg);border:1px solid var(--c-border-light);'
    + 'padding:22px 24px 18px;animation:appDlgPop .2s cubic-bezier(.2,.9,.3,1.2);}'
    + '.app-dialog-body{color:var(--c-text);font-size:.95rem;line-height:1.75;white-space:pre-wrap;word-break:break-word;}'
    + '.app-dialog-footer{display:flex;justify-content:flex-end;gap:10px;margin-top:20px;}'
    + '@keyframes appDlgFade{from{opacity:0}to{opacity:1}}'
    + '@keyframes appDlgPop{from{opacity:0;transform:scale(.94) translateY(8px)}to{opacity:1;transform:none}}';

  function ensureCss() {
    if (document.getElementById('app-dialog-css')) return;
    var st = document.createElement('style');
    st.id = 'app-dialog-css';
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  // 同时只显示一个；新弹窗入队
  var queue = [];
  var active = false;

  function next() {
    active = false;
    if (queue.length) {
      var job = queue.shift();
      run(job.message, job.showCancel, job.resolve, job.opts);
    }
  }

  function run(message, showCancel, resolve, opts) {
    opts = opts || {};
    active = true;
    ensureCss();
    var mask = document.createElement('div');
    mask.className = 'app-dialog-mask';
    var dlg = document.createElement('div');
    dlg.className = 'app-dialog';
    dlg.setAttribute('role', 'dialog');

    var body = document.createElement('div');
    body.className = 'app-dialog-body';
    body.textContent = message == null ? '' : String(message);
    dlg.appendChild(body);

    var input = null;
    if (opts.withInput) {
      input = document.createElement('input');
      input.type = 'text';
      input.className = 'form-input';
      input.value = opts.defaultValue || '';
      input.style.marginTop = '14px';
      dlg.appendChild(input);
    }

    var footer = document.createElement('div');
    footer.className = 'app-dialog-footer';

    function close(result) {
      document.removeEventListener('keydown', onKey, true);
      mask.remove();
      resolve(result);
      next();
    }

    var okBtn = document.createElement('button');
    okBtn.className = 'btn btn-primary btn-sm';
    okBtn.textContent = '确定';
    okBtn.onclick = function () { close(input ? input.value : true); };

    if (showCancel) {
      var cancelBtn = document.createElement('button');
      cancelBtn.className = 'btn btn-secondary btn-sm';
      cancelBtn.textContent = '取消';
      cancelBtn.onclick = function () { close(input ? null : false); };
      footer.appendChild(cancelBtn);
    }
    footer.appendChild(okBtn);
    dlg.appendChild(footer);
    mask.appendChild(dlg);
    document.body.appendChild(mask);

    function onKey(e) {
      if (e.key === 'Enter') { e.preventDefault(); close(input ? input.value : true); }
      else if (e.key === 'Escape' && showCancel) { e.preventDefault(); close(input ? null : false); }
    }
    document.addEventListener('keydown', onKey, true);
    setTimeout(function () { if (input) { input.focus(); input.select(); } else { okBtn.focus(); } }, 30);
  }

  function open(message, showCancel, opts) {
    return new Promise(function (resolve) {
      if (active) { queue.push({ message: message, showCancel: showCancel, resolve: resolve, opts: opts }); return; }
      run(message, showCancel, resolve, opts);
    });
  }

  return {
    // 提示框（单按钮）
    alert: function (message) { return open(message, false); },
    // 确认框：Promise<boolean>
    confirm: function (message) { return open(message, true); },
    // 输入框：确定返回字符串，取消返回 null
    prompt: function (message, defaultValue) {
      return open(message, true, { withInput: true, defaultValue: defaultValue });
    },
  };
})();

function appAlert(message) { return AppDialog.alert(message); }
function appConfirm(message) { return AppDialog.confirm(message); }
function appPrompt(message, defaultValue) { return AppDialog.prompt(message, defaultValue); }

// 全局接管原生 alert：所有现有 alert() 零改动自动变成自定义弹窗
window.alert = function (message) { AppDialog.alert(message); };

// ===== Toast =====
function showToast(message, type, duration) {
  type = type || 'success';
  duration = duration || 2600;
  var container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  var t = document.createElement('div');
  t.className = 'toast toast-' + type;
  t.textContent = message;
  container.appendChild(t);
  setTimeout(function () {
    t.style.opacity = '0';
    t.style.transform = 'translateY(-10px)';
    t.style.transition = 'all .25s ease';
    setTimeout(function () { t.remove(); }, 250);
  }, duration);
}

// ===== API Helper =====
var VOCAB_API = {
  get: async function (url) {
    var r = await fetch(url);
    return r.json();
  },
  post: async function (url, data) {
    data = data || {};
    var csrf = document.querySelector('[name=csrfmiddlewaretoken]');
    var r = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrf ? csrf.value : '',
      },
      body: JSON.stringify(data),
    });
    return r.json();
  },
  del: async function (url, data) {
    if (data) {
      var csrf = document.querySelector('[name=csrfmiddlewaretoken]');
      var r = await fetch(url, {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrf ? csrf.value : '',
        },
        body: JSON.stringify(data),
      });
      return r.json();
    }
    var csrf = document.querySelector('[name=csrfmiddlewaretoken]');
    var r = await fetch(url, {
      method: 'DELETE',
      headers: { 'X-CSRFToken': csrf ? csrf.value : '' },
    });
    return r.json();
  },
};

// ===== Study Time =====
// 学习页显式调用 start() 后，仅在页面可见时计时；切换标签页、最小化窗口或离开页面都会暂停并同步。
// 多标签页去重：通过 BroadcastChannel + localStorage 锁，保证同一时刻只有一个标签页上报时长。
window.VocabStudyTimer = (function () {
  var active = false;
  var lastActiveAt = 0;
  var pendingSeconds = 0;
  var intervalId = null;
  var isMaster = false; // 是否为持有计时报的主标签页
  var TAB_ID = 'tab-' + Date.now() + '-' + Math.random().toString(36).slice(2);
  var LOCK_KEY = 'vocab_study_timer_master';
  var channel = null;
  try { channel = new BroadcastChannel('vocab_study_timer'); } catch (e) {}

  // 尝试获取主标签页锁
  function acquireLock() {
    if (isMaster) return true;
    var current = localStorage.getItem(LOCK_KEY);
    // 锁超过 8 秒视为过期
    if (current && Date.now() - parseInt(current.split('|')[1], 10) < 8000) return false;
    localStorage.setItem(LOCK_KEY, TAB_ID + '|' + Date.now());
    // 再读一次确认自己抢到了
    if (localStorage.getItem(LOCK_KEY).indexOf(TAB_ID) === 0) {
      isMaster = true;
      if (channel) channel.postMessage({ type: 'master_changed', tab: TAB_ID });
      return true;
    }
    return false;
  }

  // 定期续租锁
  function renewLock() {
    if (!isMaster) return;
    localStorage.setItem(LOCK_KEY, TAB_ID + '|' + Date.now());
  }

  function releaseLock() {
    if (!isMaster) return;
    isMaster = false;
    localStorage.removeItem(LOCK_KEY);
    if (channel) channel.postMessage({ type: 'master_changed', tab: null });
  }

  function collect() {
    if (!active || !isMaster || document.hidden || !lastActiveAt) return;
    var seconds = Math.floor((Date.now() - lastActiveAt) / 1000);
    if (seconds > 0) pendingSeconds += seconds;
    lastActiveAt = Date.now();
  }

  function flush() {
    collect();
    var seconds = Math.min(pendingSeconds, 90);
    if (!seconds) return;
    pendingSeconds -= seconds;
    var csrf = document.querySelector('[name=csrfmiddlewaretoken]');
    fetch('/api/study-duration/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrf ? csrf.value : '',
      },
      body: JSON.stringify({ seconds: seconds }),
      keepalive: true,
    }).catch(function () { pendingSeconds += seconds; });
  }

  function start() {
    if (active) return;
    active = true;
    intervalId = setInterval(function () {
      // 每个周期：尝试获取锁、续租锁、如果可见则计时
      if (!isMaster) acquireLock();
      if (isMaster) {
        renewLock();
        if (!document.hidden) {
          if (!lastActiveAt) lastActiveAt = Date.now();
          flush();
        }
      }
    }, 20000);
    // 立即尝试获取锁
    if (!document.hidden) acquireLock();
    lastActiveAt = document.hidden ? 0 : Date.now();
  }

  function stop() {
    if (!active) return;
    flush();
    releaseLock();
    active = false;
    lastActiveAt = 0;
    if (intervalId) clearInterval(intervalId);
    intervalId = null;
  }

  document.addEventListener('visibilitychange', function () {
    if (!active) return;
    if (document.hidden) {
      flush();
      releaseLock();
      lastActiveAt = 0;
    } else {
      acquireLock();
      lastActiveAt = Date.now();
    }
  });

  // 监听其他标签页释放锁的消息，尝试接管
  if (channel) {
    channel.onmessage = function (e) {
      if (e.data && e.data.type === 'master_changed' && !e.data.tab && active && !document.hidden) {
        acquireLock();
      }
    };
  }

  // 页面卸载时释放锁
  window.addEventListener('pagehide', function () {
    flush();
    if (active) releaseLock();
  });

  return { start: start, stop: stop, flush: flush };
})();

// ===== 刷新当前页 =====
// 全站共用的刷新按钮（base.html 顶栏）。刷新前先把学习时长同步给服务端，
// 避免刚计时未上报的几十秒随页面卸载丢掉；随后整页重载，数据全部从服务端重取。
function refreshPage(btn) {
  if (btn && btn.disabled) return;      // 连点保护
  if (btn) {
    btn.disabled = true;
    var icon = btn.querySelector('i');
    if (icon) icon.classList.add('spinning');
  }
  var go = function () { location.reload(); };
  var timer = window.VocabStudyTimer;
  if (timer && typeof timer.flush === 'function') {
    try {
      timer.flush();
      setTimeout(go, 120);              // 给 flush 的请求一点发出时间
      return;
    } catch (e) { /* 计时器异常不阻塞刷新 */ }
  }
  go();
}

// ===== Init =====
document.addEventListener('DOMContentLoaded', function () {
  initFontSize();
});
