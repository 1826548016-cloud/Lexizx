# Lexizx 交接文档

> 面向接手开发的技术人员。更新日期：2026-10-24

## 1. 项目概览

Lexizx 是本机优先的 Windows 考研英语学习应用，使用 Django、SQLite、pywebview 和 PyInstaller。无需账号，学习数据保存在本机。主要功能包括词汇背诵与复习、真题模拟、英语一长难句翻译、六级汉译英、AI 组卷与学习助手。

## 2. 开发与验证

```powershell
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

运行测试和检查：

```powershell
python manage.py test words.tests --settings=vocab_project.test_settings
python manage.py check
python manage.py makemigrations --check --dry-run
```

打包命令：

```powershell
python -m PyInstaller --noconfirm --clean --distpath product/dist --workpath product/build product/word.spec
```

## 3. 代码地图

| 路径 | 内容 |
| --- | --- |
| `vocab_project/` | Django 配置、URL、WSGI 与测试设置 |
| `words/models.py` | 数据模型 |
| `words/views.py` | 页面/API 视图及主要业务逻辑；修改前先定位相关功能 |
| `words/ai_prompts.py`、`words/ai_exam_prompts.py` | AI 提示词 |
| `words/templates/`、`words/static/` | 页面模板和前端资源；静态资源引用使用版本戳 |
| `words/migrations/` | 数据库迁移 |
| `updater.py` | 自动更新逻辑 |
| `product/launch.py`、`product/word.spec` | 桌面入口与打包配置 |
| `docs/USER_GUIDE.md` | 用户指南 |

主要模型包括词库（`Unit`、`Word`）、学习记录（`StudyProgress`、`StudyRecord`）、错误知识关系（`KnowledgeFeedback`、`WordFamily*`、`WordRelation`）、考试与翻译、AI 配置及调用记录。完整字段和关系以 `words/models.py` 为准。

## 4. 必须遵守的约束

1. 本地服务只绑定 `127.0.0.1`，不得绑定 `0.0.0.0`；启用 CSRF，改变状态的接口使用 POST，不得添加 `@csrf_exempt`。
2. `DATA_UPLOAD_MAX_MEMORY_SIZE` 保持 200MB。备份恢复只增不改；按单词字符串映射目标库 ID，缺失记录跳过并提示；恢复学习记录时保留原始时间戳，旧记录无时间戳则跳过并提示；恢复流程正确关闭并重新启用 SQLite 外键检查。
3. 任何接口都不得向前端返回 API 密钥。
4. 考试过程中不得向用户泄露对错，结果在成绩页统一公布；未作答或答错不得降级已掌握词。考试判分遵守现有批量判定、等待在途任务及评分与写库分离流程。
5. AI 生成题目必须标注为训练题/AI 生成，不得伪装真题；组卷目标词由后端从本地词库选择，生成结果须经本地校验。
6. 长难句先独立翻译再展示解析，解析默认折叠；六级翻译保留完整段落语境。
7. pywebview 使用持久化模式和固定存储目录（exe 同目录 `data/webview2`）；桌面外链交由系统浏览器打开。
8. 不得引入外网 CDN；图标及其他静态依赖使用本地资源。「分批复习/每批数量/继续下一批」已移除，不得恢复。

## 5. 自动更新与发布

`updater.py` 负责版本检查、下载、SHA-256 校验、取消及状态管理；`product/launch.py` 负责应用更新与迁移前 SQLite 在线备份。更新接口包括：

| 方法 | 路径 |
| --- | --- |
| POST | `/api/update/download/` |
| GET | `/api/update/download/status/` |
| POST | `/api/update/download/cancel/` |
| GET | `/api/update/result/` |
| POST | `/api/update/result/ack/` |

发布前更新 `APP_VERSION` 和 `APP_VERSION_NOTE`；Release 标签使用 `vX.Y.Z`，并上传 `word.exe` 与对应的 `word.exe.sha256`。未经维护者明确指令，不得打包、提交、推送或发布。

## 6. 开发规范

- 改动完成后，将 `words/templates/settings.html`「关于」卡片中的最后更新日期改为当天。
- 清理本次改动产生的可再生构建产物和 Python 缓存；保留 `product/dist/word.exe`。
- 开工前检查代码现状及本文件变更记录，避免依赖过时文档；接口和数据格式视为契约。
- 完成后在变更记录追加日期、摘要、测试结果及是否提交/打包；保留最近 5–10 条。

## 7. 前端改版（已终止）

2026-10-25 起，前端改版整体撤销：设计样品目录（`design-prototypes/`）、首页 Study Index 改造（`words/templates/dashboard.html` + `words/static/css/study-index-dashboard.css`）、改版前模板快照（`docs/ui-legacy/`）及 `PRODUCT.md` 均已删除，`words/templates/` 与 `words/static/css/style.css` 还原至改版前状态，现有界面保持原样。

除非维护者明确提出新方向，不得再自行启动视觉改版、样稿制作或页面重排；新增功能一律在现有视觉体系内实现，不新增设计产物目录。

## 8. 变更记录

#### 2026-10-26 ｜ 本机密钥保险箱与依赖安全维护

安全审查确认本程序的安全边界是单机单用户：Django 服务只能监听 `127.0.0.1`，不得改为局域网或公网监听。AI 模型密钥现在使用 Windows 当前用户的 DPAPI 加密后写入 SQLite；密钥明文读取接口已经移除，旧版明文密钥在应用读取或打开模型列表时迁移为密文。桌面 Cookie 使用 `HttpOnly`、`SameSite=Strict`，保持 HTTP 回环访问兼容，不能启用 `Secure` 标志（本机服务没有 TLS）。Django `SECRET_KEY` 改为本机首次启动生成或由 `DJANGO_SECRET_KEY` 注入，密钥文件不纳入版本控制。依赖升级至 Django 5.2.18。AI 上游错误文本在写入页面前统一转义。已应用 `words.0060`，`manage.py check`、迁移检查和密钥保险箱定向测试通过；完整测试运行 156 项，其中 8 项更新器测试受沙箱临时目录权限限制而无法创建测试文件。未提交、未打包。

#### 2026-10-25 ｜ 撤销前端改版并清理相关产物

按维护者要求整体撤销前端改版：删除 `design-prototypes/`（含 01–05 样稿与启动页演示）、`docs/ui-legacy/`（改版前模板快照）、`PRODUCT.md` 与新增的 `words/static/css/study-index-dashboard.css`；`words/templates/` 全部模板与 `words/static/css/style.css` 经 `git restore` 还原至改版前状态，首页回到原今日任务卡 / 统计 / 计划 / 热力图布局，同时清除了 30 余个文件仅因行尾（CRLF）产生的改动噪声。交接文档第 7 节改为「前端改版（已终止）」并记述撤销范围，未改动任何后端逻辑、接口与数据格式。Django 系统检查、迁移检查与 155 项测试通过，首页渲染验证正常。未提交、未打包。

#### 2026-10-24 ｜ 精简并统一交接文档

删除了互相冲突的历史方案正文、重复规则和失效执行指令，保留项目开发、安全、发布规范与功能约定；启动页样品明确为独立演示，尚未接入生产应用。此次仅整理文档，未修改业务代码、未运行测试、未提交或打包。

#### 2026-10-23 ｜ 应用内自动更新

新增自动更新流程及相关接口，并在安装更新前进行 SQLite 在线备份；发布时需同时提供 exe 与 SHA-256 文件。未打包、未提交。

#### 2026-10-23 ｜ 考试本地兜底判定提示

成绩页区分统计本地兜底判定，并提示用户其判定可能偏严；当时记录的测试结果为 138/138。

#### 2026-10-13 ｜ 错误知识库与待复核

记录了错误知识库、词族变式和待复核功能及相关排序规则；具体实现以当前代码为准。
