// ===== AI 模型配置独立窗口（API 填写）=====
// 全局可用：AI 单词导入 / 设置 / 小助手等页面均可通过 openAddModel() 打开此窗口。
// 配置统一存入数据库 AIModel 表，后端 resolve_ai_model 读取。
// 保存成功后调用 window.aiModelModalOnSaved 钩子（由当前页面注册，用于刷新模型列表/选择框）。

// 模型服务商预设（全部为 OpenAI 兼容接口），models 元素: { id, vision: 是否支持图片识别 }
// 预置模型 ID 仅为常用推荐，各家新模型可随时通过「使用其他模型」手动填写
var AI_PROVIDERS = [
  // ---- 国内 ----
  { id: 'deepseek', name: 'DeepSeek（深度求索）', base: 'https://api.deepseek.com/v1', models: [
    { id: 'deepseek-chat', vision: false }, { id: 'deepseek-reasoner', vision: false },
  ]},
  { id: 'qwen', name: '通义千问（阿里云百炼）', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', models: [
    { id: 'qwen-plus', vision: false }, { id: 'qwen-max', vision: false },
    { id: 'qwen-turbo', vision: false }, { id: 'qwen-vl-max', vision: true },
  ]},
  { id: 'zhipu', name: '智谱 GLM（BigModel）', base: 'https://open.bigmodel.cn/api/paas/v4', models: [
    { id: 'glm-4-plus', vision: false }, { id: 'glm-4-air', vision: false },
    { id: 'glm-4-flash', vision: false }, { id: 'glm-4v-plus', vision: true },
  ]},
  { id: 'moonshot', name: 'Kimi（月之暗面）', base: 'https://api.moonshot.cn/v1', models: [
    { id: 'moonshot-v1-8k', vision: false }, { id: 'moonshot-v1-32k', vision: false },
    { id: 'moonshot-v1-128k', vision: false }, { id: 'moonshot-v1-8k-vision-preview', vision: true },
  ]},
  { id: 'doubao', name: '豆包（火山方舟）', base: 'https://ark.cn-beijing.volces.com/api/v3', models: [
    { id: 'doubao-1-5-pro-32k-250115', vision: false }, { id: 'doubao-1-5-vision-pro-32k-250115', vision: true },
  ]},
  { id: 'qianfan', name: '文心一言（百度千帆）', base: 'https://qianfan.baidubce.com/v2', models: [
    { id: 'ernie-4.0-8k', vision: false }, { id: 'ernie-4.0-turbo-8k', vision: false },
    { id: 'ernie-speed-128k', vision: false },
  ]},
  { id: 'hunyuan', name: '腾讯混元', base: 'https://api.hunyuan.cloud.tencent.com/v1', models: [
    { id: 'hunyuan-turbos-latest', vision: false }, { id: 'hunyuan-large', vision: false },
    { id: 'hunyuan-lite', vision: false },
  ]},
  { id: 'spark', name: '讯飞星火', base: 'https://spark-api-open.xf-yun.com/v1', models: [
    { id: 'generalv3.5', vision: false }, { id: '4.0Ultra', vision: false },
  ]},
  { id: 'siliconflow', name: '硅基流动 SiliconFlow', base: 'https://api.siliconflow.cn/v1', models: [
    { id: 'deepseek-ai/DeepSeek-V3', vision: false }, { id: 'deepseek-ai/DeepSeek-R1', vision: false },
    { id: 'Qwen/Qwen2.5-72B-Instruct', vision: false },
  ]},
  { id: 'minimax', name: 'MiniMax', base: 'https://api.minimaxi.com/v1', models: [
    { id: 'MiniMax-Text-01', vision: false }, { id: 'abab6.5s-chat', vision: false },
  ]},
  // ---- 国外（需可访问外网）----
  { id: 'openai', name: 'OpenAI', base: 'https://api.openai.com/v1', models: [
    { id: 'gpt-4o', vision: true }, { id: 'gpt-4o-mini', vision: true },
  ]},
  { id: 'gemini', name: 'Google Gemini', base: 'https://generativelanguage.googleapis.com/v1beta/openai/', models: [
    { id: 'gemini-2.5-pro', vision: true }, { id: 'gemini-2.5-flash', vision: true },
    { id: 'gemini-2.0-flash', vision: true },
  ]},
  { id: 'xai', name: 'xAI Grok', base: 'https://api.x.ai/v1', models: [
    { id: 'grok-3', vision: false }, { id: 'grok-3-mini', vision: false },
    { id: 'grok-2-vision-1212', vision: true },
  ]},
  { id: 'openrouter', name: 'OpenRouter（聚合）', base: 'https://openrouter.ai/api/v1', models: [
    { id: 'openai/gpt-4o-mini', vision: true }, { id: 'anthropic/claude-3.5-sonnet', vision: true },
    { id: 'google/gemini-2.0-flash-001', vision: true },
  ]},
  { id: 'groq', name: 'Groq', base: 'https://api.groq.com/openai/v1', models: [
    { id: 'llama-3.3-70b-versatile', vision: false }, { id: 'llama-3.1-8b-instant', vision: false },
  ]},
  { id: 'mistral', name: 'Mistral AI', base: 'https://api.mistral.ai/v1', models: [
    { id: 'mistral-large-latest', vision: false }, { id: 'mistral-small-latest', vision: false },
    { id: 'pixtral-large-latest', vision: true },
  ]},
  // ---- 其他 ----
  { id: 'codex2api', name: 'Codex2API', base: 'https://www.codex2api.com/v1', models: [
    { id: 'gpt-5.6-sol', vision: true }, { id: 'gpt-5.2', vision: true },
    { id: 'gpt-5.2-chat-latest', vision: true }, { id: 'gpt-5.2-pro', vision: true },
    { id: 'gpt-5.3-codex', vision: true }, { id: 'gpt-5.3-codex-spark', vision: true },
  ]},
  { id: 'custom', name: '自定义（OpenAI 兼容）', base: 'https://api.openai.com/v1', models: [] },
];

function aiProviderInfo(pid) {
  return AI_PROVIDERS.find(function (p) { return p.id === pid; }) || AI_PROVIDERS[AI_PROVIDERS.length - 1];
}

function openAddModel(m) {
  fillAddModel(m || null);
  var editIdEl = document.getElementById('amEditId');
  editIdEl.value = m ? m.id : '';
  editIdEl.dataset.hasKey = m && m.has_key ? '1' : '';
  document.getElementById('amModalTitle').textContent = m ? '编辑模型' : '添加模型';
  document.getElementById('amSubmitBtn').innerHTML = '<i class="ph ph-check"></i> ' + (m ? '保存模型' : '添加模型');
  document.getElementById('addModelModal').classList.remove('hidden');
}

function closeAddModel() {
  document.getElementById('addModelModal').classList.add('hidden');
}

function fillAddModel(m) {
  var psel = document.getElementById('amProvider');
  psel.innerHTML = '';
  AI_PROVIDERS.forEach(function (p) {
    var opt = document.createElement('option');
    opt.value = p.id; opt.textContent = p.name;
    psel.appendChild(opt);
  });
  psel.value = m ? m.provider : AI_PROVIDERS[0].id;
  renderAMPresets(psel.value);

  var preset = document.getElementById('amModelPreset');
  var inPreset = Array.prototype.some.call(preset.options, function (o) { return o.value === (m ? m.model_id : ''); });
  if (m && !inPreset) {
    preset.value = '__custom__';
    document.getElementById('amCustomModelWrap').classList.remove('hidden');
    document.getElementById('amModelId').value = m.model_id;
  } else if (m) {
    preset.value = m.model_id;
    document.getElementById('amCustomModelWrap').classList.add('hidden');
  } else {
    document.getElementById('amCustomModelWrap').classList.add('hidden');
  }

  var keyInput = document.getElementById('amApiKey');
  keyInput.value = m ? (m.api_key || '') : '';
  keyInput.placeholder = (m && m.has_key && !m.api_key)
    ? '已保存密钥，留空表示不修改'
    : 'sk-...（无鉴权服务可不填）';
  document.getElementById('amBaseUrl').value = m ? (m.endpoint || m.base_url || '') : '';
  document.getElementById('amFullUrl').checked = !!(m && m.endpoint);
  onAMFullUrlChange();
  document.getElementById('amDisplayName').value = m ? (m.display_name || '') : '';
  document.getElementById('amContext').value = m ? (m.context || '128K') : '128K';
  document.getElementById('amAdvanced').classList.add('hidden');
  document.getElementById('amAdvCaret').className = 'ph ph-caret-right';
  document.getElementById('amTestResult').className = 'ai-test-result';
  document.getElementById('amTestResult').innerHTML = '';
}

function changeAMProvider() {
  renderAMPresets(document.getElementById('amProvider').value);
}

function renderAMPresets(pid) {
  var p = aiProviderInfo(pid);
  var sel = document.getElementById('amModelPreset');
  sel.innerHTML = '';
  p.models.forEach(function (m) {
    var opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.id + (m.vision ? '（视觉）' : '（文本模式）');
    sel.appendChild(opt);
  });
  var opt2 = document.createElement('option');
  opt2.value = '__custom__'; opt2.textContent = '使用其他模型';
  sel.appendChild(opt2);
  sel.value = p.models.length ? p.models[0].id : '__custom__';
  onAMPresetChange();
}

function onAMPresetChange() {
  var custom = document.getElementById('amModelPreset').value === '__custom__';
  document.getElementById('amCustomModelWrap').classList.toggle('hidden', !custom);
  if (custom) document.getElementById('amModelId').value = '';
}

function toggleAMAdvanced() {
  var adv = document.getElementById('amAdvanced');
  var show = adv.classList.toggle('hidden');
  document.getElementById('amAdvCaret').className = show ? 'ph ph-caret-right' : 'ph ph-caret-down';
}

function onAMFullUrlChange() {
  var full = document.getElementById('amFullUrl').checked;
  var input = document.getElementById('amBaseUrl');
  var hint = document.getElementById('amUrlHint');
  if (full) {
    input.placeholder = 'http://localhost:4096/v1/chat/completions';
    hint.textContent = '将直接使用该完整地址发起请求（不再自动拼接 /chat/completions）';
  } else {
    input.placeholder = 'https://api.openai.com/v1';
    hint.textContent = '关闭：填基础地址，自动拼接 /chat/completions；打开：直接填完整请求地址，如 http://localhost:4096/v1/chat/completions';
  }
}

function toggleAMKeyVisible() {
  var input = document.getElementById('amApiKey');
  var icon = document.querySelector('#amKeyToggle i');
  if (input.type === 'password') {
    input.type = 'text';
    icon.className = 'ph ph-eye-slash';
  } else {
    input.type = 'password';
    icon.className = 'ph ph-eye';
  }
}

// 读取弹窗表单并组装为测试/保存所需参数；校验失败返回 null
function readAMForm() {
  var editId = document.getElementById('amEditId').value;
  var provider = document.getElementById('amProvider').value;
  var preset = document.getElementById('amModelPreset').value;
  var modelId = preset === '__custom__' ? document.getElementById('amModelId').value.trim() : preset;
  var apiKey = document.getElementById('amApiKey').value.trim();
  var customUrl = document.getElementById('amBaseUrl').value.trim();
  var fullUrl = document.getElementById('amFullUrl').checked;
  var displayName = document.getElementById('amDisplayName').value.trim();
  var context = document.getElementById('amContext').value;

  if (!modelId) { showToast('请填写模型 ID', 'error'); return null; }

  // 视觉能力判断：预置模型按标记，自定义默认支持；非视觉模型可通过文本/文件方式导入
  var vision = true;
  if (preset !== '__custom__') {
    var pCheck = aiProviderInfo(provider);
    var presetObj = pCheck.models.find(function (x) { return x.id === preset; });
    vision = presetObj ? presetObj.vision : true;
  }

  var p = aiProviderInfo(provider);
  var endpoint = '';                 // 完整请求地址（打开"完整 URL"开关时）
  var baseUrl = p.base;              // 基础地址（关闭开关时）
  if (fullUrl) {
    endpoint = customUrl;
    if (!endpoint) { showToast('请填写完整的请求地址，如 http://localhost:4096/v1/chat/completions', 'error'); return null; }
    baseUrl = '';
  } else if (customUrl) {
    baseUrl = customUrl;
  }

  return {
    editId: editId, provider: provider, modelId: modelId, apiKey: apiKey,
    baseUrl: baseUrl, endpoint: endpoint, displayName: displayName,
    context: context, vision: vision,
  };
}

// 调用后端测试接口，把结果渲染到 amTestResult；返回是否成功
async function runAMConnectionTest(f) {
  var box = document.getElementById('amTestResult');
  box.className = 'ai-test-result ai-test-loading';
  box.innerHTML = '<span class="ai-test-loading"><i class="ph ph-circle-notch" style="animation: spin 1s linear infinite;"></i> 正在验证连接…</span>';
  try {
    var res = await VOCAB_API.post('/api/ai/test/', {
      api_key: f.apiKey,
      base_url: f.baseUrl,
      endpoint: f.endpoint,
      model: f.modelId,
    });
    if (res && res.success) {
      box.className = 'ai-test-result ai-test-ok';
      box.innerHTML = '<i class="ph ph-check-circle"></i> 连接成功，密钥有效，模型可正常调用';
      return true;
    }
    box.className = 'ai-test-result ai-test-fail';
    var hint = res && res.key_invalid ? '（密钥无效，请检查后重试）'
      : (res && res.model_invalid ? '（模型或接口地址错误）' : '');
    box.innerHTML = '<i class="ph ph-x-circle"></i> ' + ((res && res.error) || '验证失败') + hint;
    return false;
  } catch (e) {
    box.className = 'ai-test-result ai-test-fail';
    box.innerHTML = '<i class="ph ph-x-circle"></i> ' + ((e && e.error) || '验证请求失败，请检查网络或接口地址');
    return false;
  }
}

// 独立「测试连接」：只验证当前填写的内容，不保存
async function testAMConnection() {
  var f = readAMForm();
  if (!f) return;
  var btn = document.getElementById('amTestOnlyBtn');
  btn.disabled = true;
  btn.innerHTML = '<i class="ph ph-circle-notch" style="animation: spin 1s linear infinite;"></i> 测试中…';
  try {
    await runAMConnectionTest(f);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="ph ph-plugs-connected"></i> 测试连接';
  }
}

// 提交添加/编辑模型：先测试连接，成功后保存到数据库，再刷新当前页面的模型列表/选择框
async function submitAddModel() {
  var f = readAMForm();
  if (!f) return;

  var editId = f.editId;
  var btn = document.getElementById('amSubmitBtn');
  btn.disabled = true;
  btn.innerHTML = '<i class="ph ph-circle-notch" style="animation: spin 1s linear infinite;"></i> 验证连接中…';

  try {
    // 编辑已有密钥的模型且密钥框留空 = 不修改密钥，跳过连接测试直接保存
    var keyUnchanged = !!editId && !f.apiKey &&
      document.getElementById('amEditId').dataset.hasKey === '1';
    var ok = true;
    if (!keyUnchanged) {
      ok = await runAMConnectionTest(f);
    }
    if (!ok) return;

    var saveRes = await VOCAB_API.post('/api/ai-models/', {
      id: editId || undefined,
      provider: f.provider,
      model_id: f.modelId,
      display_name: f.displayName,
      base_url: f.baseUrl,
      endpoint: f.endpoint,
      api_key: f.apiKey,
      context: f.context,
      vision: f.vision,
      enabled: true,
    });
    var box = document.getElementById('amTestResult');
    if (!saveRes.success) {
      box.className = 'ai-test-result ai-test-fail';
      box.innerHTML = '<i class="ph ph-x-circle"></i> ' + (saveRes.error || '保存失败');
      return;
    }
    box.className = 'ai-test-result ai-test-ok';
    box.innerHTML = '<i class="ph ph-check-circle"></i> 连接成功，模型已' + (editId ? '更新' : '添加');
    // 刷新当前页面：设置页刷新模型列表，AI 导入页刷新选择框等
    if (typeof window.aiModelModalOnSaved === 'function') {
      try { await window.aiModelModalOnSaved(); } catch (e) {}
    }
    setTimeout(function () { closeAddModel(); }, 700);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="ph ph-check"></i> ' + (editId ? '保存模型' : '添加模型');
  }
}

// ESC 关闭弹窗
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && !document.getElementById('addModelModal').classList.contains('hidden')) {
    closeAddModel();
  }
});
