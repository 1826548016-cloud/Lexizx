# Lexizx 交接文档

> 面向接手开发的技术人员与 Agent。读完本文件（约 5 分钟）即可动工。
> 更新日期：2026-10-23｜仓库：<https://github.com/1826548016-cloud/Lexizx>

## 一、这是什么

考研英语学习 Windows 桌面应用（Django + SQLite + pywebview，PyInstaller 打包单文件 exe）。本机单用户、无注册、数据全在本机。核心功能：词汇背诵/复习（掌握状态机 + 间隔复习）、真题模拟考、英语一长难句翻译、六级汉译英、AI 组卷与 AI 学习助手（用户自配 OpenAI 兼容接口的模型，18 家预设）。

## 二、快速开始。

```powershell
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver          # http://127.0.0.1:8000/
```

测试（内存库，不碰开发库）：

```powershell
python manage.py test words.tests --settings=vocab_project.test_settings   # 当前 155 项通过
python manage.py check
python manage.py makemigrations --check --dry-run
```

打包（产物 `product/dist/word.exe`，中间文件 `product/build/`）：

```powershell
python -m PyInstaller --noconfirm --clean --distpath product/dist --workpath product/build product/word.spec
```

注意：本机开发用 `--noreload` 时，改完 Python/模板必须重启服务器才生效。

## 三、架构地图

| 位置                                               | 内容                                                 |
| ------------------------------------------------ | -------------------------------------------------- |
| `vocab_project/`                                 | Django 配置（settings.py、urls、wsgi）、test\_settings.py |
| `words/models.py`                                | 全部数据模型（单文件）                                        |
| `words/views.py`                                 | 页面/API 视图与大部分业务逻辑（文件很大，先定位专题再改，勿无关重构）              |
| `words/ai_prompts.py`、`words/ai_exam_prompts.py` | AI 提示词                                             |
| `words/templates/`、`words/static/`               | 模板与前端资源（静态引用带 `?v=` 版本戳防缓存）                        |
| `words/migrations/`                              | 迁移，当前最新 `0059_pendingjudgment_dedupe_key_and_more` |
| `product/launch.py`、`product/word.spec`          | 桌面版入口与 PyInstaller 配置                              |
| `docs/USER_GUIDE.md`                             | 用户使用说明                                             |

核心模型（均在 `words/models.py`）：

- **词库**：`Unit`、`Word`（`get_meanings()` 返回 list）
- **学习**：`StudyProgress`（掌握状态机 new/learning/mastered + 排期）、`StudyRecord`（每次作答明细，掌握状态的可信证据源）、`DailyCheckIn`、`DailyPick`（今日自选）、`PickRecord/PickRecordWord`（选词记录快照）
- **错误知识库**：`KnowledgeFeedback`、`WordFamily/WordFamilyMember/WordFormationPattern/WordRelation`（词族构词法，member 带 AI 中文释义）、`StudyProgress.risk_override`（手动风险档 -1/0/1/2）
- **考试/翻译**：`ExamPaper/ExamQuestion`、`TranslationSentence/TranslationPractice`（英语一长难句）、`Cet6TranslationPassage/Cet6TranslationPractice/GeneratedCet6Paper/GeneratedCet6Question`（六级汉译英，与英语一数据模型独立）
- **AI**：`AIModel`（密钥本机明文存储）、`AICallLog`、`UserSettings`（各功能模型选择）

## 四、红线约束（违反即事故，一定不能动）

1. 服务器只绑 `127.0.0.1`，禁止 `0.0.0.0`。
2. 必须启用 `CsrfViewMiddleware`；**所有改状态的接口 POST-only**，禁止 `@csrf_exempt`。
3. `DATA_UPLOAD_MAX_MEMORY_SIZE = 200MB`（备份以 JSON 请求体恢复，不得降回默认值）。
4. 备份恢复：`StudyRecord.created_at` 必须写回原始时间（缺时间戳的旧记录跳过并提示）；跨库映射一律用 word 字符串查目标库 Word.id，查不到就跳过；恢复时先 `PRAGMA foreign_keys = OFF`、完成后重新开启；恢复只增不改（自动补齐缺失单元/单词）。
5. 任何接口不得向前端回传 API 密钥。
6. 考试答题阶段判定结果保密，对错在成绩页统一公布；考试不得因未作答/答错把已掌握词降级（未作答只计卷面分；mastered 答错仅 error\_count+1、排期明天）。
7. 模拟考/AI 判定：释义判定以最后一次作答为准；交卷先等在途判定（≤25 秒），未判的 10 题一组 temperature=0.1 批量补判；评分与写库分离，全部判完统一写库。
8. AI 生成题目（组卷、六级翻译、词族）必须永久标注「AI 训练题/AI 生成」，禁止伪装真题；AI 组卷目标词只能由后端从本地词库选取；输出须过本地校验（数量、长度、目标词命中率、相似度）。
9. 长难句必须「先独立翻译、再查看解析」，解析默认折叠；六级汉译英保留完整段落语境，不拆独立句。
10. pywebview：`private_mode=False` + 固定 `storage_path`（exe 同目录 `data/webview2`）；桌面版外链一律 `webbrowser.open` 调系统浏览器。
11. 不得引入任何外网 CDN（Phosphor 图标等已本地化到 `words/static/vendor/`）。
12. 「分批复习/每批数量/继续下一批」功能已彻底移除，不得恢复。

## 五、协作规范（改完代码必须做）

1. **状态登记**：任何改动后在下方「状态登记」最上方追加一条（`#### 日期 ｜ 内容` + `` `类型` · 操作者 `` + 正文：改动文件/测试结果/是否提交打包/遗留事项）。
2. 同步把 `words/templates/settings.html`「关于」卡片 `stat-value` 的「最后更新」日期改为当天（硬编码 YYYY-MM-DD）。
3. 清理可再生文件：`product/build/`、根目录 `build/`、`dist/`、各级 `__pycache__/`、`*.pyc`；保留 `product/dist/word.exe`。
4. commit / push / 打包 / 发 Release 均需维护者明确指令，不擅自执行。
5. 多 Agent 协作：动工前先读状态登记最新一条 + 核对代码真实现状（文档可能滞后）；接口即契约，不擅自扩大范围；确需偏离先在登记区写明理由。

## 六、待 Trae 执行：2026-10-23 Bugbot 功能与逻辑修复

> 登记时间：2026-10-23 14:43:13（Asia/Shanghai）｜来源：Bugbot 审查｜状态：**仅审查，未改业务代码**

### P1-1：AI 不可用不能写成用户答错

> **用户已否决（2026-10-23）**：维持当前「本地先判 → 判不了 AI 仲裁 → AI 失败本地兜底判错」实现，理由是 **即时反馈优先于待复核体验**。用户反馈待复核流程"超级难用"，宁可牺牲零 AI 调用也不想答题无对错反馈。AI 失败时兜底结果在成绩页独立标记为 `local_fallback`（琥珀色"AI 暂不可用，判定偏严"），与精确匹配区分。待复核页面、PendingJudgment 模型和相关 API 仍保留，词性学习开放题仍在使用；语义释义判定不再自动产生 pending。

- **问题位置**：`words/views.py` 约 1765，语义释义判定的降级路径。
- **现象**：AI 断网、超时、熔断、未配置模型或返回非法内容时，本地规则未覆盖的同义正确答案会被最终判为 `wrong`；`api_meaning_check` 随后增加 `error_count`、写入错误记录并改变掌握状态。这会将服务故障误学习为用户错误，污染易错词推荐、风险词与后续错误预测。
- **修改建议**：
  1. 为判定结果增加明确的 `pending_review` / `unavailable` 非最终状态；AI 不可用时不得返回最终 `wrong`。
  2. 仅对 `is_final=True` 且 verdict 为 `wrong` 的结果调用写错逻辑；非最终结果不更新 `StudyProgress`、`StudyRecord`、错误计数或风险画像。
  3. 前端提示「AI 暂不可用，答案已进入待复核，不影响学习数据」，提供稍后补判入口；AI 恢复后再执行补判。
  4. 补判接口在 AI 仍不可用时必须保持待复核，禁止将旧待复核直接落为错误。
- **验收**：mock 超时、熔断、非法 JSON、无模型四种情况；输入本地规则未覆盖的正确同义答案后，断言无 `error_count` / 掌握状态 / 错误记录写入，且产生一条待复核记录。AI 恢复后补判正确答案，断言状态仍不被降级。

### P1-2：高效背诵穿插必须保证最大距离

> **用户已否决（2026-10-23）**：维持当前「真随机」实现（副本在剩余队列任意位置均匀随机抽取落点），理由是 **随机感优先于最小间距约束**。用户明确要求"改为真的随机穿插"，接受副本紧挨着出现或成堆的可能性。Bugbot 担心的副本扎堆问题通过 10 次重试去重缓解，但不强制最小间距。

- **问题位置**：`words/templates/learn_session.html` 约 1021，`scheduleInterleave`。
- **现象**：随机槽位可为 `0`，当前词完成后可能立刻再次出现；尾部可用槽位少于插入副本数时，重试后仍接收冲突槽位，多个副本可能连续出现。
- **修改建议**：
  1. 设定硬约束 `MIN_INTERLEAVE_GAP`（建议 3 张不同原词卡）；候选槽位从当前卡后的最小距离开始，绝不允许 0 或相邻插入。
  2. 插入前同时检查候选槽位前后：同一 `word_id`、同一词的已排副本、以及最近 N 张卡的原词；冲突则重采样。
  3. 在队列尾部空间不足时宁可少插 / 不插，并记录 `skipped_due_to_spacing`，禁止为了数量破坏间隔约束。
  4. 随机源可注入或可种子化，令单元测试稳定复现边界情况。
- **验收**：固定随机值 `0`、队列仅剩 1–2 个槽位、连续多词完成三类场景；断言同词之间至少隔 `MIN_INTERLEAVE_GAP` 张不同原词卡，不能满足时没有插入副本。

### P2-1：考试页必须如实展示本地兜底判定 ✅ 已完成

> **完成日期：2026-10-23**｜改动：`words/views.py` 5058 行 `source_stats` 字典加 `local_fallback: 0`，分支逻辑单独识别 `source='local_fallback'` 不再吞进 `local_exact`；`words/templates/exam.html` 成绩页统计区新增琥珀色"本地兜底 N 题（AI 暂不可用，判定偏严）"提示。验收：138 项测试全过。

- **问题位置**：`words/views.py` 约 5058，考试成绩判定结果组装。
- **现象**：`source='local_fallback'` 被计作 `local_exact`，返回给页面的 `local_fallback` 永远为 `0`；用户无法分辨低置信度兜底结果，也无法决定是否人工复核。
- **修改建议**：
  1. 统计时将 `local_exact` 与 `local_fallback` 分开计数；只将真正严格本地匹配归入 `local_exact`。
  2. 成绩页明确显示「本地兜底判定 X 题，建议复核」，并为每题保留判定来源与复核入口。
  3. 如该题的结果是非最终的 AI 不可用状态，按 P1-1 显示「待复核」，不能计入最终错题与学习画像。
- **验收**：构造含 `local_exact`、`local_fallback`、AI 三种来源的考试记录；断言 API 统计与页面展示分别正确，兜底题可进入复核而不会伪装为精确匹配。

### 实施顺序与回归范围

1. 先完成 P1-1 的结果状态契约与写库保护，再改考试与普通背诵调用方，避免一个入口遗漏。
2. 再完成 P1-2；这是纯前端队列逻辑，需覆盖边界队列，不要只凭手动体验验证。
3. 最后完成 P2-1 的统计与 UI 文案；复用 P1-1 的来源/待复核字段，禁止复制另一套状态。
4. 执行：`python manage.py test words.tests --settings=vocab_project.test_settings`、`python manage.py check`、`python manage.py makemigrations --check --dry-run`。未经维护者明确指令，不提交、不打包。

## 七、项目发展展望与执行方案（待维护者确认后执行）

> 登记时间：2026-10-23 19:45:15（Asia/Shanghai）｜状态：**规划已登记，禁止据此自动实施**

### 7.1 产品北极星

从「背词工具」升级为本机优先的个人英语知识库：系统能保存每次错误的上下文、解释用户为何容易错、预测下一次高风险点，并把词义、词性、词族、构词法、搭配、真题语境和输出练习组织成可调整的学习路径。AI 是增强与解释层，本地规则、用户手动纠正和可追溯学习记录才是最终依据。

### 7.2 执行原则（所有阶段共用）

1. **数据先于智能**：没有可追溯的答题事件、判定来源、置信度与人工修正，不上线预测或自动推荐。
2. **本地优先、AI 可降级**：基础复习、状态更新、风险推荐必须离线可用；AI 只处理语义消歧、讲解与内容生成。调用失败不得泄露密钥、阻塞学习或伪装成高置信结论。
3. **用户可控**：每个风险标记、关联关系和推荐都须展示原因；用户可一键纠正、关闭或设置 `risk_override`，人工决定优先级高于算法。
4. **渐进放量**：先记录与只读展示，再提示推荐，最后才允许影响队列；任何自动策略均需开关、回滚与版本标记。
5. **隐私边界**：默认仅本机 SQLite；向 AI 发送最少必要的题目与答案，严禁上传整库、备份文件或 API 密钥。

### 7.3 阶段路线与交付物

#### 阶段 A：可信学习事件层（预计 1–3 周）

- 建立统一的不可变「学习判定事件」：记录 `word_id`、练习场景、用户输入、最终判定、判定来源（local_exact/local_fallback/ai/manual）、置信度、模型/规则版本、时间与是否影响进度。
- 将所有会/不会、拼写、释义、词性、考试和待复核入口接入同一写库服务；状态更新必须幂等，前端多页面通过事件版本或广播同步。
- 增加错误异常检测：短时大量错误只弹出人工复核建议，不自动篡改历史或批量降级。
- **验收**：任一状态变化能追溯来源；重复提交不重复计数；备份恢复后事件、进度和解释可校验一致。

#### 阶段 B：词汇知识图谱与词性学习（预计 2–4 周）

- 扩充现有 `WordFamily`、`WordFamilyMember`、`WordFormationPattern`、`WordRelation`：同词不同词性、派生链、构词法、搭配与易混词均可建立双向关联，并标注来源（词典/AI/人工）与置信度。
- 学习页以「核心词 → 词性变化 → 构词法 → 对比练习」组织微课；AI 仅生成候选释义、例句和关系，需经本地校验或人工确认后入库。
- **验收**：如 `economy → economic → economical → economically` 能连续展示词性、词义差异与构词规律；用户可修改或删除任一关系，修改会立即覆盖推荐依据。

#### 阶段 C：可解释的本地风险模型（预计 2–3 周）

- 先实现规则/统计基线，不直接训练黑盒：综合遗忘间隔、近期错误率、错误类型、词族迁移错误、词性混淆、作答耗时和人工覆盖，输出 0–100 风险分与原因列表。
- 推荐队列只将高风险词前移，仍须遵守复习间隔和穿插最小距离；禁止为追求预测命中而连续推送同一词。
- 记录推荐曝光、用户是否接受、后续正确率，按周评估「风险命中率」「人工纠正率」「复习负担」；不达标则只展示建议、不影响排序。
- **验收**：每个推荐可解释为具体证据；关闭推荐或人工设定风险档后立即生效；离线时结果稳定可复现。

#### 阶段 D：AI 协作与个性化课程（预计 3–6 周）

- AI 读取最小化的脱敏事件摘要，为低置信语义题补判、解释错误、生成词性/构词法对比题和每日微课；失败时保留本地结果与待复核，不阻塞学习。
- 引入「预测—验证—校正」闭环：AI/本地模型先给出下一个可能错误及理由，实际作答后记录命中与否；用户的手动改判作为最高权重反馈。
- 模型健康与熔断保持独立于学习状态；连接恢复后批量补判必须限流、可暂停、可审计。
- **验收**：AI 不可用时所有核心背诵流程可完成；AI 建议能显示证据与来源；人工改判能在下一次推荐中反映。

### 7.4 上线顺序与决策门

1. 先完成当前 P1/P2 修复的维护者决策与回归，再开阶段 A；尤其要明确「AI 不可用时是待复核还是本地偏严兜底」的产品策略，不能让不同入口各自决定。
2. 阶段 A 只采集事件与展示诊断，不改变既有推荐排序；通过一周真实数据核验后，开启阶段 B。
3. 阶段 C 先灰度为「建议模式」至少两周；以人工纠正率、风险命中率和复习时长作为启用门槛。
4. 阶段 D 的 AI 功能必须可单独关闭；模型、提示词、规则变更都记录版本，保证可回放与回滚。

### 7.5 建议的优先级

- **立即规划、先不执行**：阶段 A 的事件字段、状态契约与迁移方案设计。
- **第二优先级**：阶段 B 的词性—构词法关系编辑与学习页展示。
- **第三优先级**：阶段 C 的规则基线与评估看板；未有稳定事件数据前不做训练模型。
- **最后启用**：阶段 D 的 AI 预测与自动课程生成。

## 八、自动更新与数据库兼容（2026-10-23 实施）

> 依据根目录 `开发文档.md` 执行阶段 B+C+D（阶段 E 打包与端到端测试未做，需维护者指令）。

### 8.1 改动清单

| 文件 | 内容 |
| --- | --- |
| `updater.py`（项目根，新增） | 纯标准库：版本解析/比较、标签校验、官方资产地址、流式下载（.part + 进度 + 取消）、SHA-256、状态文件、启动确认、独立更新器 `run_updater` |
| `words/views.py` | 新增 5 个接口（见 8.2）；下载任务用模块级 dict + Lock + Event 管理，单任务并发保护 |
| `words/urls.py` | 注册 5 条路由 |
| `product/launch.py` | 顶部拦截 `--apply-update`（在 `django.setup()` 与 webview 之前，只做文件操作）；新增 `install_update` WebView API；`safe_migrate()`（迁移前 SQLite 在线备份 + 迁移后完整性检查，失败中止启动）；启动就绪后 `confirm_boot()` 回写更新成功；清理 `data/update` 过期临时文件 |
| `product/word.spec` | `hiddenimports` 增加 `updater` |
| `words/templates/settings.html` | 更新弹窗新增进度条、「下载并安装」「取消下载」「仍要安装（未校验）」，轮询进度 |
| `words/templates/base.html` | 全站一次性提示上次更新结果（成功/已回滚/失败） |
| `words/tests.py` | `UpdaterCoreTests`（10 项）+ `UpdateApiTests`（5 项） |

### 8.2 接口契约（全部 POST-only，除状态查询）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/update/download/` | body `{"tag":"v3.4.6"}`；已有任务返回 409；标签非法 400 |
| GET | `/api/update/download/status/` | 返回 state/percent/path/sha_ok/desktop |
| POST | `/api/update/download/cancel/` | 置取消标志，下载线程删除 `.part` |
| GET | `/api/update/result/` | 上次安装结果（done/rolled_back/failed），一次会话只提示一次 |
| POST | `/api/update/result/ack/` | 确认已读 |

状态机：`idle → starting → downloading → verifying → ready | ready_unverified | cancelled | error`；安装阶段由更新器写 `waiting → replacing → installed → done | rolled_back | failed`。

### 8.3 更新器流程（复用当前 exe 隐藏模式）

1. 前端下载完成 → `pywebview.api.install_update(path, tag)` → 主程序把自身 exe 复制到 `data/update/updater_<pid>.exe` 并以 `--apply-update` 独立启动。
2. 前端调用 `exit_app()` 退出主进程。
3. 更新器按传入 PID 等待退出（轮询，不用固定 sleep）→ 备份旧 exe 到 `data/update/word.exe.bak` → `os.replace` 替换（20 次重试）→ 启动新版。
4. 新版服务就绪后写 `data/update/boot_ok.json`；更新器或新版自身把 `last_update.json` 升级为 `done`。**未收到启动确认前只显示 `installed`，不宣称更新成功。**
5. 替换/启动失败自动复制回滚备份并重启旧版，状态 `rolled_back`；回滚也失败则 `failed` 并保留备份。
6. 日志：`data/update/updater.log`。

### 8.4 发布要求（否则自动更新不可用）

1. 提升 `vocab_project/settings.py` 的 `APP_VERSION` 与 `APP_VERSION_NOTE`。
2. 按第六节命令打包，产物 `product/dist/word.exe`。
3. **必须同时上传 `word.exe` 与 `word.exe.sha256`**（`Get-FileHash -Algorithm SHA256`）。
4. Release 标签必须是 `vX.Y.Z`，正文写更新说明。
5. 现状：`v3.4.5` 的 Release **没有任何资产**，`v3.4.1` 有 `word.exe` 但无 `.sha256`。因此当前客户端点「下载并安装」会得到 404 提示；补齐资产后即可正常工作。

### 8.5 回滚步骤（人工）

1. 关闭程序。
2. 把 `data/update/word.exe.bak` 复制回程序目录并重命名为 `word.exe`（先备份当前损坏的 exe）。
3. 数据无需处理：`data/db.sqlite3` 从未被更新流程删除或覆盖。
4. 若迁移失败，启动会自动停在提示页，原库与 `data/backups/db_before_migrate_*.sqlite3` 均保留。

### 8.6 本次测试结果（真实执行）

- `python manage.py test words.tests --settings=vocab_project.test_settings` → **155/155 通过**。
- `python manage.py check` → 无问题；`makemigrations --check --dry-run` → 无变更。
- 真实网络：`v3.4.1` 的 `word.exe` 成功下载 **61,455,922 字节 / 6.8 秒**，SHA-256 计算与自校验通过；`.sha256` 接口返回 404（符合现状）。
- 隐藏模式实测：用 11MB/12MB 占位文件执行 `python product/launch.py --apply-update ...`，完整走通「等待 PID → 备份 → 替换 → 启动失败 → 自动复制回滚 → 写状态」链路，状态为 `rolled_back`、退出码 1、目标文件恢复为原大小，`updater.log` 记录完整。
- **未验证**：用真实新 EXE 走「启动成功 → boot_ok 回写 → done」分支（需真机换包，属阶段 E）。

### 8.7 剩余风险

1. 目标目录无写权限（如装在 `Program Files`）时替换会失败并保留备份，但当前没有前置写权限检测与引导。
2. Release 只发源码不发 EXE / `.sha256` 时，自动更新无法完成（已在 UI 明确提示）。
3. 新版启动后 25 秒内未回写确认，会停留在 `installed`，界面不显示「更新成功」（保守设计，下次启动会由新版自行确认）。
4. 杀毒软件可能拦截「复制自身 + 独立进程替换 exe」的行为，需用户放行。

## 九、状态登记

#### 2026-10-23 ｜ 自动更新与数据库兼容改造（阶段 B+C+D）

`代码` · Trae

正文：按根目录 `开发文档.md` 实施。新增 `updater.py`（版本比较、标签白名单、流式下载、SHA-256、状态文件、启动确认、独立更新器）；`views.py`/`urls.py` 新增 5 个更新接口（下载/状态/取消/结果/确认，改状态接口均 POST-only）；`launch.py` 增加 `--apply-update` 隐藏模式（在 django.setup 与 webview 之前拦截）、`install_update` WebView API、`safe_migrate`（有未应用迁移才备份，备份走 SQLite 在线 API + integrity_check，迁移后 quick_check，失败中止启动不重建库）、启动确认与临时文件清理；`word.spec` 增加 `updater` 隐藏导入；`settings.html` 增加进度条/下载/取消/未校验安装 UI；`base.html` 增加一次性更新结果提示。新增 16 项测试。结果：155/155 通过；check 与 makemigrations --check 无问题；真实下载 v3.4.1 资产 61,455,922 字节成功并校验通过；`--apply-update` 隐藏模式用占位文件走通「替换→启动失败→自动回滚」全链路。未打包、未提交、未 push；阶段 E（打包端到端）待维护者指令。已识别关键阻塞：**GitHub 上 v3.4.5 Release 无任何资产**，发布新版本时必须同时上传 `word.exe` 与 `word.exe.sha256`。

#### 2026-10-23 19:45:15 ｜ 项目发展展望与执行方案登记

`文档` · Codex

正文：新增阶段 A–D 的发展路线、交付物、验收标准、上线决策门、隐私与 AI 降级原则；明确本轮**仅写方案，未执行、未改业务代码、未运行测试、未打包、未提交**。Trae 看到请先确认 7.4 的产品决策门，再按阶段顺序拆分任务，禁止跳过事件层直接实现预测模型。

#### 2026-10-23 ｜ Bugbot 三项任务处理完成（P1-1/P1-2 用户否决，P2-1 已实施）

`代码` · Trae

正文：
- **P1-1 AI 不可用判错 → 维持当前实现**（已在上一轮会话改成本地兜底判错，理由是即时反馈优先于待复核体验）。待复核页面与模型保留，仅语义释义判定不再自动产生 pending。
- **P1-2 穿插最小间距 → 维持当前实现**（真随机副本，均匀抽取落点，10 次重试去重，不强制间距）。
- **P2-1 考试页拆分统计 → ✅ 已完成**：`source_stats` 加 `local_fallback` 键、分支单独计数；考试成绩页新增琥珀色"本地兜底 N 题（AI 暂不可用，判定偏严）"提示。
- 同步改 settings.html「最后更新」为 2026-10-23。
- 测试：`python manage.py test words.tests --settings=vocab_project.test_settings` → **138/138 通过**。`python manage.py check` 无问题。
- 未提交、未打包、未 push；无遗留事项。

#### 2026-10-23 14:43:13 ｜ Bugbot 功能与逻辑审查及修复交接

`文档` · Codex

正文：审查发现 2 项 P1、1 项 P2，已将位置、风险、修改建议、验收用例与实施顺序写入本文件；本次只更新交接文档，未改业务代码、未运行打包、未提交。**Trae 看到请按「六」执行，并在完成后补充改动文件与测试结果。**
