# Zsh AI 命令建议 (只是一个想法，目前还没有运行起来)

本项目在本地运行的 LLM（通过 `llama_cpp`）基础上，为 Zsh 提供“按前缀生成命令”的智能建议。脚本会结合剪贴板内容与历史命令，在保证前缀正确的前提下生成更贴近当前上下文的命令。

## 功能特性
- 剪贴板感知：根据内容类型（IP、路径、文本）智能使用或忽略
- 前缀限定：严格以指定前缀（如 `ssh`、`cd`、`git`）开头
- 历史参考：优先参考以该前缀开头的历史命令
- 安全兜底：当剪贴板不适用时不拼接，回退到安全模板
- Token 相关：基于本地的 LLM 模型生成命令建议，节省token，并且想办法提升响应速度。

## 目录结构
- `scripts/zsh_ai_complete.py`：核心脚本（生成命令建议）
- `scripts/zsh_ai_server.py`：常驻服务（一次加载模型，复用处理请求）
- `models/`：本地模型目录（已在 `.gitignore` 中忽略）
- `data/history.txt`：历史命令文件（默认读取最近若干行）
- `.gitignore`：忽略 `models/`、缓存文件等

## 依赖与环境
- 系统：macOS
- Python：3.9+
- 依赖包：
  - `llama-cpp-python`
  - `pyperclip`

安装示例：

```bash
pip install llama-cpp-python pyperclip
```

> 说明：脚本默认关闭 Metal 加速（`use_metal=False`），如需开启可根据环境调整。

## 模型准备
- 将 GGUF 模型文件放置到 `models/` 目录
- 通过环境变量指定模型路径：

```bash
export ZSH_AI_MODEL_PATH="$HOME/workspace/zsh_ai/models/codellama-7b.Q4_K_M.gguf"
```

## 配置项
- `ZSH_AI_MODEL_PATH`：模型文件路径（必需）
- `ZSH_AI_HISTORY_FILE`：历史文件路径，默认：
  - `~/workspace/zsh_ai/data/history.txt`
- 服务相关：
  - `ZSH_AI_SERVER_HOST`：服务监听地址（默认 `127.0.0.1`）
  - `ZSH_AI_SERVER_PORT`：服务端口（默认 `8765`）
  - `ZSH_AI_SERVER_URL`：客户端请求地址（默认 `http://127.0.0.1:8765/complete`）
  - `ZSH_AI_AUTO_START_SERVER`：客户端自动启动服务（默认 `1`，设为 `0` 禁用）
  - `ZSH_AI_THREADS`：服务端推理线程数（默认 `4`，不稳定时可调小）

如需修改上下文与提示词预算，可编辑脚本中的：
- `N_CTX`（默认 1024）
- `RESERVED_TOKENS`（默认 100）

## 使用示例
直接调用脚本，传入前缀和可选剪贴板文本（第二个参数）：

```bash
# 结合剪贴板为 IP 的场景
python3 scripts/zsh_ai_complete.py ssh 192.168.1.10

# 结合剪贴板为路径的场景
python3 scripts/zsh_ai_complete.py cd /var/log

# 结合剪贴板为文本的场景
python3 scripts/zsh_ai_complete.py git feature/login
```

仅输出一行完整命令，例如：
```
ssh root@192.168.1.10
```

## 服务模式与启动
为提升响应速度，推荐使用常驻服务模式（模型只在第一次加载，后续请求复用）：

```bash
# 启动服务（前台）
python3 scripts/zsh_ai_server.py

# 健康检查
curl -s http://127.0.0.1:8765/health
# {"status":"ok"}
```

客户端脚本会优先请求服务；如服务未运行且 `ZSH_AI_AUTO_START_SERVER=1`（默认），客户端会尝试自动启动并重试；仍失败时才回退到本地直接加载模型。

### 服务控制
支持简单的状态与停止控制：

```bash
# 查看状态（运行则输出 RUNNING <pid> 127.0.0.1:8765）
python3 scripts/zsh_ai_server.py status

# 停止服务（向进程发送 SIGTERM 并清理 pid 文件）
python3 scripts/zsh_ai_server.py stop
```

如需手动终止进程：

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
kill <PID>
```

## HTTP 接口
服务端提供统一完成接口：

```bash
curl -s -X POST http://127.0.0.1:8765/complete \
  -H 'Content-Type: application/json' \
  -d '{"prefix":"ssh","clipboard":{"type":"ip","value":"192.168.1.10"},"history":["ssh root@192.168.1.1"]}'
# -> {"suggestion":"ssh root@192.168.1.10"}
```

## 与 Zsh 集成（示例）
可以在 `~/.zshrc` 中添加一个简单函数以便快速调用：

```bash
ai() {
  local prefix="$1"
  local clip="$(pbpaste)"
  python3 "$HOME/workspace/zsh_ai/scripts/zsh_ai_complete.py" "$prefix" "$clip"
}
```

用法示例：
```bash
$(ai ssh)           # 生成 ssh 命令并执行（慎用）
ai cd               # 仅打印建议命令
ai git              # 仅打印建议命令
```

> 建议先打印检查，再决定是否执行生成的命令。

## 生成逻辑要点
- `ssh`：剪贴板仅当为 IP/主机名且不含空格或 “/” 时使用，否则忽略
- `cd`：剪贴板仅当为以 “/” 或 “~” 开头的路径时使用，否则忽略
- `git`：剪贴板为非空文本时可用；生成以 `git` 开头的相关子命令
- 剪贴板不可用时，绝不拼接剪贴板，回退到安全兜底或历史

## 常见问题
- “模型文件不存在”：确认 `ZSH_AI_MODEL_PATH` 指向的 GGUF 文件存在
- “剪贴板读取失败”：确认系统剪贴板可用，或传入第二参数作为替代
- 生成为空或仅前缀：可能为提示词预算不足或上下文不匹配，可重试或检查历史/剪贴板
- “端口已被占用”：`lsof -nP -iTCP:8765 -sTCP:LISTEN` 检查占用；确认后 `kill <PID>` 释放再启动
- “服务启动提示已在运行”：说明已有服务实例，无需重复启动；可直接使用客户端或接口
- “segmentation fault”：
  - 将 `ZSH_AI_THREADS` 调小（如 `2`）后重试
  - 确认模型文件路径与权限正确
  - 在释放端口并重启服务后测试

## 许可
MIT
