# pdf-merge-toc

把多个 PDF / Word / 图片合并为一个 PDF，并自动生成**真实可见、可点击跳转的目录页**。

面向需要将证明材料（保研、申报、求职等）汇总成单个 PDF 的场景，一站式解决：合并、目录、Word 转换、材料复用、大小压缩。

## 功能

- **合并**：PDF / Word（docx、doc、odt、rtf）/ 图片（PNG、JPG）混合合并，顺序可调
- **目录页**：自动生成印刷式目录页（大标题、层级缩进、点状引导线、页码），**整行可点击跳转**到对应正文页；支持「按材料名自动生成」+ 逐条自定义（层级 1-3 级、跳转目标=材料首页或正文第 N 页）
- **目录页字体**：中文宋体（SimSun）+ 西文 Times New Roman，中西文混排基线对齐；
- **常用材料库**：上传一次持久保存，之后一键「加入合并」，无需重复上传；支持改名/删除/预览
- **Word 转换**：服务器端 LibreOffice 转 PDF（含中文字体映射配置）
- **压缩到目标大小**：基于 Ghostscript，在 300→60dpi 阶梯上自动选择「≤ 目标大小」的最高清晰度档（如系统要求 ≤20MB，填 20 即可）；压缩后链接/书签/搜索/字体完整性不受影响
- **自定义输出文件名**（含中文名，RFC 5987 编码）

## 技术栈

- 后端：FastAPI + uvicorn
- PDF 引擎：PyMuPDF（合并、目录页绘制、链接、书签）
- Word/图片转换：LibreOffice headless（独立 user profile 防并发冲突）
- 压缩：Ghostscript pdfwrite（`-dDownsampleThreshold=1.0` + 分辨率阶梯）

## 快速开始

### 依赖

- Python 3.9+
- `pip install pymupdf fastapi uvicorn python-multipart`
- LibreOffice（含 **writer** 组件；只有 core/draw/impress 时 docx 转换会静默失败）：
  `apt install libreoffice-writer`
- Ghostscript：`apt install ghostscript`
- 字体（可选，见 [fonts/README.md](fonts/README.md)）

### 运行

```bash
git clone https://github.com/<your-name>/pdf-merge-toc.git
cd pdf-merge-toc
pip install -r requirements.txt
# 默认只监听 127.0.0.1:8788，通过环境变量改绑定地址
PDF_TOC_BIND_IP=100.x.y.z python3 app.py     # 例：绑定 Tailscale 网卡 IP
```

浏览器打开 `http://<bind-ip>:8788/` 即可使用。

### systemd 部署

```bash
sudo cp deploy/pdf-toc-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pdf-toc-web
```

服务模板默认从环境变量读取配置，按需修改 `Environment=` 行。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PDF_TOC_BIND_IP` | `127.0.0.1` | 监听地址。**只绑可信网络**（127.0.0.1 或 Tailscale/内网 IP） |
| `PDF_TOC_PORT` | `8788` | 监听端口 |
| `PDF_TOC_LIB_DIR` | `<项目目录>/library` | 材料库持久目录 |
| `PDF_TOC_JOBS_DIR` | `/tmp/pdf_toc_jobs` | 临时任务目录（2 小时自动清理） |
| `PDF_TOC_FONT_DIR` | 自动查找 | 宋体/Times New Roman 字体目录，见 fonts/README.md |
| `PDF_TOC_ACCESS_KEY` | 空（不启用） | 访问口令；设置后所有 API 需 session token，公网开放实例**务必设置** |

## 双实例部署（私用 + 公用）

同一套代码可部署多个互相隔离的实例（独立目录 = 独立材料库）：

```bash
# 私用实例：只绑 Tailscale/内网 IP，不设口令
PDF_TOC_BIND_IP=100.x.y.z PDF_TOC_PORT=8788 python3 app.py

# 公用实例：绑 0.0.0.0 + 设置访问口令（公网任何人可访问但需口令）
PDF_TOC_BIND_IP=0.0.0.0 PDF_TOC_PORT=8789 PDF_TOC_ACCESS_KEY=你的口令 python3 app.py
```

口令门机制：前端弹窗输口令 → 后端校验后发放 24h session token（内存存储）→ 所有 API 与下载链接需携带 token；无 token 访问一律 401（含下载直链）。

## 多用户模式（每人独立材料库 + 管理员面板）

实例目录存在 `users.json` 时自动进入多用户模式（优先级高于单口令）：

```bash
# 1. 初始化用户库并创建管理员
python3 user_admin.py init admin 管理员口令
# 2. 添加用户（口令缺省自动生成）
python3 user_admin.py add zhangsan
# 3. 更多 CLI：list / reset / disable / enable / delete
python3 user_admin.py --help

# 4. 启动（绑定 0.0.0.0 对外提供）
PDF_TOC_BIND_IP=0.0.0.0 PDF_TOC_PORT=8789 python3 app.py
```

特性：
- **每人独立库**：`library/<username>/`，登录后只能读写自己的材料，互不可见
- **登录**：用户名 + 口令（scrypt 加盐哈希存储，不存明文）
- **管理员面板**（admin 登录后可见）：用户列表（含材料数）、创建用户、重置口令、禁用/恢复（**禁用即刻生效**，已发放的 session 也会被拦截）、删除账号
- **任务隔离**：合并产物按属主校验，他人无法下载你的 job
- 密码与账号操作也可用 `user_admin.py` 在服务器上直接管理

认证模式优先级：`users.json 存在`（多用户）> `PDF_TOC_ACCESS_KEY`（单口令）> 无认证。

## 安全说明

- 服务**无内置认证**，请只绑定在可信网络内（如 Tailscale 网卡 IP），切勿用 0.0.0.0 暴露公网
- 材料库中的个人材料仅存于你自己的服务器 `library/` 目录，不随仓库分发；注意备份与访问控制
- 上传大小限制 300MB，文件名做路径穿越防护

## 已知限制

- **Word 转换排版可能有差异**：服务器字体与 Windows 字体字宽度量不同，换行/分页位置可能变化。对版式要求严格的文档（如单页推荐信），建议在 Word 里「另存为 PDF」后上传
- 压缩只降低扫描图像分辨率（文字不受影响）；压到最低档仍超目标时，前端会提示拆分文档
- 同一文件在合并列表中不可重复出现（重名拒绝）

## 许可

本项目代码以 [MIT License](LICENSE) 发布。

注意依赖许可：
- [PyMuPDF](https://github.com/pymupdf/PyMuPDF) 为 AGPL-3.0 / 商业双许可
- SimSun / Times New Roman 字体为微软版权字体，**不随本仓库分发**，请从你自己的 Windows 系统拷贝用于个人用途（见 fonts/README.md）
