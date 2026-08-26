#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backup.py — 合并产物备份到 Windows PC（scp over Tailscale，密钥已配置）。

设计：
- 合并完成后后台线程执行：scp merged.pdf → PC 备份目录
- 重试 3 次（间隔 0 / 60 / 300 秒），PC 离线时不阻塞主流程
- 成功后：清理服务器侧工作文件（raw_*/NNN_*），保留 merged.pdf + meta.json 供下载
  （2 小时自动清理兜底）
- 状态写 job 目录 backup.json：{"status": "ok"|"failed", "time": ..., "detail": ...}
"""
import json
import os
import shutil
import subprocess
import threading
import time

RETRY_DELAYS = [0, 30, 60]      # 秒（3 次尝试，总窗口 90 秒，配合前端 2 分钟轮询）
SCP_TIMEOUT = 120
SSH_TIMEOUT = 30
KEEP_FILES = {"merged.pdf", "meta.json", "backup.json"}


def _ssh_ok(host):
    """检查远端主机 SSH 是否可达。"""
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                            host, "echo ok"], capture_output=True, text=True,
                           timeout=SSH_TIMEOUT)
        return r.returncode == 0 and "ok" in r.stdout
    except subprocess.TimeoutExpired:
        return False


def _ensure_remote_dir(host, remote_dir):
    """远端目录不存在则创建（Windows OpenSSH 支持 mkdir -p）。"""
    subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                    f"mkdir -p \"{remote_dir}\""],
                   capture_output=True, timeout=SSH_TIMEOUT)


def _scp_file(local, host, remote_dir, filename):
    r = subprocess.run(
        ["scp", "-o", "ConnectTimeout=10", local,
         f'{host}:"{remote_dir}/{filename}"'],
        capture_output=True, text=True, timeout=SCP_TIMEOUT)
    return r.returncode == 0


def cleanup_workdir(workdir):
    """备份成功后清理工作文件，只留下载所需。"""
    for name in os.listdir(workdir):
        if name not in KEEP_FILES:
            p = os.path.join(workdir, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except OSError:
                pass


def schedule_backup(target, workdir, merged_path, fname, username=None):
    """后台线程执行备份。target 形如 "win-pc:C:/Users/Admin/Desktop/PDF导出备份"。

    远端落点：<target>/<username 或 private>/<fname>
    状态写 <workdir>/backup.json，前端可轮询 /api/jobs/<job>/backup。
    """
    def _write_status(status, detail=""):
        try:
            with open(os.path.join(workdir, "backup.json"), "w", encoding="utf-8") as f:
                json.dump({"status": status, "detail": detail,
                           "time": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        except OSError:
            pass

    def _run():
        host, _, base_dir = target.partition(":")
        if not host or not base_dir:
            _write_status("failed", "备份目标配置无效")
            return
        remote_dir = base_dir.rstrip("/\\") + "/" + (username or "private")
        last_err = ""
        for delay in RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            if not _ssh_ok(host):
                last_err = f"PC 不可达（{host}）"
                continue
            try:
                _ensure_remote_dir(host, remote_dir)
                if _scp_file(merged_path, host, remote_dir, fname):
                    cleanup_workdir(workdir)
                    _write_status("ok", f"已备份到 {host}:{remote_dir}/{fname}")
                    return
                last_err = "scp 传输失败"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
        _write_status("failed", last_err or "重试次数用尽")

    threading.Thread(target=_run, daemon=True).start()
