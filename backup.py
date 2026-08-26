#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backup.py — 合并产物备份到 Windows PC（scp over Tailscale，密钥已配置）。

策略（2026-08-27 起）：
- 合并完成后立即后台尝试（快速路径，3 次重试总窗口 90 秒，不阻塞响应）
- 快速路径失败（PC 离线等）→ 任务进入待备份队列，服务器保留 6 小时
- 后台 daemon 每 60 秒探测 PC：上线即扫描队列补备份，成功后清理服务器工作文件
- 超 6 小时仍未成功 → 放弃，由 sweep 清理
- 状态写 job 目录 backup.json（含 username/filename，供 daemon 重建远端路径）
- 前端对备份全程静默：不显示进度、不轮询（备份是"后台兜底"，不是主流程）
"""
import json
import os
import shutil
import subprocess
import threading
import time

RETRY_DELAYS = [0, 30, 60]      # 秒（快速路径：3 次尝试）
SCP_TIMEOUT = 120
SSH_TIMEOUT = 30
DAEMON_INTERVAL = 60            # 秒（PC 上线探测 + 补备份扫描周期）
BACKUP_KEEP_HOURS = 6           # PC 离线时服务器保留时长
KEEP_FILES = {"merged.pdf", "meta.json", "backup.json"}

_STATUS_LOCK = threading.Lock()  # daemon 与快速路径互斥写状态


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


def read_backup_status(workdir):
    try:
        with open(os.path.join(workdir, "backup.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_backup_status(workdir, **kw):
    with _STATUS_LOCK:
        try:
            with open(os.path.join(workdir, "backup.json"), "w", encoding="utf-8") as f:
                json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **kw},
                          f, ensure_ascii=False)
        except OSError:
            pass


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


def _do_backup(target, workdir, merged_path, fname, username):
    """单次备份尝试（无重试）。成功返回 True，失败返回 False。"""
    host, _, base_dir = target.partition(":")
    if not host or not base_dir:
        write_backup_status(workdir, status="failed", detail="备份目标配置无效",
                            username=username, filename=fname)
        return False
    remote_dir = base_dir.rstrip("/\\") + "/" + (username or "private")
    try:
        _ensure_remote_dir(host, remote_dir)
        return _scp_file(merged_path, host, remote_dir, fname)
    except Exception:
        return False


def schedule_backup(target, workdir, merged_path, fname, username=None):
    """快速路径：后台线程立即尝试（3 次重试）。

    失败不删除任务，保持 failed 状态等待 daemon 在 PC 上线后补备份。
    远端落点：<target>/<username 或 private>/<fname>
    """
    write_backup_status(workdir, status="pending", detail="等待首次尝试",
                        username=username, filename=fname)

    def _run():
        host = target.partition(":")[0]
        last_err = ""
        for delay in RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            if not _ssh_ok(host):
                last_err = f"PC 不可达（{host}）"
                continue
            try:
                if _do_backup(target, workdir, merged_path, fname, username):
                    cleanup_workdir(workdir)
                    write_backup_status(workdir, status="ok", detail="已备份到 PC",
                                        username=username, filename=fname)
                    return
                last_err = "scp 传输失败"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
        # 快速路径失败：进入待备份队列（服务器保留 6 小时，daemon 补备份）
        write_backup_status(workdir, status="failed",
                            detail=f"{last_err or '重试次数用尽'}（等待 PC 上线补备份）",
                            username=username, filename=fname)

    threading.Thread(target=_run, daemon=True).start()


def scan_and_retry(target, jobs_dir, keep_hours=BACKUP_KEEP_HOURS):
    """daemon 调用：PC 在线时扫描待备份任务并补备份。返回本轮补成功的数量。"""
    host = target.partition(":")[0]
    if not host or not _ssh_ok(host):
        return 0            # PC 仍离线，本轮什么都不做
    if not os.path.isdir(jobs_dir):
        return 0
    done = 0
    now = time.time()
    for d in sorted(os.listdir(jobs_dir)):
        jdir = os.path.join(jobs_dir, d)
        if not os.path.isdir(jdir):
            continue
        st = read_backup_status(jdir)
        if not st or st.get("status") not in ("pending", "failed"):
            continue
        merged = os.path.join(jdir, "merged.pdf")
        if not os.path.exists(merged):
            continue
        # 年龄以 merged.pdf 的 mtime 为准（backup.json 会被反复重写，不能作年龄基准）
        if now - os.path.getmtime(merged) > keep_hours * 3600:
            continue
        fname = st.get("filename") or "合并文档.pdf"
        username = st.get("username")
        write_backup_status(jdir, status="running", detail="PC 已上线，补备份中",
                            username=username, filename=fname)
        try:
            if _do_backup(target, jdir, merged, fname, username):
                cleanup_workdir(jdir)
                write_backup_status(jdir, status="ok", detail="PC 上线后补备份成功",
                                    username=username, filename=fname)
                done += 1
            else:
                write_backup_status(jdir, status="failed", detail="PC 在线但传输失败",
                                    username=username, filename=fname)
        except Exception as e:
            write_backup_status(jdir, status="failed",
                                detail=f"{type(e).__name__}: {e}",
                                username=username, filename=fname)
    return done


def start_backup_daemon(target, jobs_dir, interval=DAEMON_INTERVAL):
    """启动周期扫描线程（app 启动时调用一次）。"""
    def _loop():
        while True:
            time.sleep(interval)
            try:
                scan_and_retry(target, jobs_dir)
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()
