#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf-toc-web — PDF 合并 + 目录页生成器的 Web 服务。

功能：
- 上传 PDF / 图片 / Word（docx/doc，服务器端 LibreOffice 转 PDF）合并
- 常用材料库：上传一次持久保存，后续点击加入合并列表
- 生成可点击跳转的目录页（中文宋体 + 西文 Times New Roman）
- 可选压缩到目标大小（Ghostscript 自动选最高可用清晰度）

认证三态（按配置自动切换）：
1. 无认证：不设 PDF_TOC_ACCESS_KEY 且不存在 users.json（私用实例，只绑可信网络）
2. 单口令：设置 PDF_TOC_ACCESS_KEY（所有用户共享一个口令，共享材料库）
3. 多用户：存在 users.json（每人独立材料库 library/<username>/，管理员可管理用户；
   用 user_admin.py 初始化与维护）

安全说明：
- 公网开放实例务必启用单口令或多用户模式；无认证模式只可绑 127.0.0.1/可信内网
- 临时任务存 <job_dir>/<job>/，2 小时后自动清理，下载按任务属主校验
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import fitz
from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from merge_pdf_toc import run_merge  # noqa: E402
from compress import compress_to_target  # noqa: E402
from user_admin import load_users, authenticate, add_user, hash_password  # noqa: E402

# 监听地址与端口：通过环境变量配置（systemd 单元里设置），默认仅本机回环
BIND_IP = os.environ.get("PDF_TOC_BIND_IP", "127.0.0.1")
PORT = int(os.environ.get("PDF_TOC_PORT", "8788"))
JOBS_DIR = os.environ.get("PDF_TOC_JOBS_DIR", "/tmp/pdf_toc_jobs")
LIB_DIR = os.environ.get("PDF_TOC_LIB_DIR") or os.path.join(BASE_DIR, "library")
JOB_KEEP_HOURS = 2
MAX_UPLOAD_MB = 300
PDF_EXT = {".pdf"}
IMG_EXT = {".png", ".jpg", ".jpeg"}
DOC_EXT = {".docx", ".doc", ".odt", ".rtf"}          # 需 LibreOffice 转换
ALLOWED_EXT = PDF_EXT | IMG_EXT | DOC_EXT
SOFFICE_TIMEOUT = 180

# ---- 认证三态 ----
AUTH_KEY = os.environ.get("PDF_TOC_ACCESS_KEY", "")   # 单口令（设置后启用）
_users_db = load_users(BASE_DIR)                      # 多用户模式用户表
MULTI_USER = bool(_users_db)                          # users.json 存在 → 多用户
_sessions = {}          # token -> {"username", "role", "exp"}
_job_owner = {}         # job -> username（多用户模式下载鉴权）
SESSION_TTL = 24 * 3600

app = FastAPI(title="PDF 合并 + 目录生成")


def _cur_user(request: Request):
    """从请求取当前 session（未认证模式返回 None）。"""
    tok = request.headers.get("x-auth-token") or request.query_params.get("token")
    s = _sessions.get(tok or "")
    if s and s["exp"] >= time.time():
        return s
    return None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in ("/api/auth", "/api/auth/info", "/api/health"):
        return await call_next(request)
    if AUTH_KEY or MULTI_USER:
        user = _cur_user(request)
        if not user:
            return JSONResponse({"error": "未授权：请先登录"}, status_code=401)
        if MULTI_USER and user.get("username"):
            # 实时校验账号状态：被禁用/删除的用户，已发放的 session 立即失效
            udb = load_users(BASE_DIR)
            cu = udb.get(user["username"])
            if not cu or cu.get("disabled"):
                return JSONResponse({"error": "账号已禁用或不存在"}, status_code=401)
        request.state.user = user
    return await call_next(request)


def _require_admin(request: Request):
    if not MULTI_USER:
        return
    u = _cur_user(request)
    if not u or u.get("role") != "admin":
        raise PermissionError("仅管理员可操作")


# ---------------- 通用工具 ----------------
def sweep_old_jobs():
    now = time.time()
    if not os.path.isdir(JOBS_DIR):
        return
    for d in os.listdir(JOBS_DIR):
        p = os.path.join(JOBS_DIR, d)
        try:
            if os.path.isdir(p) and now - os.path.getmtime(p) > JOB_KEEP_HOURS * 3600:
                shutil.rmtree(p, ignore_errors=True)
                _job_owner.pop(d, None)
        except OSError:
            pass


def convert_to_pdf(src_path, out_dir):
    """用 LibreOffice 把 Word/ODT/RTF 转成 PDF，返回生成的 PDF 路径。

    每次转换用独立的 UserInstallation 临时目录，避免并发时 soffice 抢同一 profile 而失败。
    """
    with tempfile.TemporaryDirectory(prefix="soffice_") as profile:
        cmd = [
            "soffice", f"-env:UserInstallation=file://{profile}",
            "--headless", "--norestore", "--convert-to", "pdf",
            "--outdir", out_dir, src_path,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=SOFFICE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ValueError(f"转换超时（>{SOFFICE_TIMEOUT}s）：{os.path.basename(src_path)}")
    out_pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(src_path))[0] + ".pdf")
    if not os.path.exists(out_pdf):
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        detail = detail[-1] if detail else "未知原因"
        raise ValueError(f"Word 转 PDF 失败：{os.path.basename(src_path)}（{detail}）")
    return out_pdf


def image_to_pdf(src_path, out_pdf):
    """图片转单页 A4 PDF：等比缩放 + 居中，绝不拉伸变形。"""
    img = fitz.open(src_path)
    rect = img[0].rect
    doc = fitz.open()
    page = doc.new_page(width=595.27, height=841.89)
    scale = min(595.27 / rect.width, 841.89 / rect.height)
    w, h = rect.width * scale, rect.height * scale
    x0, y0 = (595.27 - w) / 2, (841.89 - h) / 2      # 居中，保持原始宽高比
    page.show_pdf_page(fitz.Rect(x0, y0, x0 + w, y0 + h), img, 0)
    doc.save(out_pdf)
    doc.close()
    img.close()
    return out_pdf


def ensure_pdf(src_path, out_dir):
    """把任意受支持的文件统一变成 PDF 路径（引擎只接受 PDF）。"""
    ext = os.path.splitext(src_path)[1].lower()
    if ext in PDF_EXT:
        return src_path
    if ext in DOC_EXT:
        return convert_to_pdf(src_path, out_dir)
    if ext in IMG_EXT:
        out = os.path.join(out_dir, os.path.splitext(os.path.basename(src_path))[0] + ".pdf")
        return image_to_pdf(src_path, out)
    raise ValueError(f"不支持的文件类型：{os.path.basename(src_path)}")


def pdf_page_count(path):
    try:
        with fitz.open(path) as d:
            return d.page_count
    except Exception:
        return 0


def safe_name(name):
    """去掉路径与危险字符，保留中文。"""
    name = os.path.basename((name or "").strip())
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


# ---------------- 素材库（多用户按用户名隔离） ----------------
def _lib_dir_for(username):
    """多用户模式：LIB_DIR/<username>/；其他模式：LIB_DIR。username 均来自 users.json 键或 None。"""
    if MULTI_USER and username:
        return os.path.join(LIB_DIR, username)
    return LIB_DIR


def _lib_index_for(username):
    return os.path.join(_lib_dir_for(username), "index.json")


def lib_load(username=None):
    d = _lib_dir_for(username)
    os.makedirs(d, exist_ok=True)
    p = _lib_index_for(username)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def lib_save(items, username=None):
    d = _lib_dir_for(username)
    os.makedirs(d, exist_ok=True)
    p = _lib_index_for(username)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def lib_path(item_id, username=None):
    return os.path.join(_lib_dir_for(username), f"{item_id}.pdf")


# ---------------- 路由 ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/auth/info")
def auth_info():
    """告知前端当前认证模式：none / key / users。"""
    if MULTI_USER:
        return {"mode": "users"}
    if AUTH_KEY:
        return {"mode": "key"}
    return {"mode": "none"}


@app.post("/api/auth")
async def api_auth(payload: dict = Body(...)):
    """登录换 session token。返回 {ok, token, username, role, mode}。"""
    def _issue(username, role):
        token = secrets.token_hex(16)
        _sessions[token] = {"username": username, "role": role,
                            "exp": time.time() + SESSION_TTL}
        for t, s in list(_sessions.items()):      # 顺手清理过期 session
            if s["exp"] < time.time():
                del _sessions[t]
        return {"ok": True, "token": token, "username": username, "role": role}

    if MULTI_USER:
        users = load_users(BASE_DIR)      # 实时读取：admin 面板/CLI 的改动对登录立即生效
        u = authenticate(users, payload.get("username"), payload.get("password"))
        if not u:
            return JSONResponse({"error": "用户名或口令错误（或账号已禁用）"}, status_code=401)
        return _issue(payload["username"], u["role"])
    if AUTH_KEY:
        if payload.get("key") != AUTH_KEY:
            return JSONResponse({"error": "口令错误"}, status_code=401)
        return _issue(None, "admin")
    return {"ok": True, "token": ""}


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    tok = request.headers.get("x-auth-token") or request.query_params.get("token")
    _sessions.pop(tok or "", None)
    return {"ok": True}


# ---------------- 材料库 ----------------
@app.get("/api/library")
def library_list(request: Request):
    u = _cur_user(request)
    return {"items": lib_load(u["username"] if u else None)}


@app.post("/api/library/upload")
async def library_upload(request: Request, files: list[UploadFile] = File(...)):
    """上传到素材库（多用户模式：存进自己的库）。"""
    u = _cur_user(request)
    username = u["username"] if u else None
    items = lib_load(username)
    added, errors = [], []
    for f in files:
        name = safe_name(f.filename)
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            errors.append(f"{name}：不支持的类型")
            continue
        data = await f.read()
        if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
            errors.append(f"{name}：超过 {MAX_UPLOAD_MB}MB")
            continue
        item_id = uuid.uuid4().hex[:12]
        with tempfile.TemporaryDirectory(prefix="libup_") as tmpd:
            raw = os.path.join(tmpd, name)
            with open(raw, "wb") as out:
                out.write(data)
            try:
                pdf = ensure_pdf(raw, tmpd)          # Word/图片统一转 PDF 后入库
                shutil.copy(pdf, lib_path(item_id, username))
            except ValueError as e:
                errors.append(str(e))
                continue
            except Exception as e:
                errors.append(f"{name}：{type(e).__name__}: {e}")
                continue
        entry = {
            "id": item_id,
            "name": os.path.splitext(name)[0],
            "orig_name": name,
            "pages": pdf_page_count(lib_path(item_id, username)),
            "size": os.path.getsize(lib_path(item_id, username)),
            "converted": ext not in PDF_EXT,
            "added_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        items.append(entry)
        added.append(entry)
    lib_save(items, username)
    return {"ok": True, "added": added, "errors": errors, "items": items}


@app.post("/api/library/rename")
async def library_rename(request: Request, item_id: str = Form(...), name: str = Form(...)):
    u = _cur_user(request)
    username = u["username"] if u else None
    items = lib_load(username)
    name = name.strip()
    if not name:
        return JSONResponse({"error": "名称不能为空"}, status_code=400)
    for it in items:
        if it["id"] == item_id:
            it["name"] = name
            lib_save(items, username)
            return {"ok": True, "items": items}
    return JSONResponse({"error": "素材不存在"}, status_code=404)


@app.post("/api/library/delete")
async def library_delete(request: Request, item_id: str = Form(...)):
    u = _cur_user(request)
    username = u["username"] if u else None
    items = lib_load(username)
    keep = [i for i in items if i["id"] != item_id]
    if len(keep) == len(items):
        return JSONResponse({"error": "素材不存在"}, status_code=404)
    try:
        os.remove(lib_path(item_id, username))
    except OSError:
        pass
    lib_save(keep, username)
    return {"ok": True, "items": keep}


@app.get("/api/library/preview/{item_id}")
def library_preview(request: Request, item_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}", item_id):
        return JSONResponse({"error": "非法 id"}, status_code=400)
    u = _cur_user(request)
    username = u["username"] if u else None
    # 多用户模式：条目必须存在于自己的库（或管理员可跨用户预览）
    if MULTI_USER and u["role"] != "admin":
        if not any(i["id"] == item_id for i in lib_load(username)):
            return JSONResponse({"error": "无权访问该素材"}, status_code=403)
    p = lib_path(item_id, username)
    if not os.path.exists(p):
        return JSONResponse({"error": "素材不存在"}, status_code=404)
    return FileResponse(p, media_type="application/pdf", content_disposition_type="inline")


# ---------------- 合并 ----------------
@app.post("/api/merge")
async def api_merge(request: Request, files: list[UploadFile] = File(default=[]),
                    config: str = Form("{}")):
    """
    合并入口。config 里必须带 items，描述合并顺序与来源：
      items: [{"src":"upload","name":"a.pdf"}, {"src":"library","id":"abc123def456"}]
    toc 条目用 {"item": <items 下标>} 引用目标文件，或 {"page": N} 用正文页码。
    """
    sweep_old_jobs()
    u = _cur_user(request)
    username = u["username"] if u else None
    try:
        cfg = json.loads(config)
    except json.JSONDecodeError:
        return JSONResponse({"error": "config 不是合法 JSON"}, status_code=400)

    items = cfg.get("items") or []
    if not items:
        return JSONResponse({"error": "合并列表为空"}, status_code=400)

    uploads, total = {}, 0
    for f in files:
        name = safe_name(f.filename)
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            return JSONResponse({"error": f"不支持的文件类型：{name}"}, status_code=400)
        data = await f.read()
        total += len(data)
        if total > MAX_UPLOAD_MB * 1024 * 1024:
            return JSONResponse({"error": f"上传总大小超过 {MAX_UPLOAD_MB}MB"}, status_code=400)
        uploads[name] = data

    lib = {i["id"]: i for i in lib_load(username)}
    job = uuid.uuid4().hex[:10]
    workdir = os.path.join(JOBS_DIR, job)
    os.makedirs(workdir, exist_ok=True)

    try:
        file_names = []
        for i, it in enumerate(items):
            tag = f"{i:03d}"
            if it.get("src") == "library":
                entry = lib.get(it.get("id"))
                if not entry:
                    raise ValueError(f"素材库中找不到条目（可能已被删除）：{it.get('id')}")
                dest = os.path.join(workdir, f"{tag}_{safe_name(entry['name'])}.pdf")
                shutil.copy(lib_path(entry["id"], username), dest)
            else:
                name = safe_name(it.get("name"))
                if name not in uploads:
                    raise ValueError(f"缺少上传文件：{name}")
                raw = os.path.join(workdir, f"raw_{tag}_{name}")
                with open(raw, "wb") as out:
                    out.write(uploads[name])
                pdf = ensure_pdf(raw, workdir)       # Word/图片统一转 PDF
                dest = os.path.join(workdir, f"{tag}_{os.path.splitext(name)[0]}.pdf")
                os.replace(pdf, dest)
            file_names.append(os.path.basename(dest))

        toc = []
        for e in cfg.get("toc", []):
            row = {"level": e.get("level", 1), "title": e.get("title", "")}
            if "item" in e:
                k = int(e["item"])
                if not 0 <= k < len(file_names):
                    raise ValueError(f"目录条目「{row['title']}」引用了不存在的文件序号 {k}")
                row["file"] = file_names[k]
            elif "page" in e:
                row["page"] = int(e["page"])
            else:
                raise ValueError(f"目录条目「{row['title']}」缺少跳转目标")
            toc.append(row)

        engine_cfg = {
            "title": cfg.get("title", "目录"),
            "numbering": cfg.get("numbering", "physical"),
            "bookmarks": cfg.get("bookmarks", True),
            "lines_per_page": cfg.get("lines_per_page", 24),
            "files": file_names,
            "toc": toc,
        }
        out_path = os.path.join(workdir, "merged.pdf")
        total_pages, toc_pages, n = run_merge(engine_cfg, workdir, out_path)

        comp_info = None
        comp = cfg.get("compress") or {}
        try:
            target_mb = float(comp.get("target_mb", 0))
        except (TypeError, ValueError):
            target_mb = 0
        if target_mb > 0:
            merged_tmp = out_path + ".comp.pdf"
            try:
                r = compress_to_target(out_path, merged_tmp, target_mb)
                os.replace(merged_tmp, out_path)
            except Exception as e:
                shutil.rmtree(workdir, ignore_errors=True)
                return JSONResponse({"error": f"压缩失败：{type(e).__name__}: {e}"}, status_code=500)
            comp_info = {
                "target_mb": target_mb,
                "orig_mb": round(r["orig_bytes"] / 1048576, 2),
                "final_mb": round(r["final_bytes"] / 1048576, 2),
                "dpi": r["dpi"],
                "exceeded": r["exceeded"],
            }

        fname = safe_name(cfg.get("filename") or "").strip()
        fname = re.sub(r"\.pdf$", "", fname, flags=re.I) or "合并文档"
        fname = fname[:120] + ".pdf"
        with open(os.path.join(workdir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"filename": fname}, f, ensure_ascii=False)
    except (ValueError, FileNotFoundError) as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return JSONResponse({"error": f"合并失败：{type(e).__name__}: {e}"}, status_code=500)

    if MULTI_USER:
        _job_owner[job] = username      # 多用户模式记录任务属主，下载时校验
    resp = {"ok": True, "job": job, "pages": total_pages, "toc_pages": toc_pages,
            "entries": n, "filename": fname}
    if comp_info:
        resp["compress"] = comp_info
    return resp


@app.post("/api/probe")
async def api_probe(files: list[UploadFile] = File(...)):
    """探测上传文件的页数（Word 会真实转换一次），供前端显示与页码换算。"""
    out = []
    for f in files:
        name = safe_name(f.filename)
        ext = os.path.splitext(name)[1].lower()
        data = await f.read()
        pages, err = 0, None
        with tempfile.TemporaryDirectory(prefix="probe_") as tmpd:
            raw = os.path.join(tmpd, name)
            with open(raw, "wb") as o:
                o.write(data)
            try:
                pdf = ensure_pdf(raw, tmpd)
                pages = pdf_page_count(pdf)
            except ValueError as e:
                err = str(e)
        out.append({"name": name, "pages": pages, "error": err})
    return {"files": out}


@app.get("/api/download/{job}")
def download(request: Request, job: str, inline: int = 0):
    if not re.fullmatch(r"[0-9a-f]{10}", job):
        return JSONResponse({"error": "非法 job id"}, status_code=400)
    # 多用户模式：仅任务属主或管理员可下载
    if MULTI_USER:
        u = _cur_user(request)
        if not u or (u["role"] != "admin" and _job_owner.get(job) != u["username"]):
            return JSONResponse({"error": "无权访问该任务结果"}, status_code=403)
    d = os.path.join(JOBS_DIR, job)
    p = os.path.join(d, "merged.pdf")
    if not os.path.exists(p):
        return JSONResponse({"error": "结果不存在或已过期"}, status_code=404)
    fname = "合并文档.pdf"
    try:
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
            fname = json.load(f).get("filename") or fname
    except (OSError, json.JSONDecodeError):
        pass
    disposition = "inline" if inline == 1 else "attachment"
    return FileResponse(p, media_type="application/pdf", filename=fname,
                        content_disposition_type=disposition)


# ---------------- 管理员 API（多用户模式） ----------------
@app.get("/api/admin/users")
def admin_users(request: Request):
    try:
        _require_admin(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    users = load_users(BASE_DIR)          # 实时读取（user_admin.py 的修改立即生效）
    out = []
    for name, u in users.items():
        out.append({
            "username": name,
            "role": u.get("role"),
            "disabled": bool(u.get("disabled")),
            "created_at": u.get("created_at"),
            "materials": len(lib_load(name)),
        })
    return {"users": out}


@app.post("/api/admin/users")
async def admin_create(request: Request, payload: dict = Body(...)):
    try:
        _require_admin(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    role = payload.get("role") if payload.get("role") == "admin" else "user"
    if len(password) < 6:
        return JSONResponse({"error": "口令至少 6 位"}, status_code=400)
    users = load_users(BASE_DIR)
    try:
        add_user(users, username, password, role)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    from user_admin import save_users
    save_users(users, BASE_DIR)
    return {"ok": True, "users": users}


@app.post("/api/admin/users/{name}/reset")
async def admin_reset(request: Request, name: str, payload: dict = Body(...)):
    try:
        _require_admin(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    users = load_users(BASE_DIR)
    if name not in users:
        return JSONResponse({"error": "用户不存在"}, status_code=404)
    password = payload.get("password") or ""
    if len(password) < 6:
        return JSONResponse({"error": "口令至少 6 位"}, status_code=400)
    users[name]["hash"] = hash_password(password)
    users[name]["disabled"] = False
    from user_admin import save_users
    save_users(users, BASE_DIR)
    return {"ok": True}


@app.post("/api/admin/users/{name}/disable")
async def admin_disable(request: Request, name: str, payload: dict = Body(...)):
    try:
        _require_admin(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    users = load_users(BASE_DIR)
    if name not in users:
        return JSONResponse({"error": "用户不存在"}, status_code=404)
    if users[name].get("role") == "admin" and payload.get("disabled"):
        return JSONResponse({"error": "不能禁用管理员账号"}, status_code=400)
    users[name]["disabled"] = bool(payload.get("disabled", True))
    from user_admin import save_users
    save_users(users, BASE_DIR)
    return {"ok": True}


@app.post("/api/admin/users/{name}/delete")
async def admin_delete(request: Request, name: str):
    try:
        _require_admin(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    users = load_users(BASE_DIR)
    if name not in users:
        return JSONResponse({"error": "用户不存在"}, status_code=404)
    if users[name].get("role") == "admin":
        return JSONResponse({"error": "不能删除管理员账号"}, status_code=400)
    del users[name]
    from user_admin import save_users
    save_users(users, BASE_DIR)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    os.makedirs(JOBS_DIR, exist_ok=True)
    os.makedirs(LIB_DIR, exist_ok=True)
    uvicorn.run(app, host=BIND_IP, port=PORT, log_level="warning")
