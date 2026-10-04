# -*- coding: utf-8 -*-
"""API 密钥保险箱：PBKDF2-HMAC-SHA256 派生密钥 + Fernet 认证加密。

设计要点：
- 用户密码本身不落库，库中只保存随机 salt 与一个由派生根密钥加密的校验串；
- Fernet 自带 HMAC 认证，密文被篡改时解密直接抛 InvalidToken；
- 解锁后的派生根密钥只保存在进程内存（模块级变量），重启程序即自动锁定；
- 密文以 Fernet 版本字节开头（base64 后形如 gAAAAA...），据此与旧版明文区分。
"""
import base64
import os
import threading

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 240_000
VERIFIER_PLAINTEXT = b'vocab-vault-ok-v1'

# 进程内解锁状态：{'key': bytes|None}。单进程本地服务，重启即失效。
_lock = threading.Lock()
_state = {'key': None}


def _derive_key(password, salt):
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode('utf-8')))


def new_salt():
    """生成新的随机 salt（返回 hex 字符串，便于入库）。"""
    return os.urandom(16).hex()


def _fernet_for(password, salt_hex):
    return Fernet(_derive_key(password, bytes.fromhex(salt_hex)))


def make_verifier(password, salt_hex):
    """用密码加密固定校验串，供日后验证密码正确性。"""
    return _fernet_for(password, salt_hex).encrypt(VERIFIER_PLAINTEXT).decode('ascii')


def verify_password(password, salt_hex, verifier):
    """校验密码是否正确；正确返回派生根 Fernet（bytes），错误返回 None。"""
    try:
        key = _derive_key(password, bytes.fromhex(salt_hex))
        Fernet(key).decrypt(verifier.encode('ascii'))
        return key
    except (InvalidToken, ValueError, TypeError):
        return None


def unlock_in_memory(key):
    with _lock:
        _state['key'] = key


def lock():
    with _lock:
        _state['key'] = None


def is_unlocked():
    with _lock:
        return _state['key'] is not None


def _fernet_locked_or_none():
    with _lock:
        key = _state['key']
    return Fernet(key) if key else None


def encrypt(plaintext):
    """用当前内存中的解锁密钥加密；未解锁或空文本返回 ''。"""
    if not plaintext:
        return ''
    f = _fernet_locked_or_none()
    if f is None:
        raise RuntimeError('保险箱未解锁')
    return f.encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt(ciphertext):
    """用当前内存中的解锁密钥解密；未解锁/空串抛错或返回 ''。"""
    if not ciphertext:
        return ''
    f = _fernet_locked_or_none()
    if f is None:
        raise RuntimeError('保险箱未解锁')
    return f.decrypt(ciphertext.encode('utf-8')).decode('utf-8')


def is_encrypted(value):
    """粗略判断库中值是否为 Fernet 密文（用于旧明文兼容）。"""
    return bool(value) and isinstance(value, str) and value.startswith('gAAAAA')
