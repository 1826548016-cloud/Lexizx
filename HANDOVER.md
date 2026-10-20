# Lexizx 交接文档

> 面向接手开发的技术人员与 Agent。读完本文件（约 5 分钟）即可动工。
> 更新日期：2026-10-20｜仓库：https://github.com/1826548016-cloud/Lexizx

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
python manage.py test words.tests --settings=vocab_project.test_settings   # 当前 138 项通过
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
| `words/migrations/` | 迁移，当前最新 `0059_pendingjudgment_dedupe_key_and_more` |
| `product/launch.py`、`product/word.spec` | 桌面版入口与 PyInstaller 配置 |
| `docs/USER_GUIDE.md` | 用户使用说明 |

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
