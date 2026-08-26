# pdf-merge-toc

把多个 PDF / Word / 图片合并为一个 PDF，自动生成**真实可见、可点击跳转的目录页**，并支持多用户材料库、Word 转换、按目标大小压缩、自动备份到 PC。

面向需要将证明材料（保研、申报、求职等）汇总成单个 PDF 的场景。

## 功能

| 功能 | 说明 |
|---|---|
| 合并 | PDF / Word（docx、doc、odt、rtf，LibreOffice 转换）/ 图片（等比居中转 A4，不拉伸）混合合并 |
| 目录页 | 中文宋体（SimSun）+ 西文 Times New Roman 混排；层级缩进；点线引导；整行可点击跳转；页码右对齐；物理/正文两种页码口径；侧栏书签可选 |
| 目录编辑 | 「按材料名自动生成」（可选剥序号前缀）+ 逐条手动编辑（层级 1-3、标题、跳转到材料首页或正文第 N 页） |
| 材料库 | 上传一次持久保存（Word/图片入库即转 PDF），后续一键加入合并列表；改名/预览/删除 |
| 压缩 | Ghostscript 阶梯降采样（300→220→150→110→96→84→72→60 dpi），自动选「≤ 目标大小」的最高清晰度档；仅压缩图像，文字无损 |
| 多用户 | 账号体系（scrypt 密码哈希、admin/普通用户、禁用/重置/删除）；材料库、任务下载、PC 备份全部按用户隔离；用户自助改密 |
| 备份 | 合并产物自动 scp 到 Windows PC（按用户名分文件夹）；PC 离线时服务器保留 6 小时，daemon 每分钟探测、上线自动补备份；前端全程静默 |
| 自定义 | 目录大标题、输出文件名（中文名正常）、压缩目标大小 |

## 架构与数据流

```
浏览器（Tailscale 内网 / 公网 nginx 443）
   │  HTTP
   ▼
nginx（可选：HTTPS 终结 + 登录限流）──反代──► FastAPI (app.py, 端口 8788)
   ├── merge_pdf_toc.py   合并引擎（目录页、字体混排、书签、ToUnicode 修复）
   ├── compress.py        Ghostscript 压缩
   ├── user_admin.py      多用户管理（users.json）
   ├── backup.py          后台备份线程 + daemon
   └── 外部程序            soffice（Word→PDF）、gs（压缩）
```

一次「生成」的数据旅程：

1. 前端 POST `/api/merge`（files + config JSON）
2. 后端开任务目录 `/tmp/pdf_toc_jobs/<10位job>/`（按 `PDF_TOC_JOBS_DIR` 可配）
3. 统一转 PDF：Word 走 soffice（独立 UserInstallation 防并发锁）、图片转单页 A4
4. `merge_pdf_toc.py` 合并 → 生成目录页 → 移到最前 → 插链接 → 书签 → 修正 ToUnicode（保证"GPA 3.85"这类可搜索）
5. 可选压缩到目标大小
6. 响应立即返回（job id、页数、条目数、压缩信息）；浏览器下载/内嵌预览
7. 后台线程 scp 备份到 PC（3 次重试/90 秒）；失败进队列，daemon 每分钟探测 PC 上线补备份
8. 任务清理：备份完成的 2 小时、等待补备份的 6 小时

## 目录结构

```
├── app.py               # FastAPI 主服务（路由、认证中间件、任务清理）
├── merge_pdf_toc.py     # 合并引擎（也可独立 CLI 使用）
├── compress.py          # Ghostscript 压缩模块（也可独立 CLI 使用）
├── user_admin.py        # 多用户管理（app 导入复用 + CLI）
├── backup.py            # PC 备份（快速路径 + 周期 daemon）
├── static/index.html    # 单页前端（无框架，原生 JS）
├── library/             # 材料库（运行期生成；多用户模式按 <用户名>/ 分目录）
├── users.json           # 用户表（不存在 = 无认证模式；运行期生成）
├── fonts/               # 字体（自备，见下文）
└── deploy/pdf-toc-web.service  # systemd 单元模板
```

## 部署

### 依赖

- Python 3.9+，`pip install -r requirements.txt`（fastapi、uvicorn、pymupdf）
- `libreoffice-writer`（Word 转换；仅装 core/draw 不行，docx 会静默失败）
- `ghostscript`（压缩）
- `nginx` + `certbot`（可选，公网 HTTPS）
- 中文环境：Noto CJK / 文泉驿等（Word 转 PDF 用）

### 字体（目录页：中文宋体 + 西文 Times New Roman）

两种字体均为微软版权字体，**不随仓库分发**。个人使用可从自己的 Windows 拷贝：

```bash
mkdir -p fonts
scp win-pc:"C:/Windows/Fonts/simsun.ttc" fonts/   # 中文宋体
scp win-pc:"C:/Windows/Fonts/times.ttf"  fonts/   # Times New Roman
```

缺字体时按顺序回退：Noto Serif CJK / Liberation Serif（度量兼容）/ PyMuPDF 内置。字体查找顺序：环境变量 `PDF_TOC_FONT_DIR` → 项目 `fonts/` 目录。

### 启动

```bash
# 开发模式（仅本机回环）
python3 app.py

# 生产：systemd（模板见 deploy/，按需改 IP/目录）
systemctl enable --now pdf-toc-web
```

### 环境变量（全部可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PDF_TOC_BIND_IP` | 127.0.0.1 | 监听地址。**只绑可信网络**（Tailscale IP / 127.0.0.1）；公网请走 nginx 反代 |
| `PDF_TOC_PORT` | 8788 | 端口 |
| `PDF_TOC_ACCESS_KEY` | 空 | 单口令模式（所有用户共享一个口令） |
| `PDF_TOC_JOBS_DIR` | /tmp/pdf_toc_jobs | 任务目录 |
| `PDF_TOC_LIB_DIR` | ./library | 材料库目录 |
| `PDF_TOC_FONT_DIR` | 空 | 字体目录（优先级最高） |
| `PDF_TOC_BACKUP_TARGET` | 空 | PC 备份目标，形如 `win-pc:C:/Users/xxx/Desktop/备份目录`；不设则不启用 |

### 认证三态（按配置自动切换）

1. **无认证**：不设 ACCESS_KEY 且无 users.json —— 仅限私用（绑可信网络）
2. **单口令**：设 `PDF_TOC_ACCESS_KEY` —— 所有人共享口令、共享材料库
3. **多用户**：存在 users.json —— 每人独立材料库/任务/备份，admin 可管理用户

### 启用多用户

```bash
python3 user_admin.py init <管理员用户名> <密码>     # 初始化并创建 admin
python3 user_admin.py add <用户名> [密码]            # 加用户（密码缺省随机生成）
python3 user_admin.py list                          # 列出用户
python3 user_admin.py reset <用户名> <新密码>         # 重置（用户忘密码）
python3 user_admin.py disable|enable <用户名>         # 禁用/恢复（禁用=踢人，保留数据）
python3 user_admin.py delete <用户名>                # 删除（材料库保留）
# 之后重启服务生效；或通过 admin 登录后的管理面板操作
```

### 公网 HTTPS（nginx 反代）

```nginx
# 参考部署：nginx 443 终结 TLS → 反代 http://<BIND_IP>:8788
# deploy/ 目录提供完整配置模板要点：
#   - 登录端点限流：limit_req_zone rate=5r/m（防口令爆破）
#   - client_max_body_size 300m
#   - 证书：Let's Encrypt（DNS-01 manual 签发需自行续期）
```

⚠️ 已知坑：nginx 1.18 + OpenSSL 3.0 对部分客户端 TLS 1.3 握手报 `bad key share`，需 `ssl_protocols TLSv1.2;`。Ubuntu 的 certbot 若被 pip 的 cryptography 阴影炸掉，可用剥离 `sys.path` 的 wrapper 运行。

## 配置参考

### 合并 config（POST /api/merge）

```json
{
  "title": "申报材料目录",
  "filename": "卓志勇-保研申请材料",
  "numbering": "physical",
  "bookmarks": true,
  "compress": {"target_mb": 20},
  "items": [
    {"src": "upload", "name": "简历.pdf"},
    {"src": "library", "id": "abc123def456"}
  ],
  "toc": [
    {"level": 1, "title": "一、个人简历", "item": 0},
    {"level": 2, "title": "1. 教育经历", "page": 3}
  ]
}
```

- `items`：合并顺序与来源（upload = 本次上传，library = 材料库条目）
- `toc[].item` = items 下标（跳材料首页）；`toc[].page` = 正文第 N 页
- `numbering`：`physical`（目录页算第 1 页，与阅读器一致）/ `body`（正文第 1 页算 1）
- `compress.target_mb`：>0 时压缩到目标大小

### 引擎 CLI（merge_pdf_toc.py，可独立使用）

```bash
python3 merge_pdf_toc.py config.json [-o 输出.pdf]
```

配置文件另支持：`auto_from_files`（按文件名自动生成目录）、`strip_number_prefix`（剥 `01_` 类前缀）、`title_overrides` / `level_overrides`（逐文件覆盖标题/层级）、`exclude_files`、`lines_per_page`。

## API 端点

| 端点 | 说明 |
|---|---|
| `GET /` | 前端页面 |
| `GET /api/health` | 健康检查 |
| `POST /api/auth` · `/logout` · `/password` · `GET /api/auth/info` | 登录/登出/自助改密/认证模式 |
| `GET/POST /api/library` 系 | 材料库：列表/上传/改名/删除/预览 |
| `POST /api/merge` | 合并入口 |
| `POST /api/probe` | 探测上传文件页数（Word 真实转换一次） |
| `GET /api/download/{job}` | 下载结果（`?inline=1` 内嵌预览） |
| `GET /api/jobs/{job}/backup` | 备份状态（前端静默，供调试） |
| `GET/POST /api/admin/users` 系 | 管理用户（仅 admin，多用户模式） |

## 运维

- **服务**：`systemctl status pdf-toc-web`；任务目录 2/6 小时双轨自动清理
- **备份验证**：`backup.json` 在任务目录里记状态与去向；PC 落点 `<备份根>/<用户名>/<文件名>`（无用户名为 `private/`）
- **日志**：`journalctl -u pdf-toc-web`
- **证书续期**：Let's Encrypt DNS-01 manual 证书不会自动续期，到期前重跑签发流程

## 安全设计

- 无认证模式只应绑 127.0.0.1/可信内网；公网开放务必启用单口令或多用户
- 密码 scrypt 加盐哈希（标准库实现，零第三方依赖）；会话 24h；改密后其他会话失效
- 多用户下材料库、任务下载、备份目录全部按用户隔离；被禁用账号的会话即时失效
- 上传限 300MB、文件名过滤路径穿越字符、job id 10 位随机 hex
- 公网建议 nginx：HTTPS + 登录端点限流（5 req/min/IP）

## 已知限制

- **Word 转换排版漂移**：服务器字体与 Windows 字宽度量不同，换行/分页可能变化。版式要求严格的材料（如单页推荐信）请先在 Word 里「另存为 PDF」再上传
- **压缩**只动图像不动文字；压到最低档仍超标时界面会提示拆分文档
- 合并引擎用 PyMuPDF（AGPL/商业双许可），自用不受影响，分发请留意

## License

MIT（依赖：PyMuPDF AGPL、字体自备）
