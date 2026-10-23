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
import shutil
import socket
import threading
import webbrowser
import subprocess
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

# ===== 隐藏模式：作为独立更新器运行 =====
# 必须在 django.setup() 和 webview 初始化之前拦截：更新器只做文件操作，
# 不启动服务、不打开窗口，避免占用文件句柄导致替换失败。
if '--apply-update' in sys.argv:
    from updater import run_updater
    sys.exit(run_updater(sys.argv))

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


def backup_before_migrate():
    """存在未应用迁移时，先用 SQLite 在线备份 API 备份数据库。

    返回备份路径；无需迁移或备份失败返回 None（备份失败时不阻断启动，
    但会写日志，迁移本身仍受 safe_migrate 保护）。
    """
    if not DB_PATH.exists():
        return None
    try:
        import sqlite3
        from django.db import connections
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connections['default'])
        if not executor.migration_plan(executor.loader.graph.leaf_nodes()):
            return None  # 没有待应用迁移，纯 bug 修复不产生无意义备份

        backup_dir = DATA_DIR / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / ('db_before_migrate_%s.sqlite3' % time.strftime('%Y%m%d_%H%M%S'))
        src = sqlite3.connect(str(DB_PATH))
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)  # 在线备份，避免直接复制正在写入的数据库文件
                result = dst.execute('PRAGMA integrity_check').fetchone()
                if not result or result[0] != 'ok':
                    raise RuntimeError('备份文件完整性检查未通过')
            finally:
                dst.close()
        finally:
            src.close()
        print(f'迁移前备份完成：{dest}')
        return dest
    except Exception as e:
        print(f'迁移前备份失败（继续尝试迁移）：{e}')
        return None


def safe_migrate():
    """迁移前备份 + 迁移 + 迁移后完整性检查。

    失败时抛出异常，由调用方中止启动：绝不删除数据库或重建空库。
    """
    import sqlite3

    backup = backup_before_migrate()
    try:
        call_command('migrate', interactive=False, verbosity=0)
    except Exception:
        print(f'数据库迁移失败，原库已保留；备份：{backup or "无"}')
        raise
    try:
        con = sqlite3.connect(str(DB_PATH))
        try:
            result = con.execute('PRAGMA quick_check').fetchone()
        finally:
            con.close()
        if not result or result[0] != 'ok':
            raise RuntimeError('迁移后数据库完整性检查未通过')
    except Exception:
        print(f'迁移后完整性检查失败，已停止启动；备份：{backup or "无"}')
        raise


def _read_update_result():
    """读取上次更新结果，供启动加载页提示（仅看 10 分钟内的记录）"""
    from updater import read_status
    data = read_status(DATA_DIR / 'update' / 'last_update.json')
    if not data or data.get('state') not in ('done', 'rolled_back', 'failed'):
        return None
    try:
        at = time.mktime(time.strptime(str(data.get('at') or ''), '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return None
    if time.time() - at > 600:
        return None
    return {
        'done': '新版本已安装，正在启动…',
        'rolled_back': '本次更新失败，已自动恢复到旧版本',
        'failed': '本次更新失败，原版本未受影响',
    }.get(data.get('state'))


def cleanup_update_dir():
    """清理上次更新遗留的临时文件。

    保留 last_update.json（启动时展示更新结果）与 .bak（回滚备份）；
    只删除 1 小时前的 .part、更新器副本和已下载但未安装的 EXE。
    """
    update_dir = DATA_DIR / 'update'
    if not update_dir.exists():
        return
    cutoff = time.time() - 3600
    for p in update_dir.iterdir():
        try:
            if not p.is_file() or p.name == 'last_update.json' or p.name.endswith('.bak'):
                continue
            if (p.name.endswith('.part') or p.name.startswith('updater_')
                    or p.suffix.lower() == '.exe'):
                if p.stat().st_mtime < cutoff:
                    p.unlink()
        except OSError:
            pass


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

    safe_migrate()
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

        def open_external(self, url):
            """外链直接用电脑系统默认浏览器打开，不再用打包窗口内新开窗。"""
            if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                return False
            host = (urllib.parse.urlparse(url).hostname or '').lower()
            if host in ('127.0.0.1', 'localhost', '::1'):
                return False  # 应用内地址不拦截，避免把本机页面丢给浏览器
            try:
                webbrowser.open(url)
                return True
            except Exception as e:
                print(f'open_external 失败: {e}')
                return False

        def save_backup(self, src_path, filename):
            """弹出系统「另存为」对话框，把已生成的本机备份副本复制到用户选择的位置。

            返回 {'cancelled': True} 表示用户取消；成功返回路径与大小。
            不在前端传文件内容，避免大备份走 JS 桥；服务端副本已在本机磁盘。
            """
            import webview
            try:
                if not isinstance(src_path, str) or not os.path.exists(src_path):
                    return {'error': '备份副本不存在，可能已被清理'}
                win = webview.active_window()
                result = win.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=filename or 'backup.json',
                    file_types=('JSON 备份文件 (*.json)', '所有文件 (*.*)'),
                )
                if not result:
                    return {'cancelled': True}
                dest = result[0] if isinstance(result, (list, tuple)) else str(result)
                if not dest:
                    return {'cancelled': True}
                if not dest.lower().endswith('.json'):
                    dest += '.json'
                shutil.copy2(src_path, dest)
                return {'cancelled': False, 'path': os.path.abspath(dest),
                        'size': os.path.getsize(dest)}
            except Exception as e:
                print(f'save_backup 失败: {e}')
                return {'error': str(e)}

        def show_in_folder(self, path):
            """在 Windows 资源管理器中定位并选中该文件（Edge 下载项同款行为）。"""
            try:
                if not isinstance(path, str) or not os.path.exists(path):
                    return {'success': False, 'error': '文件不存在，可能已被移动或删除'}
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
                return {'success': True}
            except Exception as e:
                return {'success': False, 'error': str(e)}

        def open_local_file(self, path):
            """用系统默认程序打开本地文件。"""
            try:
                if not isinstance(path, str) or not os.path.exists(path):
                    return {'success': False, 'error': '文件不存在，可能已被移动或删除'}
                os.startfile(os.path.normpath(path))
                return {'success': True}
            except Exception as e:
                return {'success': False, 'error': str(e)}

        def exit_app(self):
            """退出整个程序。

            用于首次启动「不同意用户许可与使用协议」等必须退出的场景：
            浏览器里的 window.close() 对非脚本打开的窗口无效，必须由宿主关闭。
            先销毁窗口让 webview.start() 返回，再由主线程 os._exit(0) 收尾；
            销毁失败时直接强制结束进程，保证「不同意」一定能退出。
            """
            try:
                win = webview.active_window()
                if win is not None:
                    win.destroy()
                    return {'success': True}
            except Exception as e:
                print(f'exit_app 销毁窗口失败，强制退出: {e}')
            # 兜底：延迟片刻让返回值先送达前端，再结束进程
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return {'success': True}

        def install_update(self, download_path, tag=''):
            """启动独立更新器安装已下载并校验的新 EXE。

            前端在返回成功后调用 exit_app 退出主进程；更新器会等待本进程
            真正结束，再备份、替换、启动新版。只接受 data/update 下的文件。
            返回后不立即退出，保证结果能送达前端。
            """
            if not FROZEN:
                return {'success': False, 'error': '仅 Windows 桌面版支持自动安装'}
            if getattr(self, '_updater_started', False):
                return {'success': False, 'error': '更新器已在运行，请勿重复操作'}
            try:
                update_dir = (DATA_DIR / 'update').resolve()
                src = Path(str(download_path)).resolve()
                if update_dir not in src.parents:
                    return {'success': False, 'error': '更新文件路径不合法'}
                if not src.exists():
                    return {'success': False, 'error': '更新文件不存在，请重新下载'}

                from updater import MIN_EXE_BYTES
                if src.stat().st_size < MIN_EXE_BYTES:
                    return {'success': False, 'error': '更新文件大小异常，已拒绝安装'}

                target = Path(sys.executable).resolve()
                updater_copy = update_dir / ('updater_%d.exe' % os.getpid())
                shutil.copy2(str(target), str(updater_copy))
                status_file = update_dir / 'last_update.json'
                backup_file = update_dir / 'word.exe.bak'

                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                subprocess.Popen(
                    [str(updater_copy), '--apply-update',
                     '--pid', str(os.getpid()),
                     '--target', str(target),
                     '--new', str(src),
                     '--backup', str(backup_file),
                     '--status', str(status_file),
                     '--to-version', str(tag or '')],
                    cwd=str(update_dir), close_fds=True, creationflags=flags,
                )
                self._updater_started = True
                return {'success': True}
            except Exception as e:
                print(f'install_update 失败: {e}')
                return {'success': False, 'error': str(e)}

    api = AppApi()

    # 先弹加载窗（尤其首启初始化耗时较长，不能让用户对着空气等）
    window = webview.create_window(
        APP_TITLE, html=LOADING_HTML,
        width=1280, height=860, min_size=(960, 640),
        js_api=api,
    )

    def boot():
        try:
            cleanup_update_dir()
            update_result = _read_update_result()
            if update_result:
                window.evaluate_js('setMsg(%s)' % json.dumps(
                    update_result, ensure_ascii=False))
            if first_run:
                first_run_init(progress=lambda m: window.evaluate_js('setMsg(%s)' % json.dumps(m, ensure_ascii=False)))
            else:
                # 即便 db 已存在，也确保 migrations 已应用（防止跨版本升级）
                safe_migrate()
        except Exception as e:
            print(f'启动前数据库准备失败：{e}')
            window.evaluate_js('setMsg(%s)' % json.dumps(
                f'数据库准备失败：{e}。原数据库与备份已保留在 data 目录，请查看 data/launch.log',
                ensure_ascii=False))
            return
        threading.Thread(target=run_django, args=(port,), daemon=True).start()
        if wait_server_ready(port):
            # 服务已就绪说明新版本确实启动起来了；回写启动确认，更新器/界面据此显示"更新成功"
            try:
                from updater import confirm_boot
                confirm_boot(DATA_DIR / 'update', settings.APP_VERSION)
            except Exception as e:
                print(f'写入更新启动确认失败：{e}')
            window.load_url(f'http://127.0.0.1:{port}/')
        else:
            window.evaluate_js("setMsg('服务启动超时，请查看 data/launch.log')")

    threading.Thread(target=boot, daemon=True).start()
    # webview.start() 阻塞到窗口关闭；关窗即退出整个进程
    # private_mode=False + storage_path：让 WebView2 的 cookie / localStorage / IndexedDB
    # 持久化到 data/webview2，否则每次关闭窗口都会丢失会话与本地缓存（壁纸、考试进度等）
    webview.start(private_mode=False, storage_path=str(DATA_DIR / 'webview2'))
    os._exit(0)


def main_console():
    """开发模式：保持原行为（runserver + 默认浏览器）"""
    if not DB_PATH.exists():
        first_run_init()
    else:
        safe_migrate()

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
