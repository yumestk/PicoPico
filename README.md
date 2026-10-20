# pico

`pico` 是一个面向代码仓库的轻量本地 coding agent。它直接跑在终端里，先看当前工作区，再用一组受约束的工具去读文件、改文件、跑命令，并把会话状态保存在本地 `.pico/` 目录里。

它更像一个能在仓库里持续工作的命令行助手，不是纯聊天窗口。你可以拿它做代码排查、测试修复、仓库分析，或者让它在当前项目里执行一次性的工程任务。

## 适合做什么

- 在本地仓库里排查测试失败
- 读取当前代码结构并给出修改建议
- 基于现有文件做小步迭代，而不是脱离仓库空想
- 在会话中保留上下文，支持继续上一次工作

## 主要特性

- 包名是 `pico`
- CLI 命令是 `pico`
- 模块入口是 `python -m pico`
- 会话、运行、审批和事件保存在 `.pico/state.sqlite3`
- 每个工作区由独立 Host 管理后台任务，支持断线重连和跨终端审批
- 审计工件通过 `pico runs export <run_id>` 显式导出到 `.pico/exports/<run_id>/`
- 模型通过原生工具调用执行任务，支持一轮多个调用，按顺序校验、审批和执行
- 进度、工具状态与审批显示在 stderr，最终回答输出到 stdout；Ctrl+C 取消运行并保存现场
- 支持四类模型后端：
  - Ollama
  - OpenAI 兼容 Responses API
  - Anthropic 兼容 Messages API
  - DeepSeek Anthropic 兼容 API

## 使用截图

CLI 帮助信息：

![pico help](assets/screenshots/pico-help.png)

启动界面：

![pico start](assets/screenshots/pico-start.png)

REPL 内置命令与会话路径：

![pico repl](assets/screenshots/pico-repl.png)

## 安装

需要 Python 3.10+。

如果你用 `uv`，直接安装依赖：

```bash
uv sync
```

如果你已经在自己的 Python 环境里工作，也可以直接装成可编辑模式：

```bash
pip install -e .
```

## 快速开始

在当前仓库里启动交互模式。默认 provider 是 DeepSeek：

```bash
uv run pico
```

指定另一个工作目录：

```bash
uv run pico --cwd /path/to/repo
```

直接跑一次性任务：

```bash
uv run pico run "inspect the test failures and propose a fix"
```

如果当前环境已经安装过包，也可以直接这样启动：

```bash
python -m pico
```

## 模型后端

Pico 启动时会读取项目根目录的 `.env`。本地真实 key 放在 `.env`，仓库只保留 `.env.example`。配置优先级是：

```text
显式 CLI 参数 > .env 里的 PICO_* 变量 > 旧环境变量 > 代码默认值
```

Provider 选择的具体顺序是：

```text
--provider > PICO_PROVIDER > 代码默认 deepseek
```

不传 `--provider` 且没有 `PICO_PROVIDER` 时默认使用 `deepseek`。这是推荐配置路径：DeepSeek 的 Anthropic-compatible endpoint 比本地 Ollama 更少依赖本机模型环境，也比 OpenAI-compatible/Anthropic-compatible 代理少一层默认 gateway 假设。其他 provider 仍然保留，可以在 `.env` 里写 `PICO_PROVIDER=openai`、`PICO_PROVIDER=anthropic`、`PICO_PROVIDER=ollama`，也可以显式传 `--provider openai`、`--provider anthropic` 或 `--provider ollama`。

`.env` 会在构建 provider client 前加载，并覆盖当前进程里的同名环境变量。模型名和 base URL 可以通过 `--model`、`--base-url` 临时覆盖；API key 只从环境变量读取。

本地第一次配置：

```bash
cp .env.example .env
```

然后把要使用的 provider key 填进去。`.env` 已经被 `.gitignore` 忽略，不要提交真实 key。

### 推荐配置：DeepSeek

最小配置只需要 key：

```bash
PICO_DEEPSEEK_API_KEY="your-api-key"
```

默认模型和接口是：

```bash
PICO_DEEPSEEK_API_BASE="https://api.deepseek.com/anthropic"
PICO_DEEPSEEK_MODEL="deepseek-v4-pro"
```

所以常规情况下 `.env` 里只填 `PICO_DEEPSEEK_API_KEY` 就能直接启动：

```bash
uv run pico
```

如果你需要临时切模型或代理地址，不必改 `.env`，可以直接覆盖：

```bash
uv run pico --model deepseek-v4-pro --base-url https://api.deepseek.com/anthropic
```

DeepSeek 当前走 Anthropic-compatible Messages API，所以 runtime 里复用的是 Anthropic-compatible client；这只影响 HTTP 协议，不影响 CLI 用法。

Pico 使用原生工具协议，并完整保存回放所需的 provider 响应块。DeepSeek 当前仍显式关闭 thinking，沿用有界的单步输出预算；这不影响原生工具调用。

### 后台任务、重连与取消

`pico` 默认进入交互会话，`pico run "任务"` 提交一次任务并显示进度。首次使用时自动启动当前工作区的本地 Host；同一工作区的任务串行执行。

```bash
uv run pico run "检查测试失败" --detach
uv run pico runs list
uv run pico runs attach <run_id>
uv run pico approvals list
uv run pico approvals approve <approval_id>
uv run pico approvals deny <approval_id>
uv run pico runs cancel <run_id>
uv run pico host status
uv run pico host stop
```

运行选项放在子命令之前，例如 `pico --session latest --max-steps 10 run "继续修复"`；显式选项覆盖会话默认配置。`--detach` 输出 run_id 后退出，任务继续执行。终端关闭或连接断开也不会取消任务；在 attach 时按 Ctrl+C 会向 Host 提交取消。进度和审批显示在 stderr，最终回答输出到 stdout。新 attach 默认回放已有事件，`--after <sequence>` 可以从指定游标继续。

ask 模式下的审批保存在数据库中，有效期为 30 分钟，可以从另一个终端回复。批准只对对应调用生效；审批期间工作区变化会使批准失效。等待审批会占用当前工作区执行槽，用户可以拒绝或取消来继续处理后续任务。

旧 `.pico/sessions/*.json` 不自动导入，也不再作为恢复入口；请创建新的数据库会话。使用 `--session <session_id>` 或 `--session latest` 继续当前原生协议的会话，切换 provider 协议需要新会话。

Host 异常退出后，原来未结束的运行标记为 interrupted，不自动重放工具。执行结果未知时需要先检查文件；未确认命令已结束的工作区会阻止新执行。`completed` 表示执行正常结束，任务正确性仍由测试或 verifier 判断。

当前验收平台为 macOS/POSIX。每个根 Run 使用独立 worker，命令监督进程在 worker 退出后清理 shell 进程组；两路输出各保留最多 64 KiB，默认命令超时 20 秒，最大 120 秒。模型可见工具文本另有 4000 字符展示限制。

开发者入口为 `ApplicationService` 和 `HostClient`，执行内核继续使用 `ModelRequest`、`ModelResponse`、`ToolExecutionResult`。模型所需原始回放块保存在私有会话中，展示事件先脱敏再公开。逐 Token 流式输出、Windows 进程管理和桌面 UI 留待后续阶段。

阶段计划和验收记录仅保存在本地 `docs/`，不纳入 GitHub 仓库。

### macOS 桌面预览

桌面端目前提供工作区选择和 Host 连接状态，任务操作继续使用 CLI。关闭桌面窗口不会停止工作区 Host。

在 macOS Apple Silicon 上构建内部试用包：

```bash
uv run python -m scripts.prepare_desktop_runtime
cd desktop
nvm use
npm ci
npm run typecheck
npm test
npm run package
```

产物为 `desktop/out/Pico-darwin-arm64/Pico.app`，可用 Finder 打开。包内带有经过 SHA256 校验的 CPython；运行不依赖系统 Python、uv、项目虚拟环境或开发服务器。目标仓库使用的 Git、测试框架等开发工具仍需自行安装，模型配置仍读取该仓库的 `.env`。

首版仅支持 macOS arm64 的本地内部试用，尚未签名公证；不包含桌面对话、审批或 Diff 面板。构建下载发生在打包阶段，Python 的来源与校验值见 `desktop/python-runtime.json`。运行 `npm run test:electron` 可验收真实应用窗口及断连后的 Host。

### 可选配置：right.codes

right.codes 在 Pico 里有两条可选 provider 路径：

- `--provider openai`：走 OpenAI-compatible `/responses`，默认 base URL 是 `https://www.right.codes/codex/v1`，默认模型是 `gpt-5.4`
- `--provider anthropic`：走 Anthropic-compatible `/messages`，默认 base URL 是 `https://www.right.codes/claude/v1`，默认模型是 `claude-sonnet-4-6`

如果 right.codes 给你的是一把共享 key，推荐只填这一项：

```bash
PICO_RIGHT_CODES_API_KEY="your-right-codes-key"
```

然后按需要选择 provider：

```bash
uv run pico --provider openai
uv run pico --provider anthropic
```

如果你想显式区分两条 provider 的 key，也可以分别配置：

```bash
PICO_OPENAI_API_KEY="your-right-codes-key-for-codex"
PICO_ANTHROPIC_API_KEY="your-right-codes-key-for-claude"
```

不要在 `.env` 里写 `PICO_OPENAI_API_KEY=$PICO_RIGHT_CODES_API_KEY` 这种 shell 展开形式；Pico 的 `.env` 解析器只读取字面量，不展开变量引用。要么只写 `PICO_RIGHT_CODES_API_KEY`，要么把 key 字符串分别填到 provider-specific 变量里。

如果请求 right.codes 返回 `API Key额度不足`，说明协议和 endpoint 已经打通，但当前 key 没有可用额度；换一把有额度的 key，或到 right.codes 后台处理额度。

当前 provider 环境变量：

| provider | base URL | API key | model |
| --- | --- | --- | --- |
| `deepseek` | `PICO_DEEPSEEK_API_BASE`，回退 `DEEPSEEK_API_BASE`，默认 `https://api.deepseek.com/anthropic` | `PICO_DEEPSEEK_API_KEY`，回退 `DEEPSEEK_API_KEY` | `PICO_DEEPSEEK_MODEL`，回退 `DEEPSEEK_MODEL`，默认 `deepseek-v4-pro` |
| `openai` | `PICO_OPENAI_API_BASE`，回退 `OPENAI_API_BASE`，默认 `https://www.right.codes/codex/v1` | `PICO_OPENAI_API_KEY`，回退 `OPENAI_API_KEY`、`PICO_RIGHT_CODES_API_KEY`、`RIGHT_CODES_API_KEY`、`PICO_ANTHROPIC_API_KEY`、`ANTHROPIC_API_KEY` | `PICO_OPENAI_MODEL`，回退 `OPENAI_MODEL`，默认 `gpt-5.4` |
| `anthropic` | `PICO_ANTHROPIC_API_BASE`，回退 `ANTHROPIC_API_BASE`，默认 `https://www.right.codes/claude/v1` | `PICO_ANTHROPIC_API_KEY`，回退 `ANTHROPIC_API_KEY`、`PICO_RIGHT_CODES_API_KEY`、`RIGHT_CODES_API_KEY`、`PICO_OPENAI_API_KEY`、`OPENAI_API_KEY` | `PICO_ANTHROPIC_MODEL`，回退 `ANTHROPIC_MODEL`，默认 `claude-sonnet-4-6` |
| `ollama` | `--host`，默认 `http://127.0.0.1:11434` | 不需要 | `--model`，默认 `qwen3.5:4b` |

如果有额外的敏感环境变量需要从 trace/report 里脱敏，可以用 `PICO_SECRET_ENV_NAMES` 配置逗号分隔的变量名，或启动时重复传 `--secret-env-name NAME`。

### OpenAI 兼容接口

如果要改用 OpenAI-compatible `/responses` 服务，显式传 `--provider openai`：

```bash
uv run pico --provider openai
```

默认 OpenAI 兼容接口使用 right.codes 的 Codex endpoint：

```bash
PICO_OPENAI_API_BASE="https://www.right.codes/codex/v1"
PICO_RIGHT_CODES_API_KEY="your-right-codes-key"
PICO_OPENAI_MODEL="gpt-5.4"
```

也可以改成其他 OpenAI-compatible 服务：

```bash
PICO_OPENAI_API_BASE="https://your-api.example/v1"
PICO_OPENAI_API_KEY="your-api-key"
PICO_OPENAI_MODEL="gpt-5.4"
```

### Anthropic 兼容接口

如果要改用 Anthropic-compatible 服务，显式传 `--provider anthropic`：

```bash
uv run pico --provider anthropic
```

默认 Anthropic 兼容接口使用 right.codes 的 Claude endpoint：

```bash
PICO_ANTHROPIC_API_BASE="https://www.right.codes/claude/v1"
PICO_RIGHT_CODES_API_KEY="your-right-codes-key"
PICO_ANTHROPIC_MODEL="claude-sonnet-4-6"
```

如果你的服务端对多个兼容接口复用了同一套密钥，`pico` 也支持从 `PICO_ANTHROPIC_API_KEY` 回退到 `ANTHROPIC_API_KEY`、`PICO_RIGHT_CODES_API_KEY`、`RIGHT_CODES_API_KEY`、`PICO_OPENAI_API_KEY` 或 `OPENAI_API_KEY`。

### Ollama

如果要改用本地 Ollama，显式传 `--provider ollama`：

```bash
ollama serve
ollama pull qwen3.5:4b
uv run pico --provider ollama --model qwen3.5:4b
```

## 常用交互命令

- `/help`：查看内置命令
- `/memory`：查看提炼后的工作记忆
- `/session`：查看当前数据库会话 ID
- `/reset`：清空当前会话状态（有未结束任务时拒绝）
- `/exit` 或 `/quit`：退出 REPL

## 安全与持久化

`pico` 不会默认把所有动作都放开。像 shell 执行、文件写入这类高风险操作，会受审批模式控制：

- `--approval ask`
- `--approval auto`
- `--approval never`

运行状态、会话、工具结果、报告和事件以 SQLite 为唯一来源。完成状态与结束事件在同一事务提交；旧 JSON 文件不参与恢复。数据库和运行目录保存在本地，默认不上传仓库。

```bash
uv run pico runs export <run_id>
```

显式导出生成 `run_state.json`、`trace.jsonl`、完整的 `tool_results.json` 和已有的 `report.json`。导出失败会单独报告，不改变数据库中的执行结果。

Host 通过当前用户私有的 Unix socket 通信，不监听网络端口。凭据由环境或项目 `.env` 提供，不写入公共 RPC、运行配置快照或进程参数。Host 日志按大小轮转，worker 诊断有界保留。进程组管理不构成对同一用户恶意进程的沙箱。

## 开发

常用本地检查：

```bash
uv run python -m pytest tests -q
uv run ruff check pico tests scripts
```

内部代码现在按较轻的边界拆分：`pico/evaluation/` 放 benchmark 和 metrics，`pico/providers/` 放模型 provider client，`pico/features/` 放可选运行时能力。新代码应直接使用这些包路径；旧的 `pico.evaluator`、`pico.metrics`、`pico.models` 和 `pico.memory` import 不再作为公共入口保留。
