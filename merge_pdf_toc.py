#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_pdf_toc.py — 合并多个 PDF，并在最前面生成一张真实可见的「目录页」。

目录页特性：
  - 大标题 + 分隔线，条目按层级缩进
  - 点线引导符连接标题与页码
  - 整行可点击跳转到对应正文页
  - 中文无需外部字体（使用 PyMuPDF 内置宋体）
  - 可选同时生成 PDF 侧栏书签（默认开启，可关）

用法：
  python3 merge_pdf_toc.py 目录.json [-o 输出.pdf]

配置文件格式（JSON，UTF-8）：
{
  "title": "目录",                // 目录页大标题，缺省为「目录」
  "numbering": "physical",       // 目录里显示的页码口径：
                                 //   "physical" = 合并后的物理页（目录页算第 1 页，与 PDF 阅读器一致）
                                 //   "body"     = 正文第 1 页算 1（目录页不计入）
  "bookmarks": true,             // 是否同时写入侧栏书签（默认 true）
  "lines_per_page": 24,          // 每页目录最多多少条（超出自动分页）
  "files": [                     // 按此顺序合并的文件（相对配置文件的路径）
    "a.pdf",
    "b.pdf"
  ],

  // ===== 自动模式（按文件名生成目录，默认关闭）=====
  "auto_from_files": true,       // true = 每个文件自动生成一条 level 1 条目，标题=文件名（去扩展名）
  "strip_number_prefix": true,   // 标题去掉 "01_"/"02-" 这类数字前缀（只剥「数字+分隔符」，"2024年度总结" 不受影响）
  "title_overrides": {           // 对个别文件改名（自定义修改）
    "b.pdf": "二、本科成绩单"
  },
  "level_overrides": {           // 对个别文件调层级（如附件挂成 2 级，缩进并挂靠上一条 1 级书签）
    "b.pdf": 2
  },
  "exclude_files": ["c.pdf"],    // 仍会合并，但不进自动目录

  // ===== 显式目录条目（两种模式通用，排在自动条目之后）=====
  "toc": [                       // 目录条目，从上到下
    {"level": 1, "title": "一、个人简历", "file": "a.pdf"},   // 指向某文件正文第 1 页
    {"level": 2, "title": "1. 教育经历", "page": 2}          // page = 正文第几页（不含目录页，从 1 起算）
  ]
}

注意：
  - {"file": ...} 的条目跳到该文件在合并结果中的第 1 页
  - {"page": N}   的条目跳到正文第 N 页（正文不含目录页，从 1 起算）
  - 两者同时给出时以 file 为准
  - 目录文字默认不来自文件名；想用文件名就开 auto_from_files，再靠 overrides 自定义
"""

import argparse
import json
import math
import os
import re
import sys

import fitz  # PyMuPDF

A4_W, A4_H = 595.27, 841.89
MARGIN = 60          # 左右页边距
TITLE_SIZE = 22
BASE_FONT_SIZE = 12.5

# ---- 字体：中文用宋体(SimSun)，西文用 Times New Roman ----
# 字体查找顺序：环境变量 PDF_TOC_FONT_DIR → 本模块同目录 fonts/ → 上级目录 fonts/
# → 系统字体回退（Noto Serif CJK / Liberation Serif，度量兼容）。找不到时用 PyMuPDF 内置。
# 字体文件获取：见 fonts/README.md（从你自己装有 Windows 的电脑拷贝，个人使用）。
FONT_DIRS = [d for d in [
    os.environ.get("PDF_TOC_FONT_DIR"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts"),
] if d]
CJK_CANDIDATES = ["simsun.ttc", "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"]
LATIN_CANDIDATES = ["times.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"]


def _find_font(candidates):
    for c in candidates:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        else:
            for d in FONT_DIRS:
                p = os.path.join(d, c)
                if os.path.exists(p):
                    return p
    return None


def _load_fonts():
    cjk_path, latin_path = _find_font(CJK_CANDIDATES), _find_font(LATIN_CANDIDATES)
    cjk = fitz.Font(fontfile=cjk_path) if cjk_path else fitz.Font("china-s")
    latin = fitz.Font(fontfile=latin_path) if latin_path else fitz.Font("tiro")
    return (cjk, cjk_path), (latin, latin_path)


(FONT_CJK, FONT_CJK_PATH), (FONT_LATIN, FONT_LATIN_PATH) = _load_fonts()
# insert_text 用的字体别名（同页复用同一别名不会重复嵌入字体）
ALIAS_CJK, ALIAS_LATIN = "tocsong", "tocroman"


def is_cjk(ch):
    """中日韩文字、全角标点用中文字体；其余（拉丁字母/数字/半角符号）用西文字体。"""
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or      # CJK 部首扩展 ~ 统一汉字
            0xF900 <= o <= 0xFAFF or      # 兼容汉字
            0xFF00 <= o <= 0xFF60 or      # 全角 ASCII
            0xFFE0 <= o <= 0xFFE6 or      # 全角符号
            0x3000 <= o <= 0x303F)        # 中文标点


def split_runs(text):
    """把文本切成 [(片段, Font, 字体文件, 别名), ...]，相邻同类字符合并成一段。"""
    runs = []
    for ch in text:
        cjk = is_cjk(ch)
        if runs and runs[-1][1] is cjk:
            runs[-1][0] += ch
        else:
            runs.append([ch, cjk])
    out = []
    for seg, cjk in runs:
        if cjk:
            out.append((seg, FONT_CJK, FONT_CJK_PATH, ALIAS_CJK))
        else:
            out.append((seg, FONT_LATIN, FONT_LATIN_PATH, ALIAS_LATIN))
    return out


def measure(text, fontsize):
    """混排文本宽度：按字体分段累加。"""
    return sum(f.text_length(s, fontsize=fontsize) for s, f, _, _ in split_runs(text))


def draw_text(page, x, y, text, fontsize):
    """在 (x, y) 基线位置绘制中西文混排文本，返回结束时的 x 坐标。

    用 insert_text 逐段绘制并手动累加宽度。不用 TextWriter：后者在写入时做字形→Unicode
    反查，Times New Roman 里 hyphen(U+002D) 与 soft-hyphen(U+00AD) 共用字形，会被写成
    U+00AD，导致 PDF 内搜索「CET-6」这类含连字符的词失败。
    """
    cur = x
    for seg, font, path, alias in split_runs(text):
        if path:
            page.insert_text(fitz.Point(cur, y), seg, fontname=alias, fontfile=path,
                             fontsize=fontsize)
        else:   # 回退到内置字体（无字体文件）
            page.insert_text(fitz.Point(cur, y), seg,
                             fontname="china-s" if font is FONT_CJK else "tiro",
                             fontsize=fontsize)
        cur += font.text_length(seg, fontsize=fontsize)
    return cur


def fix_tounicode(doc, page_indices):
    """修正 PyMuPDF 嵌入 TTF 时 ToUnicode 表的字形反查错误。

    PyMuPDF 嵌入任何 TTF 子集（实测 Times New Roman、Liberation Serif 均如此）时，
    按字形反查 Unicode 会把 space(U+0020) 记成 NBSP(U+00A0)、hyphen(U+002D) 记成
    SHY(U+00AD)——因为这些码点在字体 cmap 里共用同一字形。后果：PDF 阅读器里搜索
    「CET-6」这类含连字符的词失败，复制文本也会带上不可见的软连字符。
    这里只改目录页所用字体的 ToUnicode，不动合并进来的正文页。
    """
    fixed = 0
    seen = set()
    for pno in page_indices:
        for f in doc[pno].get_fonts(full=True):
            font_xref = f[0]
            if font_xref in seen:
                continue
            seen.add(font_xref)
            try:
                key = doc.xref_get_key(font_xref, "ToUnicode")
                if not key or key[0] != "xref":
                    continue
                tx = int(key[1].split()[0])
                data = doc.xref_stream(tx)
                new = (data.replace(b"<00AD>", b"<002D>").replace(b"<00ad>", b"<002d>")
                           .replace(b"<00A0>", b"<0020>").replace(b"<00a0>", b"<0020>"))
                if new != data:
                    doc.update_stream(tx, new)
                    fixed += 1
            except Exception:
                continue      # 单个字体修不了不影响整体输出
    return fixed


def parse_args():
    ap = argparse.ArgumentParser(description="合并 PDF 并生成可点击跳转的目录页")
    ap.add_argument("config", help="配置文件（JSON）路径")
    ap.add_argument("-o", "--output", help="输出 PDF 路径（缺省：配置同名_合并.pdf）")
    return ap.parse_args()


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def merge_sources(cfg, base_dir):
    """按顺序合并 files 列表，返回 (合并后的文档, 每个文件的正文起始页索引列表)。"""
    body = fitz.open()
    starts = []
    for fn in cfg.get("files", []):
        p = os.path.join(base_dir, fn)
        if not os.path.exists(p):
            raise FileNotFoundError(f"找不到文件 {p}")
        src = fitz.open(p)
        starts.append((fn, body.page_count))
        # links=False/annots=False：源文件内部链接在合并后会错位，干脆不带入
        body.insert_pdf(src, links=False, annots=False)
        src.close()
    return body, starts


def file_start(starts, fn):
    for name, idx in starts:
        if name == fn:
            return idx
    raise ValueError(f"目录引用了未合并的文件：{fn}")


def shrink_to_fit(text, fontsize, max_width):
    """若标题超宽则缩小字号，仍超宽则截断加省略号。返回 (文本, 实际字号)。"""
    size = fontsize
    while size > 7 and measure(text, size) > max_width:
        size -= 0.5
    if measure(text, size) <= max_width:
        return text, size
    while text and measure(text + "…", size) > max_width:
        text = text[:-1]
    return text + "…", size


def draw_dots(page, x0, x1, y):
    """在 (x0,y)-(x1,y) 之间画点状引导线。"""
    x = x0
    while x < x1:
        page.draw_circle(fitz.Point(x, y - 3.2), 0.55,
                         color=(0, 0, 0), fill=(0, 0, 0), width=0.2)
        x += 5


def build_toc_pages(cfg, entries, toc_page_count, body):
    """
    entries: [(level, title, body_index), ...]  body_index = 正文中的页索引（不含目录页）
    目录页里的链接目标物理页 = toc_page_count + body_index

    实现要点：先往 body 末尾追加 toc_page_count 个空白页，等全部页存在后再写文字和链接
    （PyMuPDF 要求链接目标页在创建链接时已存在），最后用 move_page 把目录页移到最前面。
    """
    title = cfg.get("title", "目录")
    per_page = max(1, int(cfg.get("lines_per_page", 24)))
    numbering = cfg.get("numbering", "physical")

    toc_first = body.page_count                       # 目录页临时追加位置（正文之后）
    for _ in range(toc_page_count):                   # 先创建全部空白目录页
        body.new_page(width=A4_W, height=A4_H)

    link_specs = []                                   # 链接稍后再插（见下方说明）
    for pno in range(toc_page_count):
        page = body[toc_first + pno]
        if pno == 0:
            tw = measure(title, TITLE_SIZE)
            draw_text(page, (A4_W - tw) / 2, 100, title, TITLE_SIZE)
            page.draw_line(fitz.Point(MARGIN, 128), fitz.Point(A4_W - MARGIN, 128),
                           color=(0, 0, 0), width=0.8)
        y = 150
        right_x = A4_W - MARGIN
        lo = pno * per_page
        hi = min(len(entries), (pno + 1) * per_page)
        for i in range(lo, hi):
            level, text, body_idx = entries[i]
            indent = 20 * (level - 1)
            x0 = MARGIN + indent
            fontsize = max(9.0, BASE_FONT_SIZE - (level - 1) * 1.2)
            line_h = 26 if level == 1 else 21
            disp = toc_page_count + body_idx + 1 if numbering == "physical" else body_idx + 1
            page_num_text = str(disp)
            num_w = measure(page_num_text, fontsize)
            num_x = right_x - num_w
            max_w = num_x - x0 - 8
            line_text, size = shrink_to_fit(text, fontsize, max_w)
            t_w = measure(line_text, size)
            draw_dots(page, x0 + t_w + 6, num_x - 8, y)
            draw_text(page, x0, y, line_text, size)
            draw_text(page, num_x, y, page_num_text, fontsize)
            # 整行可点击，跳转到正文对应页（先记下，等页面移动完成后再插入：
            # move_page 会重映射已有链接目标，插早了会被改坏）
            target = toc_page_count + body_idx
            rect = fitz.Rect(MARGIN - 10, y - fontsize * 0.9, right_x + 10, y + 8)
            link_specs.append((pno, rect, target))
            y += line_h

    # 把目录页整体移到文档最前面
    for p in range(toc_page_count):
        body.move_page(toc_first, p)

    # 此时页序已定型，再插入链接，目标不会被动过
    for pno, rect, target in link_specs:
        body[pno].insert_link({"kind": fitz.LINK_GOTO, "from": rect, "page": target,
                               "to": fitz.Point(0, 0)})


def build_entries(cfg, starts):
    """按配置生成目录条目列表 [(level, title, body_index), ...]。CLI 与 Web 共用。"""
    entries = []
    if cfg.get("auto_from_files", False):
        # 自动模式：按 files 顺序，每个文件一条 level 1 条目，标题=文件名（去扩展名）
        strip_prefix = cfg.get("strip_number_prefix", False)
        title_ov = cfg.get("title_overrides", {})
        level_ov = cfg.get("level_overrides", {})
        excluded = set(cfg.get("exclude_files", []))
        for fn, idx in starts:
            if fn in excluded:
                continue
            name = title_ov.get(fn)
            if name is None:
                name = os.path.splitext(os.path.basename(fn))[0]
                if strip_prefix:
                    # 只剥「数字+分隔符」前缀（如 01_、02-），"2024年度总结" 不受影响
                    name = re.sub(r"^\d+[\s._\-]+", "", name)
            level = max(1, int(level_ov.get(fn, 1)))
            entries.append((level, str(name), idx))

    # 显式目录条目（自动模式下追加在自动条目之后）
    for e in cfg.get("toc", []):
        level = max(1, int(e.get("level", 1)))
        if "file" in e:
            idx = file_start(starts, e["file"])
        elif "page" in e:
            idx = int(e["page"]) - 1
            if idx < 0:
                raise ValueError(f"目录条目「{e['title']}」的 page 必须 ≥ 1")
        else:
            raise ValueError(f"目录条目「{e.get('title', '?')}」缺少 file 或 page")
        entries.append((level, str(e["title"]), idx))

    if not entries:
        raise ValueError("没有目录条目（files 为空且 toc 为空，或所有文件被 exclude_files 排除）")
    return entries


def run_merge(cfg, base_dir, out_path):
    """执行合并+目录页生成，返回 (总页数, 目录页数, 条目数)。CLI 与 Web 共用。"""
    body, starts = merge_sources(cfg, base_dir)
    entries = build_entries(cfg, starts)

    per_page = max(1, int(cfg.get("lines_per_page", 24)))
    toc_page_count = max(1, math.ceil(len(entries) / per_page))
    build_toc_pages(cfg, entries, toc_page_count, body)   # 生成目录页并移到最前

    if cfg.get("bookmarks", True):
        # 侧栏书签（1-based 物理页码）
        body.set_toc([[lvl, t, toc_page_count + idx + 1] for lvl, t, idx in entries])

    # 修正目录页字体的 ToUnicode（保证 PDF 内可搜索「CET-6」这类含连字符的词）
    fix_tounicode(body, range(toc_page_count))

    body.save(out_path)
    total = body.page_count
    body.close()
    return total, toc_page_count, len(entries)


def main():
    args = parse_args()
    cfg = load_config(args.config)
    base_dir = os.path.dirname(os.path.abspath(args.config))
    out_path = (args.output or cfg.get("output")
                or os.path.join(base_dir,
                                os.path.splitext(os.path.basename(args.config))[0] + "_合并.pdf"))
    try:
        total, toc_pages, n = run_merge(cfg, base_dir, out_path)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(f"错误：{e}")

    print(f"完成：{out_path}")
    print(f"  共 {total} 页（目录 {toc_pages} 页 + 正文 {total - toc_pages} 页），"
          f"目录条目 {n} 条，侧栏书签 {'已生成' if cfg.get('bookmarks', True) else '未生成'}")


if __name__ == "__main__":
    main()
