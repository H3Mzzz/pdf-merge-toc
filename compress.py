#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compress.py — 基于 Ghostscript 的 PDF 压缩，支持「压缩到目标大小」。

核心思路：
- 材料类 PDF 体积大头几乎都是扫描图像。按图像分辨率阶梯降采样（300→220→150→110→96→84→72→60 dpi），
  选第一个「≤ 目标大小」的档位 = 在满足大小限制的前提下保留最高清晰度。
- 黑白图（Mono）分辨率保持 min(300, 2×dpi)，保护证书文字的清晰度。
- 文本型 PDF 收益有限（图像降采样帮不上文字），60dpi 仍超标时给出「建议拆分」提示。

用法（CLI）：
  python3 compress.py 输入.pdf 输出.pdf [目标MB] [JPEG质量]
"""
import os
import shutil
import subprocess
import sys
import tempfile

LADDER = [300, 220, 150, 110, 96, 84, 72, 60]
GS_TIMEOUT = 300
DEFAULT_JPEGQ = 75


def max_image_dpi(src):
    """估算源 PDF 里图像的最大等效分辨率（dpi），用于跳过无效的高档位。"""
    try:
        import fitz
        doc = fitz.open(src)
        maxd = 0
        for pno in range(doc.page_count):
            for img in doc[pno].get_images(full=True):
                w = img[2]                     # 像素宽
                maxd = max(maxd, w / 8.27)     # 假设图像约占满 A4 宽(8.27in)
        doc.close()
        return maxd
    except Exception:
        return 0


def gs_compress(src, dst, dpi, jpegq=DEFAULT_JPEGQ):
    """用 Ghostscript 把 src 压缩到 dst。返回 (成功, 输出大小或错误信息)。"""
    mono_dpi = min(300, dpi * 2)
    cmd = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
        "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-dColorImageResolution={dpi}",
        f"-dGrayImageResolution={dpi}",
        f"-dMonoImageResolution={mono_dpi}",
        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",
        # 阈值 1.0：只要源图分辨率 > 目标档位就降采样（默认 1.5 会让中分辨率图漏网）
        "-dColorImageDownsampleThreshold=1.0",
        "-dGrayImageDownsampleThreshold=1.0",
        "-dMonoImageDownsampleThreshold=1.0",
        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dMonoImageDownsampleType=/Subsample",
        "-dAutoRotatePages=/None",
        "-dDetectDuplicateImages=true",
        f"-dJPEGQ={jpegq}",
        f"-sOutputFile={dst}", src,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=GS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"Ghostscript 压缩超时（>{GS_TIMEOUT}s）"
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        return False, err[-1] if err else "gs 退出码 " + str(r.returncode)
    return True, os.path.getsize(dst)


def compress_to_target(src, dst, target_mb, jpegq=DEFAULT_JPEGQ):
    """
    压缩 src 到 dst，目标是 ≤ target_mb MB。
    返回 dict：{compressed: bool, dpi: int|None, orig_bytes, final_bytes, exceeded: bool}
    """
    target_bytes = int(target_mb * 1024 * 1024)
    orig = os.path.getsize(src)
    result = {
        "compressed": False, "dpi": None,
        "orig_bytes": orig, "final_bytes": orig, "exceeded": False,
    }
    if orig <= target_bytes:
        # 已经达标，原样复制，不损失质量
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy(src, dst)
        return result

    # 完整阶梯：高档位即使不触发降采样，JPEG 重编码（Q92→Q75）本身也有收益且无损，
    # 所以每一档都值得试，选「第一个 ≤ 目标大小」的档 = 满足限制下的最高质量
    tmp = os.path.join(tempfile.mkdtemp(prefix="gs_"), "out.pdf")
    best = None                      # (dpi, size) 最后一个成功结果
    try:
        for dpi in LADDER:
            ok, size_or_err = gs_compress(src, tmp, dpi, jpegq)
            if not ok:
                continue             # 单档失败：跳过
            best = (dpi, size_or_err)
            if size_or_err <= target_bytes:
                shutil.copy(tmp, dst)
                result.update(compressed=True, dpi=dpi, final_bytes=size_or_err)
                return result
        # 所有候选档都超标：输出最低档结果（如果它成功过），并标记 exceeded
        if best:
            shutil.copy(tmp, dst)
            result.update(compressed=True, dpi=best[0], final_bytes=best[1],
                          exceeded=True)
        else:
            shutil.copy(src, dst)    # gs 完全失败：原样输出保底
            result["exceeded"] = True
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    target_mb = float(sys.argv[3]) if len(sys.argv) > 3 else 20
    jpegq = int(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_JPEGQ
    r = compress_to_target(src, dst, target_mb, jpegq)
    print(f"原大小: {r['orig_bytes']/1048576:.1f} MB")
    if not r["compressed"]:
        print("无需压缩（已在目标大小内）")
    else:
        print(f"压缩到 {r['dpi']}dpi: {r['final_bytes']/1048576:.1f} MB "
              f"(-{(1 - r['final_bytes']/r['orig_bytes'])*100:.1f}%)")
        if r["exceeded"]:
            print(f"⚠ 警告：最低档（{LADDER[-1]}dpi）仍超过 {target_mb}MB，建议拆分文档或减少扫描页")
