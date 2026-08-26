#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf-toc-web — PDF 合并 + 目录页生成器的 Web 服务。

功能：
- 上传 PDF / 图片 / Word（docx/doc，服务器端 LibreOffice 转 PDF）合并
- 常用材料库：上传一次持久保存，后续点击加入合并列表
- 生成可点击跳转的目录页（中文宋体 + 西文 Times New Roman）
- 可选压缩到目标大小（Ghostscript 自动选最高可用清晰度）

安全说明：
- 本服务无内置认证，**请只绑定在可信网络内**（如 Tailscale 网卡 IP 或 127.0.0.1），
  不要绑定 0.0.0.0 暴露到公网
- 临时任务存 <job_dir>/<job>/，2 小时后自动清理
- 素材库持久存 <library_dir>/（不随任务清理，含个人材料，注意备份与访问控制）
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

# 监听地址与端口：通过环境变量配置（systemd 单元里设置），默认仅本机回环
BIND_IP = os.environ.get("PDF_TOC_BIND_IP", "127.0.0.1")
PORT = int(os.environ.get("PDF_TOC_PORT", "8788"))
JOBS_DIR = os.environ.get("PDF_TOC_JOBS_DIR", "/tmp/pdf_toc_jobs")
LIB_DIR = os.environ.get("PDF_TOC_LIB_DIR") or os.path.join(BASE_DIR, "library")
LIB_INDEX = os.path.join(LIB_DIR, "index.json")
JOB_KEEP_HOURS = 2
MAX_UPLOAD_MB = 300
PDF_EXT = {".pdf"}
IMG_EXT = {".png", ".jpg", ".jpeg"}
DOC_EXT = {".docx", ".doc", ".odt", ".rtf"}          # 需 LibreOffice 转换
ALLOWED_EXT = PDF_EXT | IMG_EXT | DOC_EXT
SOFFICE_TIMEOUT = 180

# ---- 访问口令门（可选）----
# 设置环境变量 PDF_TOC_ACCESS_KEY 后启用：/api/auth 用口令换 session token，
# 所有 /api/* 请求必须带 token。公网开放实例务必设置；私用(Tailscale)实例可不设。
AUTH_KEY = os.environ.get("PDF_TOC_ACCESS_KEY", "")
_sessions = {}                    # token -> 过期时间戳
SESSION_TTL = 24 * 3600           # session 24 小时有效

app = FastAPI(title="PDF 合并 + 目录生成")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not AUTH_KEY:
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and path != "/api/auth":
        tok = request.headers.get("x-auth-token") or request.query_params.get("token")
        if not tok or _sessions.get(tok, 0) < time.time():
            return JSONResponse({"error": "未授权：需要访问口令"}, status_code=401)
    return await call_next(request)


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
    """把任意受支持的文件统一变成 PDF 路径（引擎只接受 PDF）。

    .pdf        → 原样返回
    Word/ODT/RTF→ LibreOffice 转换
    图片        → 等比居中单页 PDF
    """
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


# ---------------- 素材库 ----------------
def lib_load():
    os.makedirs(LIB_DIR, exist_ok=True)
    if not os.path.exists(LIB_INDEX):
        return []
    try:
        with open(LIB_INDEX, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def lib_save(items):
    os.makedirs(LIB_DIR, exist_ok=True)
    tmp = LIB_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LIB_INDEX)


def lib_path(item_id):
    return os.path.join(LIB_DIR, f"{item_id}.pdf")


# ---------------- 路由 ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/auth")
async def api_auth(payload: dict = Body(...)):
    """口令换 session token。未设置 PDF_TOC_ACCESS_KEY 时直接放行。"""
    if not AUTH_KEY:
        return {"ok": True, "token": ""}
    if payload.get("key") != AUTH_KEY:
        return JSONResponse({"error": "口令错误"}, status_code=401)
    token = secrets.token_hex(16)
    _sessions[token] = time.time() + SESSION_TTL
    # 顺手清理过期 session
    for t, exp in list(_sessions.items()):
        if exp < time.time():
            del _sessions[t]
    return {"ok": True, "token": token}


@app.get("/api/library")
def library_list():
    return {"items": lib_load()}


@app.post("/api/library/upload")
async def library_upload(files: list[UploadFile] = File(...)):
    """上传到素材库：Word 立即转 PDF 后入库，所有条目统一以 PDF 形式持久保存。"""
    items = lib_load()
    added, errors = [], []
    os.makedirs(LIB_DIR, exist_ok=True)
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
                shutil.copy(pdf, lib_path(item_id))
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
            "pages": pdf_page_count(lib_path(item_id)),
            "size": os.path.getsize(lib_path(item_id)),
            "converted": ext not in PDF_EXT,
            "added_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        items.append(entry)
        added.append(entry)
    lib_save(items)
    return {"ok": True, "added": added, "errors": errors, "items": items}


@app.post("/api/library/rename")
async def library_rename(item_id: str = Form(...), name: str = Form(...)):
    items = lib_load()
    name = name.strip()
    if not name:
        return JSONResponse({"error": "名称不能为空"}, status_code=400)
    for it in items:
        if it["id"] == item_id:
            it["name"] = name
            lib_save(items)
            return {"ok": True, "items": items}
    return JSONResponse({"error": "素材不存在"}, status_code=404)


@app.post("/api/library/delete")
async def library_delete(item_id: str = Form(...)):
    items = lib_load()
    keep = [i for i in items if i["id"] != item_id]
    if len(keep) == len(items):
        return JSONResponse({"error": "素材不存在"}, status_code=404)
    try:
        os.remove(lib_path(item_id))
    except OSError:
        pass
    lib_save(keep)
    return {"ok": True, "items": keep}


@app.get("/api/library/preview/{item_id}")
def library_preview(item_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}", item_id):
        return JSONResponse({"error": "非法 id"}, status_code=400)
    p = lib_path(item_id)
    if not os.path.exists(p):
        return JSONResponse({"error": "素材不存在"}, status_code=404)
    return FileResponse(p, media_type="application/pdf", content_disposition_type="inline")


@app.post("/api/merge")
async def api_merge(files: list[UploadFile] = File(default=[]), config: str = Form("{}")):
    """
    合并入口。config 里必须带 items，描述合并顺序与来源：
      items: [{"src":"upload","name":"a.pdf"}, {"src":"library","id":"abc123def456"}]
    toc 条目用 {"item": <items 下标>} 引用目标文件，或 {"page": N} 用正文页码。
    """
    sweep_old_jobs()
    try:
        cfg = json.loads(config)
    except json.JSONDecodeError:
        return JSONResponse({"error": "config 不是合法 JSON"}, status_code=400)

    items = cfg.get("items") or []
    if not items:
        return JSONResponse({"error": "合并列表为空"}, status_code=400)

    # 上传的文件读入内存（UploadFile 流只能读一次）
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

    lib = {i["id"]: i for i in lib_load()}
    job = uuid.uuid4().hex[:10]
    workdir = os.path.join(JOBS_DIR, job)
    os.makedirs(workdir, exist_ok=True)

    try:
        # 按 items 顺序落盘，统一命名 NNN_名称.pdf 保证唯一且有序
        file_names = []
        for i, it in enumerate(items):
            tag = f"{i:03d}"
            if it.get("src") == "library":
                entry = lib.get(it.get("id"))
                if not entry:
                    raise ValueError(f"素材库中找不到条目（可能已被删除）：{it.get('id')}")
                dest = os.path.join(workdir, f"{tag}_{safe_name(entry['name'])}.pdf")
                shutil.copy(lib_path(entry["id"]), dest)
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

        # toc 里的 item 下标 → 实际文件名
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

        # 可选压缩：压到目标大小（cfg.compress.target_mb），自动选最高可用清晰度
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

        # 自定义输出文件名（默认「合并文档」）；过滤非法字符，保证 .pdf 后缀
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
def download(job: str, inline: int = 0):
    if not re.fullmatch(r"[0-9a-f]{10}", job):
        return JSONResponse({"error": "非法 job id"}, status_code=400)
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
    # FileResponse 会按 RFC 5987 对中文文件名做 filename* 编码
    return FileResponse(p, media_type="application/pdf", filename=fname,
                        content_disposition_type=disposition)


if __name__ == "__main__":
    import uvicorn
    os.makedirs(JOBS_DIR, exist_ok=True)
    os.makedirs(LIB_DIR, exist_ok=True)
    uvicorn.run(app, host=BIND_IP, port=PORT, log_level="warning")
