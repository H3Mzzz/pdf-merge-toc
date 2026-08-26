#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
user_admin.py — 多用户模式的用户管理与认证模块（app.py 导入复用，也可作 CLI）。

设计：
- 用户数据存实例目录下 users.json（dict，username 为 key）
- 密码用 hashlib.scrypt 加盐哈希存储（零第三方依赖，标准库 KDF）
- 角色：admin（管理员，可管理所有用户）/ user（普通用户，只操作自己的库）
- disabled = 禁用（"踢人"，保留其材料库数据，重新 enable 可恢复）

CLI 用法：
  python3 user_admin.py init <管理员用户名> <密码>       # 初始化用户库并创建管理员
  python3 user_admin.py add <用户名> [密码] [--admin]   # 加用户；密码缺省自动生成随机
  python3 user_admin.py list                            # 列出用户
  python3 user_admin.py reset <用户名> <新密码>          # 重置密码
  python3 user_admin.py disable|enable <用户名>          # 禁用/恢复（踢人）
  python3 user_admin.py delete <用户名>                  # 删除账号（保留其材料库）
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import string
import sys
import time

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1   # 约 16MB 内存，单次 ~50ms


def users_path(instance_dir="."):
    return os.path.join(instance_dir, "users.json")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p))
        return dk.hex() == dk_hex
    except (ValueError, TypeError):
        return False


def load_users(instance_dir="."):
    p = users_path(instance_dir)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("users", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_users(users, instance_dir="."):
    p = users_path(instance_dir)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def validate_username(name):
    if not USERNAME_RE.match(name or ""):
        raise ValueError("用户名只允许 1-32 位字母、数字、下划线、连字符")


def add_user(users, username, password, role="user"):
    validate_username(username)
    if username in users:
        raise ValueError(f"用户已存在：{username}")
    users[username] = {
        "hash": hash_password(password),
        "role": role,
        "disabled": False,
        "created_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    return users


def authenticate(users, username, password):
    """校验通过返回用户 dict，否则 None。"""
    u = users.get(username or "")
    if not u or u.get("disabled"):
        return None
    if verify_password(password, u["hash"]):
        return u
    return None


def random_password(length=10):
    return "".join(secrets.choice(string.ascii_lowercase + string.digits)
                   for _ in range(length))


# ---------------- CLI ----------------
def main():
    ap = argparse.ArgumentParser(description="pdf-merge-toc 用户管理")
    ap.add_argument("action", choices=["init", "add", "list", "reset", "disable", "enable", "delete"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--admin", action="store_true", help="add 时设为管理员")
    ap.add_argument("--dir", default=".", help="实例目录（users.json 所在目录）")
    a = ap.parse_args()

    users = load_users(a.dir)
    try:
        if a.action == "init":
            if users:
                print("users.json 已存在，不做覆盖。如需重来请先删除该文件。")
                return
            name, pwd = a.args[0], a.args[1]
            add_user(users, name, pwd, role="admin")
            save_users(users, a.dir)
            print(f"已初始化用户库，管理员：{name}")
        elif a.action == "add":
            name = a.args[0]
            pwd = a.args[1] if len(a.args) > 1 else random_password()
            add_user(users, name, pwd, role="admin" if a.admin else "user")
            save_users(users, a.dir)
            print(f"已添加用户 {name}（{'管理员' if a.admin else '普通用户'}），口令：{pwd}")
        elif a.action == "list":
            if not users:
                print("（无用户）")
            for name, u in users.items():
                print(f"  {name:<20} role={u.get('role'):<6} "
                      f"{'[禁用]' if u.get('disabled') else ''} created={u.get('created_at')}")
        elif a.action == "reset":
            name, pwd = a.args[0], a.args[1]
            if name not in users:
                sys.exit(f"用户不存在：{name}")
            users[name]["hash"] = hash_password(pwd)
            users[name]["disabled"] = False
            save_users(users, a.dir)
            print(f"已重置 {name} 的口令")
        elif a.action in ("disable", "enable"):
            name = a.args[0]
            if name not in users:
                sys.exit(f"用户不存在：{name}")
            users[name]["disabled"] = (a.action == "disable")
            save_users(users, a.dir)
            print(f"已{'禁用' if a.action == 'disable' else '恢复'} {name}")
        elif a.action == "delete":
            name = a.args[0]
            if name not in users:
                sys.exit(f"用户不存在：{name}")
            del users[name]
            save_users(users, a.dir)
            print(f"已删除账号 {name}（其材料库 library/{name}/ 保留，需手动清理）")
    except (ValueError, IndexError) as e:
        sys.exit(f"错误：{e}")


if __name__ == "__main__":
    main()
