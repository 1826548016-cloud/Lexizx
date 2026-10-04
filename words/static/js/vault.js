// ===== API 密钥保险箱（前端）=====
// 全局能力：
// - 设置页 #vaultBar 状态条（设置/解锁/锁定/忘记密码重置）
// - 任意页面 AI 调用返回"保险箱已锁定"时自动弹出解锁框
// - 包装 openAddModel：已配置但未解锁时先解锁；编辑模型时拉取明文回填
var Vault = (function () {
  var state = { configured: false, unlocked: false, key_count: 0 };
  var unlockCbs = [];      // 解锁成功后的待执行回调
  var autoPromptShown = false; // 同一次锁定周期只自动弹一次

  // ---------- 弹窗 DOM ----------
  function injectDialogs() {
    if (document.getElementById('vaultDialogs')) return;
    var box = document.createElement('div');
    box.id = 'vaultDialogs';
    box.innerHTML =
      '<div id="vaultSetupMask" class="modal-overlay hidden" onclick="if(event.target===this)Vault.closeSetup()">' +
      '  <div class="modal" style="max-width: 460px;">' +
      '    <div class="flex justify-between items-center" style="margin-bottom:14px;">' +
      '      <h3 style="margin:0;font-size:var(--fs-lg);"><i class="ph ph-lock-key" style="color:var(--c-red);"></i> 设置密钥保险箱密码</h3>' +
      '      <button class="btn btn-ghost" onclick="Vault.closeSetup()"><i class="ph ph-x"></i></button>' +
      '    </div>' +
      '    <p class="text-sm text-secondary" style="margin:0 0 12px;line-height:1.7;">设置后，所有 API 密钥将以加密形式保存在本机，只有输入密码解锁后才能使用 AI 功能。</p>' +
      '    <div class="form-group"><label class="form-label">保险箱密码</label>' +
      '      <input type="password" id="vaultSetupPwd1" class="form-input" placeholder="至少 4 位" autocomplete="off"></div>' +
      '    <div class="form-group"><label class="form-label">再次输入密码</label>' +
      '      <input type="password" id="vaultSetupPwd2" class="form-input" placeholder="再次确认" autocomplete="off"></div>' +
      '    <div style="background:var(--c-red-bg,#fdecec);border:1px solid var(--c-red);color:var(--c-red);border-radius:10px;padding:10px 12px;font-size:13px;line-height:1.6;margin-bottom:14px;">' +
      '      <i class="ph ph-warning-circle"></i> 请务必牢记密码。忘记密码无法找回，只能清空全部已保存的 API 密钥后重新填写。' +
      '    </div>' +
      '    <div id="vaultSetupErr" class="text-sm" style="color:var(--c-red);margin-bottom:10px;display:none;"></div>' +
      '    <div class="flex gap-2 justify-end">' +
      '      <button class="btn btn-secondary" onclick="Vault.closeSetup()">取消</button>' +
      '      <button class="btn btn-primary" id="vaultSetupBtn" onclick="Vault.submitSetup()"><i class="ph ph-check"></i> 设置并加密现有密钥</button>' +
      '    </div>' +
      '  </div>' +
      '</div>' +
      '<div id="vaultUnlockMask" class="modal-overlay hidden" onclick="if(event.target===this)Vault.closeUnlock()">' +
      '  <div class="modal" style="max-width: 420px;">' +
      '    <div class="flex justify-between items-center" style="margin-bottom:14px;">' +
      '      <h3 style="margin:0;font-size:var(--fs-lg);"><i class="ph ph-lock-key" style="color:var(--c-blue);"></i> 解锁密钥保险箱</h3>' +
      '      <button class="btn btn-ghost" onclick="Vault.closeUnlock()"><i class="ph ph-x"></i></button>' +
      '    </div>' +
      '    <p class="text-sm text-secondary" style="margin:0 0 12px;line-height:1.7;">API 密钥已加密。输入保险箱密码后本次运行保持解锁，重启软件后需重新解锁。</p>' +
      '    <div class="form-group"><label class="form-label">保险箱密码</label>' +
      '      <input type="password" id="vaultUnlockPwd" class="form-input" placeholder="输入密码" autocomplete="off"></div>' +
      '    <div id="vaultUnlockErr" class="text-sm" style="color:var(--c-red);margin-bottom:10px;display:none;"></div>' +
      '    <div class="flex justify-between items-center">' +
      '      <button type="button" class="text-sm" style="background:none;border:none;color:var(--c-text-3);cursor:pointer;padding:0;text-decoration:underline;" onclick="Vault.openReset()">忘记密码？</button>' +
      '      <div class="flex gap-2">' +
      '        <button class="btn btn-secondary" onclick="Vault.closeUnlock()">取消</button>' +
      '        <button class="btn btn-primary" id="vaultUnlockBtn" onclick="Vault.submitUnlock()"><i class="ph ph-lock-key-open"></i> 解锁</button>' +
      '      </div>' +
      '    </div>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(box);
    document.getElementById('vaultSetupPwd2').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') Vault.submitSetup();
    });
    document.getElementById('vaultUnlockPwd').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') Vault.submitUnlock();
    });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ---------- 状态 ----------
  async function refresh() {
    try {
      var s = await VOCAB_API.get('/api/vault/status/');
      // 原地合并，保证外部持有 Vault.state 引用始终拿到最新值
      Object.keys(state).forEach(function (k) { delete state[k]; });
      Object.assign(state, s);
    } catch (e) {
      // 网络异常保持原状态
    }
    renderBar();
    document.dispatchEvent(new CustomEvent('vaultchanged', { detail: state }));
    return state;
  }

  function renderBar() {
    var bar = document.getElementById('vaultBar');
    if (!bar) return;
    if (!state.configured) {
      bar.className = 'vault-bar vault-warn';
      bar.innerHTML =
        '<span><i class="ph ph-lock-open"></i> API 密钥当前以明文保存在本机数据库，建议立即设置保险箱密码加密</span>' +
        '<button class="btn btn-sm btn-primary" onclick="Vault.openSetup()"><i class="ph ph-lock-key"></i> 设置密码</button>';
    } else if (state.unlocked) {
      bar.className = 'vault-bar vault-ok';
      bar.innerHTML =
        '<span><i class="ph ph-lock-key-open"></i> 密钥保险箱已解锁（本次运行有效，重启软件自动锁定），已保护 ' +
        (state.key_count || 0) + ' 个密钥</span>' +
        '<button class="btn btn-sm btn-secondary" onclick="Vault.lockNow()"><i class="ph ph-lock"></i> 立即锁定</button>';
    } else {
      bar.className = 'vault-bar vault-locked';
      bar.innerHTML =
        '<span><i class="ph ph-lock-key"></i> 密钥保险箱已锁定，AI 功能需解锁后使用，已保护 ' +
        (state.key_count || 0) + ' 个密钥</span>' +
        '<button class="btn btn-sm btn-primary" onclick="Vault.openUnlock()"><i class="ph ph-lock-key-open"></i> 输入密码解锁</button>';
    }
  }

  // ---------- 设置密码 ----------
  function openSetup() {
    injectDialogs();
    document.getElementById('vaultSetupPwd1').value = '';
    document.getElementById('vaultSetupPwd2').value = '';
    document.getElementById('vaultSetupErr').style.display = 'none';
    document.getElementById('vaultSetupMask').classList.remove('hidden');
    setTimeout(function () { document.getElementById('vaultSetupPwd1').focus(); }, 50);
  }
  function closeSetup() {
    var m = document.getElementById('vaultSetupMask');
    if (m) m.classList.add('hidden');
  }
  async function submitSetup() {
    var p1 = document.getElementById('vaultSetupPwd1').value;
    var p2 = document.getElementById('vaultSetupPwd2').value;
    var err = document.getElementById('vaultSetupErr');
    if (p1.length < 4) { err.textContent = '密码至少 4 位'; err.style.display = 'block'; return; }
    if (p1 !== p2) { err.textContent = '两次输入的密码不一致'; err.style.display = 'block'; return; }
    var btn = document.getElementById('vaultSetupBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="ph ph-circle-notch" style="animation:spin 1s linear infinite;"></i> 加密中…';
    try {
      var res = await VOCAB_API.post('/api/vault/setup/', { password: p1 });
      if (res && res.success) {
        closeSetup();
        await refresh();
        if (typeof showToast === 'function') {
          showToast('保险箱已启用，' + (res.migrated || 0) + ' 个现有密钥已加密', 'success');
        }
        flushUnlockCbs();
      } else {
        err.textContent = (res && res.error) || '设置失败';
        err.style.display = 'block';
      }
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<i class="ph ph-check"></i> 设置并加密现有密钥';
    }
  }

  // ---------- 解锁 ----------
  function openUnlock(cb) {
    injectDialogs();
    document.getElementById('vaultUnlockPwd').value = '';
    document.getElementById('vaultUnlockErr').style.display = 'none';
    document.getElementById('vaultUnlockMask').classList.remove('hidden');
    setTimeout(function () { document.getElementById('vaultUnlockPwd').focus(); }, 50);
    if (typeof cb === 'function') unlockCbs.push(cb);
  }
  function closeUnlock() {
    var m = document.getElementById('vaultUnlockMask');
    if (m) m.classList.add('hidden');
  }
  async function submitUnlock() {
    var pwd = document.getElementById('vaultUnlockPwd').value;
    var err = document.getElementById('vaultUnlockErr');
    if (!pwd) { err.textContent = '请输入密码'; err.style.display = 'block'; return; }
    var btn = document.getElementById('vaultUnlockBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="ph ph-circle-notch" style="animation:spin 1s linear infinite;"></i> 解锁中…';
    try {
      var res = await VOCAB_API.post('/api/vault/unlock/', { password: pwd });
      if (res && res.success) {
        autoPromptShown = false;
        closeUnlock();
        await refresh();
        if (typeof showToast === 'function') {
          showToast(res.migrated ? '已解锁，' + res.migrated + ' 个密钥已自动加密' : '保险箱已解锁', 'success');
        }
        flushUnlockCbs();
      } else {
        err.textContent = (res && res.error) || '解锁失败';
        err.style.display = 'block';
      }
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<i class="ph ph-lock-key-open"></i> 解锁';
    }
  }
  function flushUnlockCbs() {
    var cbs = unlockCbs.splice(0);
    cbs.forEach(function (fn) { try { fn(); } catch (e) {} });
  }

  // ---------- 锁定 / 重置 ----------
  async function lockNow() {
    await VOCAB_API.post('/api/vault/lock/', {});
    await refresh();
    if (typeof showToast === 'function') showToast('保险箱已锁定', 'success');
  }

  async function openReset() {
    var msg = '忘记密码无法找回。\n\n重置将【清空全部已保存的 API 密钥】和保险箱密码，模型的其他配置（服务商、模型 ID、地址）会保留，之后需要重新填写密钥。\n\n确定要重置吗？';
    var ok = false;
    if (typeof appConfirm === 'function') {
      ok = await appConfirm(msg);
    } else {
      ok = window.confirm(msg);
    }
    if (!ok) return;
    var res = await VOCAB_API.post('/api/vault/reset/', { confirm: true });
    closeUnlock();
    await refresh();
    if (res && res.success && typeof showToast === 'function') {
      showToast('已重置，' + (res.cleared || 0) + ' 个密钥已清空，请重新填写', 'success');
    }
  }

  // ---------- 编辑模型时拉取明文 ----------
  async function revealKey(id) {
    try {
      var res = await VOCAB_API.post('/api/vault/reveal/', { id: id });
      if (res && res.success) return res.api_key || '';
    } catch (e) {}
    return null;
  }

  return {
    state: state,
    refresh: refresh,
    renderBar: renderBar,
    openSetup: openSetup,
    closeSetup: closeSetup,
    submitSetup: submitSetup,
    openUnlock: openUnlock,
    closeUnlock: closeUnlock,
    submitUnlock: submitUnlock,
    lockNow: lockNow,
    openReset: openReset,
    revealKey: revealKey,
  };
})();

// ---------- 全局接管 ----------
(function () {
  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  ready(function () {
    Vault.refresh();

    // 1) 包装"添加/编辑模型"弹窗：已配置未解锁时先解锁；编辑已加密模型时拉明文回填
    if (typeof window.openAddModel === 'function') {
      var origOpenAddModel = window.openAddModel;
      window.openAddModel = function (m) {
        var doOpen = function (realM) { origOpenAddModel(realM); };
        if (Vault.state.configured && !Vault.state.unlocked) {
          Vault.openUnlock(function () {
            if (m && m.has_key) {
              Vault.revealKey(m.id).then(function (key) {
                if (key != null) m.api_key = key;
                doOpen(m);
              });
            } else doOpen(m);
          });
          return;
        }
        if (m && m.has_key) {
          Vault.revealKey(m.id).then(function (key) {
            if (key != null) m.api_key = key;
            doOpen(m);
          });
          return;
        }
        doOpen(m);
      };
    }

    // 2) 任意 AI 请求返回"保险箱锁定"时自动弹一次解锁框
    var origPost = VOCAB_API.post;
    VOCAB_API.post = function (url, data) {
      return origPost.call(this, url, data).then(function (res) {
        if (res && (res.code === 'vault_locked' ||
          (typeof res.error === 'string' && res.error.indexOf('保险箱') !== -1 && res.error.indexOf('解锁') !== -1))) {
          if (!Vault.state.unlocked && !Vault._autoLockShown) {
            Vault._autoLockShown = true;
            Vault.openUnlock(function () { Vault._autoLockShown = false; });
            Vault.refresh().then(function () {
              if (Vault.state.unlocked) Vault._autoLockShown = false;
            });
          }
        }
        return res;
      });
    };
  });
})();
