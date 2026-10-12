# Lexizx 项目交接

更新日期：2026-10-12

## 项目概况

Lexizx 是一个基于 Django 的考研英语学习应用，提供词汇学习与复习、真题模拟、英语一长难句翻译、六级汉译英、AI 组卷和 AI 学习助手。桌面版使用 pywebview 和 PyInstaller 打包为 Windows 单文件程序。

## 结构速览

- `vocab_project/`：Django 项目配置、URL 和 WSGI 入口。
- `words/models.py`：词汇、学习记录、真题、翻译练习、AI 模型和调用日志等数据模型。
- `words/views.py`：页面/API 视图及大部分业务逻辑；文件较大，改动时先定位对应专题，避免无关重构。
- `words/ai_prompts.py`、`words/ai_exam_prompts.py`：AI 提示词。
- `words/templates/`：Django 页面模板。
- `words/static/`：前端 CSS、JavaScript 和离线资源。
- `words/migrations/`：数据库迁移，当前最新迁移为 `0052_aicalllog_cet6_practice_and_more.py`。
- `product/launch.py`、`product/word.spec`：桌面版启动入口和 PyInstaller 配置。
- `data/`：词库 JSON、界面截图等随项目使用的资源；桌面版用户数据保存在 exe 同目录的 `data/`。

## 本地开发与打包

开发环境：

```powershell
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

在项目根目录打包 Windows 单文件程序：

```powershell
pyinstaller --noconfirm --clean product/word.spec
```

产物为 `dist/word.exe`。`product/word.spec` 文件顶部的旧用法注释仍提到 `001` 子目录，实际应从项目根目录运行上述命令。

## 最近提交

- `2b8c25c`：仓库更名为 Lexizx，同步站内更新检查接口与下载链接。
- `c74674e`：增加英语一长难句翻译、六级翻译和 AI 组卷相关功能。
- `d44975c`：整体设计和前端交互优化。
- `d522f1d`：调整 AI 提示词及相关功能。
- `125cf4e`：修复缓存问题。

## 当前工作区改动

截至本交接文档更新时，工作区有尚未提交的代码与打包产物改动。不要用 reset、checkout 或清理命令覆盖它们；先看 `git status` 和 diff，确认归属后再处理。

近期代码改动主要包括：

- 翻译批改返回值校验，拒绝缺失评分、非法分数和不合法标签。
- AI 调用日志关联长难句与六级练习；AI 组卷记录保存实际模型名称。迁移 `0052` 已在当前开发数据库应用。
- 英语一与六级组卷列表提供失败记录删除，已有关联练习的题目会阻止删除。
- 学习计时在页面隐藏时暂停；模拟考试支持有效时间累计和刷新恢复。
- 高效背诵进度按原始单词数计算，不因穿插重现副本改变分母。
- 翻译页和组卷页补充加载/结果的辅助技术播报。

当前状态还包含大量 `build/word/` 生成文件改动，以及 `product/dist.zip` 删除记录。这些改动需要由项目维护者判断是否纳入提交；不要擅自恢复或清理。重新打包后的 `dist/word.exe` 位于 `dist/`。

## 已知待办与注意事项

- 根目录 `README.md` 与 `product/README.md` 均已统一为从 GitHub Releases 下载 `word.exe`。
- `words/tests.py` 已覆盖学习计时、高效背诵进度分母、AI 翻译批改 JSON 校验，以及英语一/六级组卷删除保护。安全运行命令：`python manage.py test words.tests --settings=vocab_project.test_settings`；该配置将测试数据库放在内存中，不触碰开发库。
- 检查新数据库首次启动路径，确保会应用全部迁移；不要把开发用 `db.sqlite3` 或个人 `data/` 打进公开包。
- `build/` 属于 PyInstaller 可再生文件；目前这些文件已被 Git 跟踪，因此改动会出现在工作区。是否改为忽略构建缓存应单独评估，避免直接删除已跟踪文件。
- 打包输出目录是根目录的 `dist/`；不要与 `product/README.md` 中的旧 `dist.zip` 说明混淆。

## 最近验证情况

在前述代码改动后，`python manage.py check` 和 `python manage.py makemigrations --check --dry-run` 均通过；PyInstaller 重新打包成功，产物为 `dist/word.exe`。尚未做完整桌面端手动验收。

本次补充回归测试后，内存测试库下 `python manage.py test words.tests --settings=vocab_project.test_settings` 共 12 项通过；未对开发数据库运行迁移或写入。
