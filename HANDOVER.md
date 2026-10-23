# Lexizx 交接文档

> 面向接手开发的技术人员。读完本文件（约 5 分钟）即可动工。
> 更新日期：2026-10-23｜仓库：<https://github.com/1826548016-cloud/Lexizx>

## 一、项目概览

考研英语学习 Windows 桌面应用：Django + SQLite + pywebview，PyInstaller 打包为单文件 exe。单机单用户、无需注册，学习数据全部保存在本机。

主要功能：词汇背诵与复习（掌握状态机 + 间隔复习）、真题模拟考、英语一长难句翻译、六级汉译英、AI 组卷与学习助手。AI 功能由用户自行配置 OpenAI 兼容接口（内置 18 家服务商预设）。

## 二、快速开始

```powershell
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver          # http://127.0.0.1:8000/
```

测试（内存数据库，不碰开发库）：

```powershell
python manage.py test words.tests --settings=vocab_project.test_settings
python manage.py check
python manage.py makemigrations --check --dry-run
```

打包（产物 `product/dist/word.exe`，中间文件 `product/build/`）：

```powershell
python -m PyInstaller --noconfirm --clean --distpath product/dist --workpath product/build product/word.spec
```

开发提示：使用 `runserver --noreload` 时，修改 Python 或模板后必须重启服务才生效。

## 三、架构地图

| 位置 | 内容 |
| --- | --- |
| `vocab_project/` | Django 配置（settings.py、urls、wsgi）、test_settings.py |
| `words/models.py` | 全部数据模型（单文件） |
| `words/views.py` | 页面/API 视图与大部分业务逻辑（文件很大，先定位专题再改，勿无关重构） |
| `words/ai_prompts.py`、`words/ai_exam_prompts.py` | AI 提示词 |
| `words/templates/`、`words/static/` | 模板与前端资源（静态引用带 `?v=` 版本戳防缓存） |
| `words/migrations/` | 迁移，当前最新 `0059_pendingjudgment_dedupe_key_and_more` |
| `updater.py` | 自动更新核心逻辑（纯标准库） |
| `product/launch.py`、`product/word.spec` | 桌面版入口与 PyInstaller 配置 |
| `docs/USER_GUIDE.md` | 用户使用说明 |

核心模型（均在 `words/models.py`）：

- **词库**：`Unit`、`Word`（`get_meanings()` 返回 list）
- **学习**：`StudyProgress`（掌握状态机 new/learning/mastered + 排期）、`StudyRecord`（每次作答明细，掌握状态的可信证据源）、`DailyCheckIn`、`DailyPick`、`PickRecord/PickRecordWord`
- **错误知识库**：`KnowledgeFeedback`、`WordFamily/WordFamilyMember/WordFormationPattern/WordRelation`、`StudyProgress.risk_override`（手动风险档 -1/0/1/2）
- **考试/翻译**：`ExamPaper/ExamQuestion`、`TranslationSentence/TranslationPractice`（英语一）、`Cet6TranslationPassage/Cet6TranslationPractice/GeneratedCet6Paper/GeneratedCet6Question`（六级，与英语一独立）
- **AI**：`AIModel`（密钥本机明文存储）、`AICallLog`、`UserSettings`（各功能模型选择）

## 四、关键约束（违反即事故）

1. 服务器只绑 `127.0.0.1`，禁止 `0.0.0.0`。
2. 必须启用 `CsrfViewMiddleware`；所有改状态的接口 POST-only，禁止 `@csrf_exempt`。
3. `DATA_UPLOAD_MAX_MEMORY_SIZE = 200MB`（备份以 JSON 请求体恢复，不得降回默认值）。
4. 备份恢复：`StudyRecord.created_at` 必须写回原始时间（缺时间戳的旧记录跳过并提示）；跨库映射用 word 字符串查目标库 `Word.id`，查不到就跳过；恢复前 `PRAGMA foreign_keys = OFF`、完成后重新开启；只增不改，自动补齐缺失单元/单词。
5. 任何接口不得向前端回传 API 密钥。
6. 考试答题阶段判定结果保密，对错在成绩页统一公布；考试不得因未作答/答错把已掌握词降级（未作答只计卷面分；mastered 答错仅 `error_count+1`、排期明天）。
7. 释义判定以最后一次作答为准；交卷先等在途判定（≤25 秒），未判题目 10 题一组、`temperature=0.1` 批量补判；评分与写库分离，全部判完统一写库。
8. AI 生成题目（组卷、六级翻译、词族）必须永久标注「AI 训练题/AI 生成」，禁止伪装真题；组卷目标词只能由后端从本地词库选取；输出须过本地校验（数量、长度、目标词命中率、相似度）。
9. 长难句必须「先独立翻译、再查看解析」，解析默认折叠；六级汉译英保留完整段落语境，不拆独立句。
10. pywebview：`private_mode=False` + 固定 `storage_path`（exe 同目录 `data/webview2`）；桌面版外链一律 `webbrowser.open` 调系统浏览器。
11. 不得引入任何外网 CDN（Phosphor 图标等已本地化到 `words/static/vendor/`）。
12. 「分批复习/每批数量/继续下一批」已彻底移除，不得恢复。

## 五、自动更新与数据库兼容

`updater.py` 负责版本比较、标签校验、流式下载（`.part` + 进度 + 取消）、SHA-256 校验、状态文件与独立更新器；`product/launch.py` 提供 `--apply-update` 隐藏模式、`install_update` API 与迁移前 SQLite 在线备份。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/update/download/` | body `{"tag":"v3.4.6"}`；已有任务 409，标签非法 400 |
| GET | `/api/update/download/status/` | state / percent / path / sha_ok / desktop |
| POST | `/api/update/download/cancel/` | 置取消标志并删除 `.part` |
| GET | `/api/update/result/` | 上次安装结果（done/rolled_back/failed），每会话提示一次 |
| POST | `/api/update/result/ack/` | 确认已读 |

流程：下载并校验 → 复制自身为独立更新器 → 主程序退出 → 更新器等待 PID、备份旧 exe、`os.replace` 替换并启动新版 → 新版启动就绪写 `boot_ok.json` 后状态升级为 `done`；失败自动回滚，回滚失败保留 `data/update/word.exe.bak`。日志位于 `data/update/updater.log`。

发布要求（否则自动更新不可用）：

1. 提升 `vocab_project/settings.py` 中的 `APP_VERSION` 与 `APP_VERSION_NOTE`。
2. 打包后同时上传 `word.exe` 与 `word.exe.sha256`（`Get-FileHash -Algorithm SHA256`）。
3. Release 标签必须是 `vX.Y.Z`，正文写明更新说明。

## 六、开发规范

1. 改动完成后，同步把 `words/templates/settings.html`「关于」卡片 `stat-value` 的「最后更新」日期改为当天（硬编码 YYYY-MM-DD）。
2. 清理可再生文件：`product/build/`、根目录 `build/`、`dist/`、各级 `__pycache__/`、`*.pyc`；保留 `product/dist/word.exe`。
3. commit / push / 打包 / 发 Release 均需维护者明确指令，不擅自执行。
4. 多 Agent 协作：动工前先读本文件末尾变更记录与代码真实现状（文档可能滞后）；接口即契约，不擅自扩大范围。
5. 任何改动完成后在末尾「变更记录」追加一条（日期、改动摘要、测试结果、是否提交打包），保留最近 5–10 条。

## 七、变更记录

#### 2026-10-23 ｜ 应用内自动更新与迁移前备份

新增 `updater.py` 与 5 个更新接口（改状态接口均 POST-only）；`launch.py` 增加 `--apply-update` 隐藏模式、`install_update` API、`safe_migrate`（有未应用迁移才备份，备份走 SQLite 在线 API）；`settings.html` 增加下载进度 UI，`base.html` 增加一次性更新结果提示。新增 16 项测试，155/155 通过。未打包、未提交。注意：发布新版本时必须同时上传 `word.exe` 与 `word.exe.sha256`。

#### 2026-10-23 ｜ 考试页拆分本地兜底判定统计

`source_stats` 单独统计 `local_fallback`，成绩页新增琥珀色「本地兜底 N 题（AI 暂不可用，判定偏严）」提示。138/138 测试通过。

#### 2026-10-13 ｜ 错误知识库、词族与待复核

错误学习引擎改为按真实错误事件时间衰减（半衰期 21 天），复习排序为「人工重点 > 手动风险档 > 自动风险分 > 到期紧迫度」；新增词族变式与待复核页面，所有接口 POST-only，备份格式双向兼容旧版。
