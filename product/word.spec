# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 Django 项目打成单 exe（原生窗口版，无控制台、无浏览器）。
用法：在项目根目录执行
    cd d:\\word\\001
    pyinstaller word.spec
产物：dist\\word\\word.exe
"""
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# spec 文件位于 001/，项目根在上一层
PROJECT_ROOT = str(Path(SPECPATH).parent)

# 关键：把项目根插入 sys.path 顶部，让 collect_submodules/collect_data_files
# 能把 words / vocab_project 识别为可导入的包（否则会 "not a package" 警告）
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import words            # noqa: E402  触发包注册
import vocab_project    # noqa: E402

# ===== 数据文件（非 .py 资源）=====
datas = []
datas += collect_data_files('words')           # words/templates/、words/static/
datas += collect_data_files('vocab_project')   # vocab_project/templates/ 等
datas += collect_data_files('webview')         # webview 需要自己的 web 资源
# 词汇本词库 JSON（首启自动导入）——只打包词库，不打 db.sqlite3 等用户数据
datas += [(str(Path(PROJECT_ROOT) / 'data' / 'hongbaoshu.json'), 'data')]

# ===== 隐藏导入（Django 动态加载的模块）=====
hiddenimports = []
hiddenimports += collect_submodules('words')
hiddenimports += collect_submodules('vocab_project')
hiddenimports += collect_submodules('django.contrib')
hiddenimports += collect_submodules('webview')           # webview 的动态子模块
hiddenimports += ['vocab_project.settings', 'vocab_project.urls', 'vocab_project.wsgi']
# words 包关键模块兜底（Django 通过字符串动态 import，必须显式声明）
hiddenimports += [
    'words', 'words.apps', 'words.urls', 'words.views', 'words.models',
    'words.ai_exam_prompts', 'words.admin',
]
# management commands（首启 launch.py 通过 call_command 调用，必须显式声明）
hiddenimports += [
    'words.management.commands.generate_examples',
    'words.management.commands.import_cet6_translations',
    'words.management.commands.import_exam_questions',
    'words.management.commands.import_text',
    'words.management.commands.import_words',
    'words.management.commands.optimize_pos_meanings',
]
# reportlab 字体/子模块
hiddenimports += collect_submodules('reportlab')

a = Analysis(
    [str(Path(SPECPATH) / 'launch.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['pytest', 'tkinter', 'unittest', 'test', 'pydoc', 'IPython'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='word',
    console=False,            # 无黑框控制台，完全桌面软件体验
    disable_windowed_traceback=False,
    onefile=True,             # 单 exe
    icon=str(Path(SPECPATH).parent / 'ioc' / '图标.ico'),
    runtime_tmpdir=None,
)
