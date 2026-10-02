"""打包后的启动入口（原生窗口版）。

打包后双击 exe 运行流程：
1. 切换工作目录到 exe 同目录（让 ./data/ 落地）
2. 所有输出同时写入 data/launch.log（无控制台，排障用）
3. 弹出「正在初始化」加载窗口（Edge WebView2）
4. 首次启动：自动 migrate + 导入真题与词库；非首次仅补 migrate
5. 后台线程启动 Django runserver（固定端口 18765，被占则顺延）
6. 服务就绪后窗口加载主页面；关闭窗口即退出程序

开发模式（未打包）保持原行为：启动 runserver 并打开默认浏览器。
也可以直接 `python launch.py` 跑。
"""
import os
import sys
import json
import time
import socket
import threading
import webbrowser
import urllib.parse
from pathlib import Path

FROZEN = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')

if FROZEN:
    # PyInstaller 打包后，_MEIPASS 是解压的只读资源目录
    BASE_DIR = Path(sys._MEIPASS)
    # 切换工作目录到 exe 同目录（便于 ./data/ 落地）
    os.chdir(Path(sys.executable).resolve().parent)
else:
    # 开发模式：launch.py 在 001/ 下，项目根在上一层
    BASE_DIR = Path(__file__).resolve().parent.parent
    os.chdir(BASE_DIR)

# 让 Python 能找到项目模块
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vocab_project.settings')

# ===== 打包模式下无控制台：把 stdout/stderr 同时写入 data/launch.log =====
if FROZEN:
    _log_dir = Path(sys.executable).resolve().parent / 'data'
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = open(_log_dir / 'launch.log', 'a', encoding='utf-8', buffering=1)

    class _Tee:
        """print 既写日志文件（控制台已隐藏，只留文件）"""
        def write(self, s):
            try:
                _log_file.write(s)
            except Exception:
                pass
        def flush(self):
            try:
                _log_file.flush()
            except Exception:
                pass

    sys.stdout = _Tee()
    sys.stderr = _Tee()

import django
django.setup()

from django.core.management import call_command
from django.conf import settings

DATA_DIR = Path(settings.DATA_DIR)
DB_PATH = DATA_DIR / 'db.sqlite3'

APP_TITLE = '考研英语学习平台'
PREFERRED_PORT = 18765  # 固定端口：保证 localStorage / IndexedDB（壁纸等）跨启动保留


def find_port(preferred=PREFERRED_PORT, tries=20):
    """从首选端口开始找空闲端口（首选固定，尽量不变以保住浏览器端存储）"""
    for i in range(tries):
        port = preferred + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError('找不到可用端口')


def wait_server_ready(port, timeout=60):
    """轮询直到 Django 能响应"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def first_run_init(progress=None):
    """首次启动自动初始化数据（不含 API key，用户自行配置）

    progress: 可选回调，progress(msg) 用于向 GUI 加载页推送进度
    """
    def say(msg):
        print(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    say('正在初始化数据库，首次启动约需 1~2 分钟…')

    call_command('migrate', interactive=False, verbosity=0)
    say('数据库结构已就绪，正在导入考研真题…')
    try:
        call_command('import_exam_questions', verbosity=0)
        say('考研真题导入完成，正在导入六级翻译…')
    except Exception as e:
        say(f'考研真题导入失败：{e}')

    try:
        call_command('import_cet6_translations', verbosity=0)
        say('六级翻译导入完成，正在导入词汇本词库…')
    except Exception as e:
        say(f'六级翻译导入失败：{e}')

    # 词汇本词库（若打包时带入了 data/hongbaoshu.json）
    hongbao = BASE_DIR / 'data' / 'hongbaoshu.json'
    if hongbao.exists():
        try:
            call_command('import_words', str(hongbao), verbosity=0)
            say('词汇本词库导入完成，正在进入主程序…')
        except Exception as e:
            say(f'词库导入失败：{e}')

    say('初始化完成！')


def run_django(port):
    """后台线程跑 Django；use_reloader=False 避免 PyInstaller 下双进程"""
    call_command('runserver', f'127.0.0.1:{port}', use_reloader=False)


# ===== 初始化加载页（内置 HTML，无需网络）=====
LOADING_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>正在启动</title>
<style>
  html,body{margin:0;height:100%;display:flex;align-items:center;justify-content:center;
    background:#faf7f2;font-family:"Microsoft YaHei",sans-serif;color:#4a4238;}
  .box{text-align:center;max-width:420px;padding:0 24px;}
  .spinner{width:44px;height:44px;margin:0 auto 18px;border:4px solid #e5ddd0;
    border-top-color:#8b6f47;border-radius:50%;animation:spin 1s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg)}}
  h2{margin:0 0 10px;font-size:20px;}
  #msg{font-size:13px;color:#8a8175;line-height:1.8;min-height:22px;}
</style></head>
<body><div class="box">
  <div class="spinner"></div>
  <h2>考研英语学习平台</h2>
  <div id="msg">正在启动…</div>
</div>
<script>
  window.setMsg = function (t) { document.getElementById('msg').textContent = t; };
</script>
</body></html>"""


def main_gui():
    """打包模式：原生窗口 + 无控制台"""
    import webview

    port = find_port()
    first_run = not DB_PATH.exists()

    class AppApi:
        """暴露给前端 JS 的原生能力（window.pywebview.api）"""

        def __init__(self):
            self._wins = {}  # url -> 已打开的外链窗口（避免重复开窗）

        def open_external(self, url):
            """外链在应用内新开窗口打开，不离开程序、不调系统浏览器"""
            if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                return False
            host = (urllib.parse.urlparse(url).hostname or '').lower()
            if host in ('127.0.0.1', 'localhost', '::1'):
                return False  # 应用内地址不拦截
            old = self._wins.get(url)
            if old is not None:
                try:
                    old.show()
                    old.load_url(url)
                    return True
                except Exception:
                    self._wins.pop(url, None)
            try:
                w = webview.create_window(
                    url[:70], url,
                    width=1100, height=760, min_size=(640, 480),
                )

                def _cleanup(key=url):
                    self._wins.pop(key, None)

                w.events.closed += _cleanup
                self._wins[url] = w
                return True
            except Exception as e:
                print(f'open_external 失败: {e}')
                return False

    api = AppApi()

    # 先弹加载窗（尤其首启初始化耗时较长，不能让用户对着空气等）
    window = webview.create_window(
        APP_TITLE, html=LOADING_HTML,
        width=1280, height=860, min_size=(960, 640),
        js_api=api,
    )

    def boot():
        if first_run:
            first_run_init(progress=lambda m: window.evaluate_js('setMsg(%s)' % json.dumps(m, ensure_ascii=False)))
        else:
            # 即便 db 已存在，也确保 migrations 已应用（防止跨版本升级）
            call_command('migrate', interactive=False, verbosity=0)
        threading.Thread(target=run_django, args=(port,), daemon=True).start()
        if wait_server_ready(port):
            window.load_url(f'http://127.0.0.1:{port}/')
        else:
            window.evaluate_js("setMsg('服务启动超时，请查看 data/launch.log')")

    threading.Thread(target=boot, daemon=True).start()
    # webview.start() 阻塞到窗口关闭；关窗即退出整个进程
    webview.start()
    os._exit(0)


def main_console():
    """开发模式：保持原行为（runserver + 默认浏览器）"""
    if not DB_PATH.exists():
        first_run_init()
    else:
        call_command('migrate', interactive=False, verbosity=0)

    def open_browser():
        time.sleep(2)
        try:
            webbrowser.open('http://127.0.0.1:8000/')
        except Exception:
            pass
    threading.Thread(target=open_browser, daemon=True).start()

    print('=' * 56)
    print('  考研英语学习平台已启动')
    print('  浏览器访问：http://127.0.0.1:8000/')
    print('  数据目录：' + str(DATA_DIR))
    print('  按 Ctrl+C 退出')
    print('=' * 56)

    call_command('runserver', '127.0.0.1:8000', use_reloader=False)


if __name__ == '__main__':
    if FROZEN:
        main_gui()
    else:
        main_console()
