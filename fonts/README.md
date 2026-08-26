# 字体获取说明

目录页需要两种字体（均不随仓库分发）：

| 用途 | 字体 | 文件名 |
|---|---|---|
| 中文 | 宋体 | `simsun.ttc` |
| 西文 | Times New Roman | `times.ttf` |

## 从你自己的 Windows 电脑拷贝（个人使用）

在 Windows 上用 scp（或直接把文件复制到服务器本目录）：

```bash
mkdir -p fonts && cd fonts
scp user@your-windows-pc:"C:/Windows/Fonts/simsun.ttc" ./simsun.ttc
scp user@your-windows-pc:"C:/Windows/Fonts/times.ttf"  ./times.ttf
```

Windows 的字体目录是 `C:\Windows\Fonts\`（隐藏系统文件，直接在资源管理器地址栏输入即可访问）。

## 字体查找顺序

程序按以下顺序查找字体目录（由 `PDF_TOC_FONT_DIR` 环境变量或自动查找）：

1. 环境变量 `PDF_TOC_FONT_DIR` 指定的目录
2. 本模块同目录 `fonts/`
3. 上级目录 `fonts/`
4. 系统字体回退：Noto Serif CJK SC（替代宋体，衬线风格）/ Liberation Serif（与 Times New Roman 度量兼容）

找不到微软字体时会自动回退到开源替代字体，功能不受影响，仅字形风格略有差异。

## 服务器端 Word 转换字体映射（可选）

如果服务器上用 LibreOffice 转换 Word 文档，建议配置 fontconfig 别名，
把 Windows 常见中文字体名映射到系统已安装的 Noto CJK：

见 [`deploy/69-chinese-alias.conf`](../deploy/69-chinese-alias.conf)，放入 `/etc/fonts/conf.d/` 后执行 `fc-cache -f`。
