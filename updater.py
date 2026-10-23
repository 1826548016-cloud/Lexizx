# -*- coding: utf-8 -*-
"""EXE 自动更新核心逻辑（纯标准库，不依赖 Django / webview）。

被三处复用：
- words/views.py：版本比较、资产地址、下载、SHA-256 校验
- product/launch.py：隐藏模式 ``--apply-update`` 执行替换与回滚
- words/tests.py：单元测试

安全约定：
- 只接受官方仓库的 Release 资产地址，版本标签必须是 x.y.z 形式，
  防止把任意字符串拼进下载 URL；
- 新 EXE 先下载成 ``*.part``，校验通过后才改名为正式文件；
- 替换前备份旧 EXE，替换或启动失败自动回滚；
- 更新器进程只做文件操作，不初始化 Django，也不打开窗口。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

GITHUB_REPO = '1826548016-cloud/Lexizx'
ASSET_NAME = 'word.exe'
SHA_ASSET_NAME = 'word.exe.sha256'
USER_AGENT = 'Postgraduate-Vocabulary-Updater'

# 正常完整 EXE 约 60MB；低于该阈值视为下载不完整或异常文件
MIN_EXE_BYTES = 10 * 1024 * 1024


class UpdateError(Exception):
    """更新流程中的可预期错误（提示给用户）"""


class UpdateCancelled(Exception):
    """用户取消下载"""


# ===== 版本号 =====

def parse_version(text):
    """'v3.4.0' / '3.4.0-beta' -> (3, 4, 0)；无法解析返回 None"""
    m = re.search(r'(\d+)\.(\d+)\.(\d+)', str(text or ''))
    return tuple(int(x) for x in m.groups()) if m else None


def compare_versions(a, b):
    """a 新于 b 返回 1，a 旧于 b 返回 -1，相等或无法解析返回 0"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return 0
    return (pa > pb) - (pa < pb)


def normalize_tag(tag):
    """校验并返回 Release 标签；非法标签直接拒绝"""
    t = str(tag or '').strip()
    if not re.fullmatch(r'v?\d+\.\d+\.\d+', t):
        raise UpdateError('版本标签不合法，已拒绝下载')
    return t


def asset_urls(tag):
    """返回 (EXE 地址, SHA-256 地址)。直接拼 releases/download/<tag>/，
    不走 API，因此不受未认证 60 次/小时配额限制。"""
    t = urllib.parse.quote(normalize_tag(tag), safe='')
    base = f'https://github.com/{GITHUB_REPO}/releases/download/{t}/'
    return base + ASSET_NAME, base + SHA_ASSET_NAME


# ===== 下载 =====

def _request(url, timeout):
    return urllib.request.Request(url, headers={'User-Agent': USER_AGENT})


def download_file(url, dest, progress=None, cancel=None, timeout=30,
                  chunk_size=256 * 1024, min_bytes=MIN_EXE_BYTES):
    """流式下载到 dest（先写 .part，成功后替换）。

    progress(done, total)：total 为 0 表示服务器未给 Content-Length。
    cancel：threading.Event，置位后抛 UpdateCancelled。
    返回实际字节数。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + '.part')
    received = 0
    try:
        with urllib.request.urlopen(_request(url, timeout), timeout=timeout) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
            with open(part, 'wb') as f:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise UpdateCancelled('已取消下载')
                    buf = resp.read(chunk_size)
                    if not buf:
                        break
                    f.write(buf)
                    received += len(buf)
                    if progress:
                        progress(received, total)
        if received < min_bytes:
            raise UpdateError(f'下载文件不完整（{received} 字节），已拒绝安装')
        os.replace(str(part), str(dest))
        return received
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_sha_text(text):
    """从 sha256 文件内容中提取 64 位十六进制摘要"""
    m = re.search(r'\b([0-9a-fA-F]{64})\b', str(text or ''))
    return m.group(1).lower() if m else None


def fetch_expected_sha(tag, timeout=15):
    """读取 Release 中的 word.exe.sha256；不存在时抛 HTTPError(404)"""
    _, sha_url = asset_urls(tag)
    with urllib.request.urlopen(_request(sha_url, timeout), timeout=timeout) as resp:
        text = resp.read().decode('utf-8', 'ignore')
    digest = parse_sha_text(text)
    if not digest:
        raise UpdateError('校验文件内容无法解析')
    return digest


def verify_sha256(path, expected):
    expected = str(expected or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        return False
    return sha256_file(path) == expected



def write_status(path, payload):
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    data = dict(payload or {})
    data.setdefault('at', time.strftime('%Y-%m-%d %H:%M:%S'))
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(tmp), str(path))


def read_status(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return None


def log_line(path, message):
    """追加更新日志（更新器无控制台，日志落 data/update/updater.log）"""
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), message))
    except Exception:
        pass


def boot_marker_path(update_dir):
    return Path(update_dir) / 'boot_ok.json'


def wait_for_boot(update_dir, version, timeout=25):
    """等待新版启动后写下的 boot_ok.json（版本一致即视为启动成功）"""
    deadline = time.time() + timeout
    marker = boot_marker_path(update_dir)
    while time.time() < deadline:
        data = read_status(marker)
        if data and compare_versions(data.get('version'), version) == 0:
            return True
        time.sleep(0.5)
    return False


def confirm_boot(update_dir, version):
    """由新版自己调用：写启动标记，并把 last_update.json 升级为 done。

    只有在目标版本与当前版本一致时才确认，避免旧版本误报更新成功。
    """
    update_dir = Path(update_dir)
    marker = boot_marker_path(update_dir)
    write_status(marker, {'version': version})
    status_path = update_dir / 'last_update.json'
    data = read_status(status_path)
    if not data or data.get('state') not in ('installed', 'done'):
        return data
    target = str(data.get('to_version') or '')
    if target and compare_versions(version, target) == 0:
        data.update(state='done', message='更新成功，新版本已启动')
        write_status(status_path, data)
    return data


# ===== 独立更新器（--apply-update 隐藏模式）=====

def parse_updater_args(argv):
    """把 ['--pid','123','--target','x'] 解析成 dict"""
    opts = {}
    args = list(argv or [])
    i = 0
    while i < len(args):
        token = args[i]
        if isinstance(token, str) and token.startswith('--'):
            opts[token[2:]] = args[i + 1] if i + 1 < len(args) else ''
            i += 2
        else:
            i += 1
    return opts


def _pid_alive(pid):
    """判断进程是否仍存在（Windows 用 OpenProcess，其它平台用 os.kill）"""
    if not pid:
        return False
    if os.name != 'nt':
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    import ctypes
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def wait_for_exit(pid, timeout=180, interval=0.5):
    """等待指定 PID 退出；超时返回 False。不使用固定 sleep 猜测"""
    if not pid:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(interval)
    return not _pid_alive(pid)


def _start_detached(path):
    """以独立进程启动新版 EXE（不随更新器退出而结束）"""
    flags = 0
    if os.name == 'nt':
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(path)], cwd=str(Path(path).parent),
                     close_fds=True, creationflags=flags)


def run_updater(argv):
    """更新器入口：等待旧进程退出 -> 备份 -> 替换 -> 启动 -> 等待新版启动确认。

    返回进程退出码：0 成功（替换完成并已启动），非 0 失败。
    该函数不初始化 Django，只做文件操作。状态含义：
    - installed：已替换并启动，但新版尚未回写启动标记（视为"未确认成功"）
    - done：新版本自己写下启动标记后才升级为 done，界面据此显示"更新成功"
    """
    opts = parse_updater_args(argv[1:])
    status_path = opts.get('status') or ''
    update_dir = Path(status_path).parent if status_path else Path('.')
    log_path = str(update_dir / 'updater.log') if status_path else ''
    to_version = str(opts.get('to-version') or '')
    target = Path(opts.get('target') or '')
    new_path = Path(opts.get('new') or '')
    backup = Path(opts.get('backup') or (str(target) + '.bak'))
    try:
        pid = int(opts.get('pid') or 0)
    except ValueError:
        pid = 0

    def report(state, message=''):
        payload = {'state': state, 'message': message, 'target': str(target),
                   'backup': str(backup), 'to_version': to_version}
        write_status(status_path, payload)
        log_line(log_path, f'{state}: {message}')

    log_line(log_path, f'开始更新：目标 {target}，新文件 {new_path}，版本 {to_version}，PID {pid}')

    if not str(target).strip() or not new_path.exists():
        report('failed', '更新文件缺失，已取消安装')
        return 2
    if new_path.stat().st_size < MIN_EXE_BYTES:
        report('failed', '更新文件大小异常，已取消安装')
        return 2

    report('waiting', '等待程序退出…')
    if not wait_for_exit(pid):
        report('failed', '原程序未在限定时间内退出，已取消更新')
        return 3
    log_line(log_path, '原程序已退出')

    if target.exists():
        try:
            shutil.copy2(str(target), str(backup))
            log_line(log_path, f'旧版本已备份到 {backup}')
        except OSError as e:
            report('failed', f'备份旧版本失败：{e}')
            return 4

    # 清掉上一次的启动标记，避免误判本次启动成功
    try:
        boot_marker_path(update_dir).unlink()
    except OSError:
        pass

    report('replacing', '正在替换程序文件…')
    last_error = None
    for _ in range(20):
        try:
            os.replace(str(new_path), str(target))
            last_error = None
            break
        except OSError as e:
            last_error = e
            time.sleep(0.5)
    if last_error is not None:
        report('failed', f'替换程序文件失败：{last_error}')
        return 5

    try:
        if target.stat().st_size < MIN_EXE_BYTES:
            raise OSError('替换后的文件大小异常')
        _start_detached(target)
        log_line(log_path, '新版已启动，等待启动确认…')
    except Exception as e:
        # 替换或启动失败：先恢复旧版本文件，再尝试启动旧版本
        if backup.exists():
            try:
                shutil.copy2(str(backup), str(target))
            except Exception as e2:
                report('failed', f'更新失败且回滚失败：{e}；恢复错误：{e2}')
                return 6
            try:
                _start_detached(target)
                report('rolled_back', f'更新失败，已恢复旧版本：{e}')
            except Exception as e3:
                # 文件已恢复，只是没能自动拉起，不能算"回滚失败"
                report('rolled_back', f'更新失败，已恢复旧版本，但未能自动启动，请手动打开程序：{e3}')
            return 1
        report('failed', f'更新失败：{e}')
        return 6

    # 先落 installed（已安装，未确认启动）；新版启动后由 confirm_boot 升级为 done
    report('installed', '已替换并启动，等待新版本确认…')
    if wait_for_boot(update_dir, to_version, timeout=25):
        report('done', '更新成功，新版本已启动')
        log_line(log_path, '新版启动确认成功')
    else:
        log_line(log_path, '未在 25 秒内收到启动确认，保持 installed 状态')
    return 0

