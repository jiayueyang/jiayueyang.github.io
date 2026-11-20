# PaperTranslate

PaperTranslate 是一个为数学和理论物理论文设计的中英双语阅读器。点一句话即可从该句开始朗读，点公式可以查看原始 LaTeX、听读音、看逐步推导或追问。

[打开公开阅读器](https://jiayueyang.github.io/PaperTranslate/) · [在 GitHub Codespaces 启动完整应用](https://codespaces.new/jiayueyang/jiayueyang.github.io)

## 两种使用方式

| 功能 | 公开阅读器 | 个人工作台 |
| --- | --- | --- |
| 阅读《黑洞的霍金温度》、双语对照、书签、搜索、调节字号 | 支持 | 支持 |
| 示例公式推导与论文导读 | 内置讲解，可返回对应原文 | 可以调用模型并追问 |
| 逐句朗读、跟读高亮 | 使用浏览器提供的语音 | 使用 OpenAI 生成并缓存逐句 MP3 |
| 下载示例中文、双语、原始 PDF 和 Markdown | 支持 | 支持 |
| 导入自己的 arXiv、DOI、公开 PDF 链接及上传 PDF | | 支持 |
| 翻译、公式推导、整篇或分节语音导出 | | 配置服务器端 OpenAI API 密钥后可用 |

公开站只包含随项目附带的双语教学笔记。私有工作台将导入的论文、译文、生成语音与进度存放在自己的数据目录。调用 AI 时，服务器会将生成该次翻译、讲解或语音所需的文本发给 OpenAI。费用由配置密钥的账户承担。

## 在 GitHub 中使用

1. 在 GitHub 中打开 **Settings → Codespaces → Secrets**，添加名为 `OPENAI_API_KEY` 的 Codespaces secret，并给 `jiayueyang/jiayueyang.github.io` 仓库读取权限。已经创建的 Codespace 需要停止后重新启动以读取新增的 secret。
2. 点击上方 **在 GitHub Codespaces 启动完整应用**，选择 **PaperTranslate** 配置。首次启动会安装依赖。GitHub 按账号的 Codespaces 配额计量使用。
3. 在 **Ports** 面板打开 **8000 · PaperTranslate**，端口可见性保持 **Private**。用不到时可停止 Codespace。已经保存的论文位于工作区的 `PaperTranslate/data/`。
4. 在工作台粘贴公开论文链接，或直接上传 PDF。首次翻译等待任务提取原文，之后可阅读已完成的段落。翻译完成后，在“导出”菜单下载文件。

Codespaces 配置通过 `PT_TRUST_PRIVATE_CODESPACE=1` 信任该 Codespace 的专属转发域名。若要开放给其他人，先关闭该选项并设置 `PT_ACCESS_TOKEN`。本应用是单用户工作台，共用访问口令的读者会看到同一份论文库。GitHub Codespaces 的运行时长与端口说明见[官方文档](https://docs.github.com/en/codespaces/developing-in-a-codespace/forwarding-ports-in-your-codespace)。

## 本机运行

需要 Python 3.12、FFmpeg 和 Linux 或 macOS。Windows 可用 Docker Desktop。先取得仓库并进入项目目录。

```bash
git clone https://github.com/jiayueyang/jiayueyang.github.io.git
cd jiayueyang.github.io/PaperTranslate
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

用本机编辑器打开 `.env`，填写自己的 `OPENAI_API_KEY`。然后在同一个终端运行：

```bash
set -a
. ./.env
set +a
python -m uvicorn papertranslate.api:app --host 127.0.0.1 --port 8000 --no-access-log
```

浏览器打开 <http://127.0.0.1:8000>。API 文档位于 <http://127.0.0.1:8000/api/docs>。没有 AI 密钥时仍可打开内置样例或从上传文件提取原文，任务会显示需要配置密钥；配置后重启服务并点击“继续”。

### Docker 和长期部署

使用当前 Docker Compose v2。在项目目录中将 `.env.example` 复制为 `.env`，设置 `OPENAI_API_KEY` 和独立的 `PT_ACCESS_TOKEN`。后者是工作台的登录口令，Docker 端口映射下也应设置。可运行 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 在本机生成口令。

```bash
docker compose up --build -d
docker compose logs -f
```

打开 <http://127.0.0.1:8000>，使用工作台口令登录。Compose 绑定本机端口，命名卷 `papers` 保存数据。长期部署可在自有 Linux 主机运行同一配置，以 HTTPS 反向代理到 8000，转发原始 Host 并保留口令验证。服务只需一个实例和一块持久可写的本地磁盘。停止服务使用 `docker compose down`，备份时保留数据卷。

| 服务器环境变量 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI 服务端凭据 |
| `OPENAI_MODEL` | 支持 JSON Schema 的 Responses API 文本模型，默认 `gpt-4.1-mini` |
| `OPENAI_TTS_MODEL` | 示例配置使用 `gpt-realtime-2.1-mini`，走 Realtime WebSocket；显式选择 `gpt-4o-mini-tts` 时走 Speech API |
| `PT_DATA_DIR` | SQLite、源稿、PDF 和音频缓存目录，原生运行默认 `data/` |
| `PT_ACCESS_TOKEN` | 单用户工作台的访问口令，本机直接连接时可留空 |

Realtime 语音会在保存前比对返回的转录内容。OpenAI 公布了旧 Speech 模型的停用日期，可在[官方语音文档](https://developers.openai.com/api/docs/guides/text-to-speech)查看。

## 阅读、翻译与导出

* 导入 arXiv 时优先获取同一版本的 TeX 和 PDF。DOI 通过公开元数据寻找可访问的版本；来源受限时可上传自己有权使用的 PDF。
* 翻译以可恢复的后台任务执行。已完成的句子写入 SQLite。服务核对公式标记、引文和受保护文本，用户可编辑术语表、翻译风格、参考文献标题和致谢选项。
* 双语滚动以段落编号配对。朗读按真实句段播放结束事件推进，在中英文栏同步标出对应位置。总时长在音频时长已知后显示。阅读设置及书签保存在浏览器，完整工作台同时支持存储阅读状态。
* 公式讲解带回到原文段落的链接。全文导读涵盖研究问题、物理设定、主要推导和局限。推导中模型补充的步骤会标出，内置样例的预写讲解也有单独标识。
* 完成翻译后支持导出中文 PDF、双语 PDF、Markdown，有源稿时还能导出翻译后的 TeX 工程。音频可按整篇、章节或讲解导出，重复播放会复用缓存。

## 文档保真与适用范围

常见 LaTeX 数学环境直接保留原始源码。若提供可靠编号或辅助文件，就沿用源稿标签。PDF 缺少语义层时使用阅读顺序分析和公式截图。扫描页与无法处理的内容会显示提示；未通过完整性检查的译文不能作为完整文档导出。单篇上限为 32 MiB、450 页，PDF 提取文本也有大小限制。长篇导读根据各节选取代表段落，重要结论可通过引用回到原文核对。

中文 PDF 默认可用重新排版的阅读版。Linux 主机同时具有工作正常的 bubblewrap 用户命名空间、prlimit、XeLaTeX、中文字体和论文所需宏包时，会尝试在隔离环境中编译原模板。默认 Docker 镜像使用阅读版，在专用服务器需要原模板时可另外提供这些组件。特别复杂的宏、无文字层的扫描页和跨栏版式可能需要人工核对。仅有图像的公式保留图像；其原始 LaTeX 和可靠推导需要作者源稿。

## 开发与验证

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m unittest discover -s tests -v
node --test tests/test_player*.mjs
python scripts/build_sample.py
```

Python 测试覆盖安全导入、源稿还原、双栏顺序、中文 PDF 字形、公式图片、翻译占位符、重试恢复、音频缓存、访问控制和端到端 API 操作。自动化测试使用确定性的外部响应，实际运行默认调用 OpenAI。浏览器验收测试需要能启动 Chromium。公开示例的预览命令是 `npm run dev`；完整工作台使用上面的 FastAPI 启动命令。

| 代码位置 | 内容 |
| --- | --- |
| `papertranslate/api.py` | HTTP 接口、验证、登录、静态页面和导出 |
| `papertranslate/service.py` | SQLite 结构迁移、论文库、持久队列、断点续传、音频导出任务 |
| `papertranslate/ingest.py` | arXiv/DOI 导入、受限解析子进程、LaTeX 和 PDF 结构 |
| `papertranslate/ai.py` | 带校验的翻译、上下文检索、讲解、数学读音及逐句语音 |
| `papertranslate/exports.py` | 中文与双语 PDF、Markdown、翻译后源稿 |
| `web/` | 浏览器阅读器、公式面板、播放器和静态示例 |
| `papertranslate/sample/` | 已核对的双语教学样例及其原 PDF、LaTeX 源文件 |

封面可视化来自 NASA’s Goddard Space Flight Center / Jeremy Schnittman。[可视化原页](https://svs.gsfc.nasa.gov/13326/)。数学显示使用随项目分发的 KaTeX；其 MIT 许可证位于 `web/vendor/KaTeX-LICENSE`。
