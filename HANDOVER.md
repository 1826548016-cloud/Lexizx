# Lexizx 交接文档

> 面向接手开发的技术人员与 Agent。读完本文件（约 5 分钟）即可动工。
> 更新日期：2026-10-18 ｜ 仓库：https://github.com/1826548016-cloud/Lexizx

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
python manage.py test words.tests --settings=vocab_project.test_settings   # 当前 109 项通过
python manage.py check
python manage.py makemigrations --check --dry-run
```

打包（产物 `product/dist/word.exe`，中间文件 `product/build/`）：

```powershell
python -m PyInstaller --noconfirm --clean --distpath product/dist --workpath product/build product/word.spec
```

注意：本机开发用 `--noreload` 时，改完 Python/模板必须重启服务器才生效。

## 三、架构地图

| 位置 | 内容 |
| --- | --- |
| `vocab_project/` | Django 配置（settings.py、urls、wsgi）、test_settings.py |
| `words/models.py` | 全部数据模型（单文件） |
| `words/views.py` | 页面/API 视图与大部分业务逻辑（文件很大，先定位专题再改，勿无关重构） |
| `words/ai_prompts.py`、`words/ai_exam_prompts.py` | AI 提示词 |
| `words/templates/`、`words/static/` | 模板与前端资源（静态引用带 `?v=` 版本戳防缓存） |
| `words/migrations/` | 迁移，当前最新 `0057_wordfamilymember_meaning` |
| `product/launch.py`、`product/word.spec` | 桌面版入口与 PyInstaller 配置 |
| `docs/USER_GUIDE.md` | 用户使用说明 |

核心模型（均在 `words/models.py`）：

- **词库**：`Unit`、`Word`（`get_meanings()` 返回 list）
- **学习**：`StudyProgress`（掌握状态机 new/learning/mastered + 排期）、`StudyRecord`（每次作答明细，掌握状态的可信证据源）、`DailyCheckIn`、`DailyPick`（今日自选）、`PickRecord/PickRecordWord`（选词记录快照）
- **错误知识库**：`KnowledgeFeedback`、`WordFamily/WordFamilyMember/WordFormationPattern/WordRelation`（词族构词法，member 带 AI 中文释义）、`StudyProgress.risk_override`（手动风险档 -1/0/1/2）
- **考试/翻译**：`ExamPaper/ExamQuestion`、`TranslationSentence/TranslationPractice`（英语一长难句）、`Cet6TranslationPassage/Cet6TranslationPractice/GeneratedCet6Paper/GeneratedCet6Question`（六级汉译英，与英语一数据模型独立）
- **AI**：`AIModel`（密钥本机明文存储）、`AICallLog`、`UserSettings`（各功能模型选择）

## 四、红线约束（违反即事故）

1. 服务器只绑 `127.0.0.1`，禁止 `0.0.0.0`。
2. 必须启用 `CsrfViewMiddleware`；**所有改状态的接口 POST-only**，禁止 `@csrf_exempt`。
3. `DATA_UPLOAD_MAX_MEMORY_SIZE = 200MB`（备份以 JSON 请求体恢复，不得降回默认值）。
4. 备份恢复：`StudyRecord.created_at` 必须写回原始时间（缺时间戳的旧记录跳过并提示）；跨库映射一律用 word 字符串查目标库 Word.id，查不到就跳过；恢复时先 `PRAGMA foreign_keys = OFF`、完成后重新开启；恢复只增不改（自动补齐缺失单元/单词）。
5. 任何接口不得向前端回传 API 密钥。
6. 考试答题阶段判定结果保密，对错在成绩页统一公布；考试不得因未作答/答错把已掌握词降级（未作答只计卷面分；mastered 答错仅 error_count+1、排期明天）。
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

## 六、当前状态（2026-10-18）

- **最新提交**：`a354402`「优化了一些有用的功能」（已推送 origin/main；工作区干净）。
- **最新迁移**：`0057_wordfamilymember_meaning`（0053~0057 需 `migrate` 后生效）。
- **最新产物**：`product/dist/word.exe`（2026-10-18 18:12、61,561,178 字节），含全部最新修复；**GitHub Release 上的 v3.4.1 是旧包**（有备份导入 2.5MB 上限 + 恢复时间戳丢失两个问题），待维护者确认版本号后发布新版替换。
- **测试**：109 项全部通过。
- **待开发**：个人「下一错误」预测器与词性能力升级（方案曾在旧版交接文档第十三节，需要时从 git 历史 `2707e8d^` 之前的 HANDOVER.md 取回原文）。
- **功能专题状态**：错误知识库/风险评分 ✅、AI 词性与词族教学 ✅、今日自选（AI 扩展词性、选词记录快照）✅、复习自选 ✅、去批次化 ✅、词族变式展示（背诵页+详情页，生成即入库）✅、备份恢复时间戳与体积上限 ✅。

## 七、状态登记

> 新记录追加在**本区最上方**（最新在前）。格式见第五节。保留最近约 10 条，更早的并入归档摘要。

#### 2026-10-18 ｜ 交接文档重构为快速上手版 + 提交推送

`文档` · Agent

按用户要求将 824 行的交接文档重构为面向技术人员/Agent 的快速上手版（概况/快速开始/架构地图/红线约束/协作规范/当前状态），历史方案细节收敛（可从 git 历史取回）；README 新增「关于本项目」（前期手搓框架、后期 AI 工具协作、欢迎二创）；HANDOVER.md 移出 .gitignore 随仓库公开。提交 `a354402` 已推送。数据库 `db.sqlite3`、`backups/`、`media/` 保持 gitignore 不上传。

#### 2026-10-18 ｜ 修复「备份无法导入」：请求体超 Django 默认 2.5MB 上限

`修改 + 打包` · Agent

根因：前端以 JSON 请求体提交整份备份到 `/api/restore/`，Django 默认 `DATA_UPLOAD_MAX_MEMORY_SIZE`=2.5MB，当前备份已达 2.96MB（1941 词/7113 明细），超限抛 `RequestDataTooBig` 导致恢复 400。修复：`settings.py` 设 `DATA_UPLOAD_MAX_MEMORY_SIZE = 200MB`；新增 `RestoreUploadLimitTests` 回归（3MB 请求体断言 200）。全量 109 项测试通过。打包 `word.exe`（18:12、61,561,178 字节）。旧格式（v1.0/v2.0）备份兼容行为不变（缺 `words` 词库本体时按原提示跳过）。

#### 2026-10-18 ｜ 排查「空库恢复旧备份后今日背诵显示全部历史单词」

`排查 + 测试` · Agent

结论：当前代码已修复，无需改产品代码。根因：v3.4.0/v3.4.1 恢复备份时 `StudyRecord` 不写回 `created_at`（全部记为恢复当天），「今日已背」集合（`_today_practiced_word_ids`）按该时间取词导致历史词误计。修复于 10-16 `e7a96c7` 进入代码（逐条写回原始时间，缺时间戳跳过并提示）。真实备份空库往返验证：今日新学 0、复习 3、明细 4（均为本就属于今天的数据）；日期回退 4 天模拟旧备份后今日三项均为 0。新增 `RestoreBackupTimestampTests` 2 条回归。注意：从已被旧版污染过的库导出的备份时间已定型，无法还原。

#### 2026-10-18 ｜ 当日开发汇总（Bugbot 修复 + 快捷键/自选状态 + 词族变式）

`修改` · Agent

1. 第十九节 Bugbot P1/P2 全完成：词性答错接入错误知识库（`_apply_wrong_progress`，与考试错题同口径）；`pos_learning_model`、`PickRecord`、`KnowledgeFeedback` 稳定引用纳入备份；词族候选 key 规范化 + 并发冲突转可读响应；载入选词记录前先 rollover；复习自选排序改 `unit__number + list_number`。
2. 默写模式 ← → 翻词修复（箭头键处理提到输入框焦点守卫之前）；今日自选开始背诵可选状态（全部/不会/已掌握，`scope=daily` 时 `state` 过滤）。
3. 「词性」按钮恒可点击；背诵页/详情页新增词族变式展示（GET `api_word_family_get`）。
4. 词族变式带汉语释义 + 生成即入库：`WordFamilyMember.meaning`（迁移 0057），AI 输出中文释义并落库，生成时 `confirm=1` 直接当正式数据（不再走人工确认队列）。

验证：106 项测试通过；浏览器实测 ← → 翻词与词族变式（含释义）正常。迁移 0057 已应用开发库。

### 归档：更早的状态登记（摘要）

| 日期 | 内容 | 结论 |
| --- | --- | --- |
| 10-17 | 第十四～十八节打包 | `word.exe`（10-17 13:40），已被 10-18 产物取代 |
| 10-17 | 第十八节 选词记录快照 CRUD | 完成（90 项测试），迁移 0056 |
| 10-17 | 审查第十四～十七节 + 修回归 | 完成（83 项测试） |
| 10-17 | 第十七节 今日自选独立一级入口 | 完成 |
| 10-17 | 第十六节 复习自选（升级今日已背页） | 完成 |
| 10-17 | 第十五节 彻底去批次化 | 完成，含 test_settings 修复 |
| 10-17 | 第十四节 今日自选 AI 扩展词性 | 完成 |
| 10-17 | 第十二节 AI 词性/词族/构词法教学 | 完成，迁移 0055 |
| 10-16 | 错误知识库第 1+2 期（风险引擎/手动覆盖） | 完成，迁移 0053/0054 |
| 10-16 | 第十三节「下一错误」预测器方案 | **待开发** |
| 10-15 | 恢复备份保留 `StudyRecord.created_at` | 完成（35 项测试） |
| 10-15 | 打包产物移入 `product/` | 完成 |
| 10-13 | 删除 API 密钥加密，本机明文存储 | 完成 |
| 10-12 | 统一今日计划与复习计划规则 | 完成 |
| 10-12 | 今日已背重复练习 | 完成 |

<!-- 新记录请插到「七、状态登记」区最上方；更早记录并入上表 -->
