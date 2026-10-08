# AI Chat

一个自托管的本地聊天网页应用，对接任何 OpenAI 兼容的 API 中转站（或本机的 Ollama / LM Studio）。界面接近 ChatGPT：流式回复、模型切换、多轮对话历史、移动端 PWA。服务端只有一个 Python 文件，没有数据库、没有前端构建步骤、没有第三方脚本。

- **数据只在你和自己选择的中转站之间流动**：除你填写的 API URL 外，页面不会请求任何外部域名，也没有遥测。
- **API Key 只存在服务端**：Windows 用当前用户 DPAPI 加密，macOS 用钥匙串，Linux 用 libsecret，POSIX 文件权限收紧到 `0600`。
- **默认只监听 `127.0.0.1`**：不设密码就不会暴露到局域网。

## 核心功能

| 功能 | 说明 |
| --- | --- |
| 流式对话 | SSE 实时输出，可随时停止（点击发送按钮或按 `Esc`） |
| 模型切换 | 顶栏下拉选择；优先使用中转站 `/models` 列表，可手动补充模型名 |
| 多对话历史 | 左侧栏保存历史对话，每条记录记住上次使用的模型 |
| 自定义中转站 | 任意 OpenAI 兼容 `base_url`，含本机与局域网的 Ollama / LM Studio |
| Claude 联网搜索 | 走中转站原生 Anthropic `/v1/messages`，用 `web_search_20260209` 服务端工具，单条消息最多 3 次，默认关闭；可与图片输入同时使用。中转站把模型改名成别名时，用“该模型支持联网搜索”手动指定 |
| 访问密码 | 局域网/手机访问时可选启用登录保护，含失败锁定 |
| 移动端 PWA | 添加到主屏幕，静态界面可离线打开 |
| 点击查词 | 点击 AI 回复里的英文单词，弹出中文释义和音标；词库由开源项目 [ECDICT](https://github.com/skywind3000/ECDICT) 生成，保存在本机 |
| 图片输入 | 给消息附一张图片（点回形针、粘贴或拖拽都可以），发送前在浏览器里自动压缩到 1MB 以内；图片会保留在这条对话的历史里，后面的提问可以继续参考它 |
| 命令行客户端 | `chat.py` 复用同一份本机配置 |

## 环境要求

- **Python 3.10 或更高版本**（依赖 `openai==3.8.0` 与 `httpx==0.28.1`，它要求 Python ≥ 3.10）
- 一个 OpenAI 兼容的 **API URL** 和 **API Key**（中转站、自建网关，或本机的 Ollama / LM Studio）
- Windows、macOS 或 Linux 均可

检查 Python 版本：

```powershell
python --version
```

macOS / Linux 上如果系统同时存在 Python 2 或旧版 Python 3，请用 `python3` 代替下面的 `python`。

## 安装

在项目目录中创建虚拟环境并安装依赖。

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

依赖只有 `openai` 一个（其余全部是标准库），不需要 Node、不需要构建前端。

## 快速开始

```powershell
python server.py
```

浏览器打开 `http://127.0.0.1:8000`，然后：

1. 点击左侧栏的 **“连接设置”**。
2. 填写 **API URL**（例如 `https://relay.example.com/v1`，通常以 `/v1` 结尾）和 **API Key**。
3. 可选：选择常用模型，或在 **自定义模型名称** 里用英文逗号补充模型。
4. 如需让模型联网查证，勾选 **“允许 Claude 联网搜索”**（默认关闭，可在同一个对话框里随时关掉）。
5. 点击 **“保存并连接”**。成功后可以直接开始对话。

首次运行不会有任何配置文件；保存成功后才会在 `server.py` 同目录生成 `app-config.json`、`app-secret.json`、`chat-history.json`（三者都已被 `.gitignore` 排除，不要提交）。

## 配置 API / 中转站

设置对话框里的四个字段：

- **API URL**：OpenAI 兼容接口的基础地址。只允许 `http://` 或 `https://`，不能包含用户名密码；不能指向链路本地、组播、保留或 IPv6 唯一本地地址（云元数据服务地址会被拒绝）。本机与局域网地址（`127.0.0.1`、`192.168.x.x`、`localhost`、`*.local`）不受影响，因此 Ollama、LM Studio 可以正常使用。
- **API Key**：只写入服务端。留空保存表示“保留已保存的 Key，只改地址或模型”。Key 只能包含 ASCII 字符——如果从聊天工具里复制时带上了中文标签或全角空格，会直接报错并提示重新复制。
- **模型**：应用会先请求中转站的模型列表，与手动填写的模型合并去重；模型接口暂时不可用时，使用上一次成功获取的列表或手动模型。
- **允许 Claude 联网搜索**：勾选后本机会改用中转站的**原生 Anthropic Messages 接口**（把 API URL 末尾的 `/v1` 换成 `/v1/messages`）发请求，请求体里带服务端工具 `web_search_20260209`（`max_uses=3`），回复下方的搜索词与来源链接（citations）来自原生流事件；只有 Claude 模型支持，模型名不含 `claude` 或未勾选时继续走原来的 OpenAI 兼容接口且**完全不带 `tools`**（中转站的 OpenAI 兼容层承载不了 Anthropic 服务端工具，硬塞会被网关拒绝）。若中转站没有 `/v1/messages`，会自动退回普通对话并在状态栏提示。搜索词与来源随对话写入本地 `chat-history.json`。

也可以用环境变量提供同样的信息（环境变量优先于本机保存的配置）：

```powershell
$env:OPENAI_API_KEY="sk-your-key"
$env:OPENAI_BASE_URL="https://relay.example.com/v1"
$env:OPENAI_MODEL="your-model-name"
$env:OPENAI_MODELS="model-a,model-b"
python server.py
```

### 全部环境变量

`.env.example` 只是变量名参考，项目不会自动读取 `.env` 文件，请用上面的方式设置环境变量。

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 中转站 API Key | 无 |
| `OPENAI_BASE_URL` | OpenAI 兼容 API 地址 | 无 |
| `OPENAI_MODEL` | 默认模型 | 无 |
| `OPENAI_MODELS` | 备用模型列表，英文逗号分隔 | 无 |
| `HOST` | Web 服务监听地址 | `127.0.0.1` |
| `PORT` | Web 服务端口 | `8000` |
| `APP_PASSWORD` | 局域网访问密码 | 本机模式可不设置 |
| `ALLOWED_HOSTS` | 允许访问服务的域名，英文逗号分隔 | 仅接受 IP 与 `localhost` |
| `SECRET_BACKEND` | API Key 存储方式：`auto`、`macos-keychain`、`libsecret`、`windows-dpapi`、`file` | `auto` |
| `ALLOW_KEY_REVEAL` | 是否允许在界面上确认后查看完整 API Key | `1` |
| `COOKIE_SECURE` | 会话 Cookie 是否带 `Secure`：`auto` 按 `X-Forwarded-Proto` 判断 | `auto` |
| `RELAY_CHECK_RESOLVED_ADDRESS` | 保存中转站地址时是否解析域名并检查解析结果 | `1` |
| `RELAY_TIMEOUT_SECONDS` | 调用中转站的单次超时（秒），避免接口长时间无响应 | `60` |
| `RELAY_DISCOVERY_TIMEOUT_SECONDS` | 获取模型列表的单次超时（秒），更短以保证设置界面不卡顿 | `15` |
| `RELAY_DEBUG_RESPONSE` | 临时把中转站原始响应字段打印到终端（已脱敏） | `0` |
| `DICTIONARY_PATH` | 点击查词使用的词库 JSON 路径 | `data/dict-en-zh.json` |

启动时终端会打印当前使用的 API Key 存储方式，例如 `API Key storage backend: windows-dpapi.`；如果 macOS/Linux 上提示找不到系统凭据助手，说明退回到了本机文件保护模式。

## 使用方法

- **发送消息**：在底部输入框输入后回车（`Shift+Enter` 换行）。生成过程中发送按钮会变成停止按钮，也可以按 `Esc` 中断；已生成的部分会保留到历史里。
- **切换模型**：顶栏下拉框。切换会记住在该对话上，下一次打开同一条历史时自动使用。
- **新对话 / 清空**：左侧栏 **“新对话”** 和顶栏 **“清空”** 都只是打开一个空白对话，**不会删除**已有历史记录。
- **历史对话**：左侧栏 **“历史对话”** 列表按最近更新排序，点击即可继续。想彻底删除历史，请先停止服务，再删除 `chat-history.json`。
- **退出登录**：启用 `APP_PASSWORD` 后，左侧栏会出现 **“退出登录”**。
- **点击查词**：点击 AI 回复中的任意英文单词，会在点击位置弹出释义卡片（词性、中文释义、音标），按 `Esc`、滚动页面或点击别处即关闭。双击查词对粘连排版更稳；`children`、`running`、`happiest` 这类变形会自动回到原形查询。功能开关在 **“连接设置 → 点击英文单词查释义”**，只记在浏览器本地，不会写入服务端配置。
- **发送图片**：点击输入框左侧的回形针选择图片，或者直接把图片粘贴 / 拖拽到输入框。附件会以缩略图出现在输入框上方（并标出压缩后的大小），点 **“移除”** 可以取消。每条消息最多 1 张，支持 PNG / JPEG / WebP，并且需要输入文字后才能发送。GPT-4o、Claude、Gemini 这类多模态模型会被自动识别，识别不到时在 **“连接设置 → 允许图片输入”** 里手动勾选。注意：图片会以 Base64 形式写进本机 `chat-history.json`，该文件会变得明显更大。
- **命令行版**：

```powershell
python chat.py
```

终端版与 Web 版共用本机保存的 API URL、API Key 和默认模型，完成一次 Web 配置后不需要再输入 Key。输入 `/model` 切换模型，`exit` 或 `quit` 退出。

## 手机访问

确保手机和电脑在同一个 Wi-Fi。先查电脑的局域网 IP（Windows 用 `ipconfig`，macOS/Linux 用 `ip addr`），然后设置密码并监听所有网卡：

```powershell
$env:HOST="0.0.0.0"
$env:APP_PASSWORD="请设置一个足够长的密码"
python server.py
```

手机浏览器打开 `http://<电脑局域网IP>:8000`，例如 `http://192.168.1.20:8000`。Android Chrome 和 iPhone Safari 都能通过浏览器菜单“添加到主屏幕”当成 App 使用；完整离线能力需要 HTTPS。

注意：监听 `0.0.0.0` 时服务会强制要求 `APP_PASSWORD`，否则拒绝启动。第一次连接失败时，请允许 Python 通过防火墙的专用网络。

## 常见问题

**提示“无法连接到本地服务”（浏览器里显示 `Failed to fetch`）**
这不是配置保存失败，而是页面完全拿不到后端响应：通常是 `python server.py` 已经退出（关掉终端窗口、电脑休眠、或者监听非回环地址时没设 `APP_PASSWORD` 而拒绝启动）。
页面看起来正常是因为离线缓存提供了页面壳，所以只按 F5 刷新可能仍然读取旧的 `app.js`。正确做法：
1. 重新运行 `python server.py`，确认终端打印出访问地址（默认 `http://127.0.0.1:8000`）；
2. **关闭整个标签页再重新打开**，或在 DevTools 的 Application → Service Workers / Storage 里注销并清空本站数据；
3. 再点击“保存并连接”。密钥只保存在服务端，保存失败时输入框内容保留，可以直接重试。

**端口被占用（`Address already in use` / `只能使用每个套接字地址…`）**
换端口：`$env:PORT="8010"`（macOS/Linux：`PORT=8010`），然后重新打开对应地址。

**页面提示“请先在左侧栏‘连接设置’里配置 API URL 和 Key”（HTTP 409）**
说明还没保存过配置，或 Key/地址被环境变量覆盖失败。打开设置对话框保存一次即可。

**提示 401 / 出现登录框**
服务设置了 `APP_PASSWORD`。输入启动服务时设置的那个密码（不是 API Key）。忘记时改环境变量后重启即可，旧会话会失效。

**提示 403 跨站请求已被拒绝**
写接口只接受同源请求。请直接用启动时打印的地址访问，不要从其他网页里发起请求；用反向代理时保持 `Host` 与代理域名一致，并把该域名加入 `ALLOWED_HOSTS`。

**提示“Host 不被信任”**
通过自定义域名或反向代理访问时，需要 `$env:ALLOWED_HOSTS="chat.example.com"`。这是为了阻断 DNS 重绑定。

**连续输错密码后被限制（HTTP 429）**
同一来源连续 5 次密码错误会锁定 60 秒；聊天、配置写入和查词也有默认频率限制（60 秒内 30 次聊天、20 次配置写入、60 次查词）。等待 `Retry-After` 指示的时间后重试即可。

**保存 Key 时提示“API Key 只能包含英文字符…”**
粘贴内容里混入了中文标签、全角符号或不可见字符。请只复制 Key 本体。

**模型列表是空的**
中转站的 `/models` 接口不可用或需要单独权限。在 **“自定义模型名称”** 里手动填写模型 ID（英文逗号分隔）即可正常对话。

**聊天报错 `Error code: 404` / `model not found`**
模型名与中转站提供的不一致，或 API URL 结尾少了 `/v1`。

**勾选了“允许 Claude 联网搜索”，但模型没有真的去搜索**
按顺序确认三件事：
1. **API URL 与 API Key 必须属于同一个中转站**。用 A 站的 Key 请求 B 站会返回 `401 Invalid token`，这与联网搜索无关（把开关关掉同样失败）。
2. **模型 ID 要写对**。多数中转站把 Claude 写成 `claude-opus-5-5` 这种连字符形式，写成 `claude-opus-5.5` 会得到 `model_not_found`；模型名不含 `claude` 时不会启用搜索。
3. **中转站必须暴露原生 `/v1/messages`**。OpenAI 兼容的 `/v1/chat/completions` 无法携带 `web_search_20260209`，通常会返回 `500 ... has unsupported type web_search_20260209 without a native payload`（此时本项目自动退回普通对话并给出提示）。
CC Switch 不在这条链路上：它的本地代理把 Claude Code 的 Anthropic 请求转换成 OpenAI 格式，方向正好相反，因此不能靠它补上 Anthropic 服务端工具。

**点击单词提示“词典未收录该词”**
随项目提交的 `data/dict-en-zh.json` 是从 ECDICT 生成的 2 万条常用词，专业词、生僻词和词组不在其中。需要完整词库时（约 66MB，不入库）：

```powershell
curl.exe -L -o data\ecdict.csv https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv
python tools/build_dictionary.py --limit 0
```

macOS/Linux 用 `curl -L -o data/ecdict.csv ...`。重新生成后重启服务即可；`data/ecdict.csv` 已被 `.gitignore` 排除，不要把它提交进仓库。

**发送图片被拒绝**
按提示对应处理：
1. **“当前模型没有开启图片输入”**：模型名里不含 `gpt-4o`、`claude`、`gemini` 等视觉关键词，服务会拒绝把图片发给不支持图片的模型。确认中转站的这个模型确实支持图片后，在 **“连接设置 → 允许图片输入”** 里手动勾选。回形针按钮看不到也是同一个原因。
2. **“图片太大”**：压缩后仍超过 1.5MB（很少见），换一张小一点的图或者截取局部再发。
3. **“图片地址不合法”**：只有从页面里选择 / 粘贴的图片才会被接受，外部图片链接会被直接拒绝。
4. **图片和联网搜索可以同时用**：带图的搜索请求会一起发到中转站的原生 Anthropic 通道（图片转成它的原生 image 格式）。如果中转站的原生通道不接受这种组合，服务会自动退回 OpenAI 兼容通道并明确提示“本轮未联网搜索、图片仍然有效”，请求不会失败。每次都看到这条提示，说明该中转站的原生端点不支持带图搜索。

**填了模型名却报 `model not found`，或下拉框里找不到它**\n中转站网页上显示的名字常常**不是** API 用的模型 ID：后面的 `2x`、`3x` 一般是计费倍率徽标，中文标签也可能是界面文案。确认方法：在“连接设置”里清空“自定义模型名称”后保存，顶栏下拉框列出的就是中转站 `/v1/models` 返回的**真实 ID**（悬停提示会写“模型来自中转站”）。如果你填的名字不在那个列表里，设置对话框会立刻给出提示。\n另外，图片输入和联网搜索默认靠模型名里的关键词（`gpt-4o`、`claude`、`gemini`）自动识别；别名识别不到时，用“允许图片输入”和“该模型支持联网搜索”两个开关手动指定。两个开关都保存在服务端，切换模型、重启后仍然有效。\n\n

**怀疑中转站返回内容异常，想看原始响应**

```powershell
$env:RELAY_DEBUG_RESPONSE="1"
python server.py
```

终端会打印带 `[relay-response-debug]` 前缀的 HTTP 状态、已脱敏响应头、解析后的 SSE 数据块和模型身份字段摘要。API Key、`Authorization`、Cookie、密码等会被替换为 `[REDACTED]`，本机绝对路径会被替换为 `[PATH]`。诊断不会改变发送给中转站的内容；看完请删除该变量或设为 `0`。

**聊天记录、配置文件在哪里？**
和 `server.py` 在同一目录：`chat-history.json`（对话内容）、`app-config.json`（API URL、模型、联网搜索、图片输入与两个模型能力开关）、`app-secret.json`（加密后的 Key 引用）。macOS/Linux 上权限为 `0600`。

**项目放在 OneDrive / iCloud / Dropbox 同步目录里，保存时偶发报错**
云端按需下载或占用文件会让原子替换失败，服务已经内置重试；如果仍然频繁失败，建议把项目移到普通本地目录。

**换电脑或重装系统后 Key 打不开**
DPAPI / 钥匙串绑定当前系统用户，`app-secret.json` 拷到新机器无法解密。重新填入一次 API Key 即可。

**想彻底禁止在界面上回显 API Key**
`$env:ALLOW_KEY_REVEAL="0"`。设置对话框里的“显示”按钮会消失，读取接口直接返回 404。

## 开发与测试

集成测试使用本地模拟中转站，**不需要真实 API Key，也不会消耗额度**，全程只监听 `127.0.0.1`：

```powershell
python -m unittest discover -s tests -v
```

测试覆盖：配置校验与持久化、模型发现与备用列表、模型切换、流式回复、对话历史读写与并发写入、登录与会话、跨站写拦截、`Host` 校验、限流、API Key 存储后端与文件权限、错误信息脱敏、中转站地址的 SSRF 校验、点击查词的输入白名单与词形回退、图片输入的类型与大小校验和视觉模型开关。测试产生的临时文件都写在 `tests/` 下并已被 `.gitignore` 排除。

## 项目结构

```text
server.py               Web 服务、API、配置与密钥存储
chat.py                 终端聊天客户端
static/index.html       网页结构
static/style.css        网页样式
static/app.js           网页交互和流式聊天
data/dict-en-zh.json    点击查词的词库（从 ECDICT 生成的常用词子集）
tools/build_dictionary.py  用完整 ECDICT CSV 重新生成上面的词库
tests/                  集成与安全测试
requirements.txt        Python 依赖（只有 openai）
.env.example            环境变量示例（全部是占位值）
.gitignore              忽略密钥、历史与运行产物
.gitattributes          统一换行符
LICENSE                 MIT 许可证
```

运行时生成的 `app-config.json`、`app-secret.json`、`chat-history.json` 均被 `.gitignore` 排除。

## 安全说明

- 不要提交 `.env`、API Key 或 `chat-history.json`。
- 默认只监听 `127.0.0.1`。设置非本机监听地址时，服务会强制要求 `APP_PASSWORD`。
- 服务只接受 `Host` 为本机 IP、`localhost` 或 `ALLOWED_HOSTS` 中列出域名的请求，用于阻断 DNS 重绑定。
- 所有写接口只接受同源请求（校验 `Origin` / `Sec-Fetch-Site`，且请求体必须是 `application/json`），防止其他网页跨站改写中转站地址。
- 中转站地址只允许 `http(s)`，不能包含用户名密码，也不能指向链路本地、组播、保留或 IPv6 唯一本地地址；保存时还会解析域名并检查解析结果。
- 联网搜索默认关闭。开启后提问内容会交给上游搜索工具检索公开网页；回复下方的来源链接只接受 `http(s)`，并以 `rel="noopener noreferrer"` 在新标签页打开，链接文字按纯文本渲染。
- 同一来源连续 5 次密码错误后，登录会被限制 60 秒。
- 敏感文件（`app-config.json`、`app-secret.json`、`chat-history.json`）都通过临时文件原子替换写入，在 macOS/Linux 上权限为 `0600`。
- `/api/chat` 与配置写入接口按会话（未登录时按来源 IP）限流，超出返回 `429` 并带 `Retry-After`。
- 查词接口 `POST /api/dictionary` 只接受 24 字符以内、纯字母（可含连字符和撇号）的单个单词，其余输入直接拒绝；它只在本地词库里查找，不会向中转站或任何第三方发起请求，返回内容也不含 Key、绝对路径或聊天记录。
- 图片只接受本机生成的内联 `data:image/...` 地址，不接受远程 URL，因此聊天时服务端不会去抓取任意网址；服务端还会在解码后校验 PNG / JPEG / WebP 的文件头是否与声明类型一致，单张原始字节上限 1.5MB，每条消息最多 1 张。只有 `/api/chat` 的请求体上限放宽到 8MB，其余接口仍是 1MB。浏览器端用 canvas 重新编码，顺带去掉 EXIF（含拍摄位置信息）。
- 会话 Cookie 始终带 `HttpOnly` 与 `SameSite=Strict`；通过 HTTPS 反向代理访问时自动追加 `Secure`。用反向代理终止 TLS 时请转发 `X-Forwarded-Proto`，或设置 `COOKIE_SECURE=1`。
- 已保存的 API Key 在界面上默认只显示前后各 4 个字符；完整值必须点击“显示”并由页面带上 `X-Reveal-Api-Key: 1` 请求头才能读取，也可用 `ALLOW_KEY_REVEAL=0` 彻底关闭该接口。
- 返回给页面的错误信息、命令行输出与 `RELAY_DEBUG_RESPONSE` 诊断都会先脱敏：API Key、Token、Cookie、密码以及本机绝对路径不会出现在响应或日志里。
- 通过环境变量 `OPENAI_API_KEY` 提供的 Key 只保留在进程内存中，不会被写入 `app-secret.json`。
- 如果要公开部署到公网，请自行加上 HTTPS、真正的身份验证和集中式密钥管理；本项目按“本机/局域网自用工具”的定位设计。

## 隐私

聊天记录、API URL 和 Key 全部保存在你自己的机器上。页面加载的每一个资源都来自本服务自身，没有 CDN、字体、统计或分析脚本。你的提问内容只会发送到你自己在设置里填写的中转站地址。

## 许可

本项目以 MIT 许可证开源，详见 [LICENSE](LICENSE)。点击查词使用的词库由 [ECDICT](https://github.com/skywind3000/ECDICT)（MIT License）生成，词义内容版权归 ECDICT 项目所有。请自行确认所使用的中转站与模型服务的使用条款；产生的 API 费用由你自己承担。
