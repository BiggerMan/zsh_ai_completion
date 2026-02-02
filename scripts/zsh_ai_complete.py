#!/usr/bin/env python3
import os
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error
import pyperclip
from llama_cpp import Llama

# 全局配置
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_HISTORY_FILE = os.path.join(PROJECT_ROOT, "data", "history.txt")
HISTORY_FILE = os.environ.get("ZSH_AI_HISTORY_FILE", DEFAULT_HISTORY_FILE)
MAX_HISTORY_LINES = 10
MAX_CMD_LENGTH = 20
N_CTX = 1024
RESERVED_TOKENS = 100
MAX_PROMPT_TOKENS = N_CTX - RESERVED_TOKENS
DEFAULT_SERVER_URL = os.environ.get("ZSH_AI_SERVER_URL", "http://127.0.0.1:8765/complete")

# 基础兜底（无剪贴板时使用）
BASE_FALLBACK = {
    "ssh": "ssh root@192.168.1.1",
    "ls": "ls -l",
    "cd": "cd ~",
    "git": "git status",
    "docker": "docker ps -a",
    "kinit": "kinit yourname@DOMAIN.COM",
    "kubectl": "kubectl get pods"
}

def get_clipboard_content():
    """读取剪贴板内容，区分IP/路径/通用文本"""
    try:
        content = pyperclip.paste().strip() or ""
        # 识别IP格式（简单匹配，适配你的场景）
        if content and ('.' in content and not os.path.exists(content)):
            return {"type": "ip", "value": content}
        # 识别路径格式
        elif content and (content.startswith('/') or content.startswith('~')):
            return {"type": "path", "value": content}
        else:
            return {"type": "text", "value": content}
    except:
        return {"type": "text", "value": ""}

def load_ai_model():
    """加载模型（隐藏冗余日志）"""
    sys.stderr = open(os.devnull, 'w')
    model_path = os.environ.get(
        "ZSH_AI_MODEL_PATH",
        os.path.expanduser("~/workspace/zsh_ai/models/codellama-7b.Q4_K_M.gguf")
    )
    if not os.path.exists(model_path):
        sys.stderr = sys.__stderr__
        print(f"ERROR: 模型文件不存在 → {model_path}", file=sys.stderr)
        sys.exit(1)
    
    llm = Llama(
        model_path=model_path,
        n_ctx=N_CTX,
        n_threads=4,
        n_gpu_layers=0,
        verbose=False,
        use_metal=False,
        seed=42,
        mirostat_mode=0
    )
    sys.stderr = sys.__stderr__
    return llm

def truncate_text(text, max_len):
    return text[:max_len] + "..." if len(text) > max_len else text

def truncate_prompt_by_tokens(llm, prompt, max_tokens):
    prompt_tokens = llm.tokenize(prompt.encode("utf-8"))
    if len(prompt_tokens) <= max_tokens:
        return prompt
    return prompt[:100]  # 极端情况截断前100字符

def generate_general_suggestion(prefix, clipboard, history, llm):
    """优先使用剪贴板内容生成建议"""
    # 1. 过滤历史：仅保留前缀开头的命令
    history_filtered = [
        truncate_text(cmd, MAX_CMD_LENGTH) 
        for cmd in history if cmd.startswith(prefix)
    ][-MAX_HISTORY_LINES:]

    # 2. 核心：根据剪贴板类型生成专属建议模板（优先于兜底）
    clip_value = clipboard["value"]
    custom_fallback = BASE_FALLBACK[prefix]
    if prefix == "ssh" and clipboard["type"] == "ip" and clip_value:
        custom_fallback = f"ssh root@{clip_value}"  # ssh+IP优先
    elif prefix == "cd" and clipboard["type"] == "path" and clip_value:
        custom_fallback = f"cd {clip_value}"        # cd+路径优先
    elif prefix == "git" and clip_value:
        custom_fallback = f"git add {clip_value}"   # git+文本优先

    def _clip_usable(pfx, cb):
        t = cb.get("type", "")
        v = (cb.get("value", "") or "").strip()
        if pfx == "ssh":
            return t == "ip" and "/" not in v and " " not in v
        if pfx == "cd":
            return t == "path" and v != ""
        if pfx == "git":
            return t == "text" and v != ""
        return False

    clip_ok = _clip_usable(prefix, clipboard)
    clip_for_prompt = clip_value if clip_ok else ""
    if prefix == "ssh":
        prefix_rule = "仅当剪贴板为IP或主机名时使用；禁止路径、空格或包含“/”的内容；不可用时忽略剪贴板。格式示例：ssh root@<主机>"
    elif prefix == "cd":
        prefix_rule = "仅当剪贴板为以“/”或“~”开头的路径时使用；禁止IP或普通文本；不可用时忽略剪贴板"
    elif prefix == "git":
        prefix_rule = "当剪贴板为非空文本时可用；生成与该文本相关的子命令（如 add、checkout、commit），但必须以“git”开头"
    else:
        prefix_rule = "通常忽略剪贴板内容；若不可用，绝不拼接剪贴板"

    prompt = f"""
    任务：根据“{prefix}”和剪贴板生成一个以“{prefix}”开头的 Linux 命令。
    剪贴板类型：{clipboard.get("type","")}；是否可用：{"是" if clip_ok else "否"}；值：{clip_for_prompt}
    规则：
    1. 必须以“{prefix}”开头；
    2. {prefix_rule}；
    3. 若剪贴板不可用，必须从历史或兜底生成，且绝不拼接剪贴板；
    4. 仅输出完整命令，无任何解释、换行、注释；
    5. 参考历史命令：{history_filtered}；
    """

    prompt = truncate_prompt_by_tokens(llm, prompt, MAX_PROMPT_TOKENS)

    try:
        # 微小随机性，让模型优先使用剪贴板
        output = llm.create_completion(
            prompt=prompt,
            max_tokens=80,
            temperature=0.01,  # 0.01=几乎确定，但能响应剪贴板
            stop=["\n", "#", ";"],
            echo=False,
            top_p=1.0,
            repeat_penalty=1.2
        )
        suggestion = output["choices"][0]["text"].strip()
        
        # 校验：优先用剪贴板生成的结果，否则用custom_fallback
        if suggestion and suggestion.startswith(prefix):
            # 若ssh建议中包含剪贴板IP，直接用；否则用custom_fallback
            if prefix == "ssh" and clip_value and clip_value in suggestion:
                return suggestion
            return suggestion if suggestion != prefix else custom_fallback
        else:
            return custom_fallback
    except Exception as e:
        print(f"DEBUG: {str(e)}", file=sys.stderr)
        return custom_fallback

def request_server(prefix, clipboard, history, timeout=3.0):
    try:
        payload = json.dumps({
            "prefix": prefix,
            "clipboard": clipboard,
            "history": history
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            DEFAULT_SERVER_URL,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            obj = json.loads(data.decode("utf-8"))
            suggestion = (obj.get("suggestion") or "").strip()
            return suggestion if suggestion else None
    except Exception:
        return None

def ensure_server_started():
    server_script = os.path.join(PROJECT_ROOT, "scripts", "zsh_ai_server.py")
    try:
        subprocess.Popen(
            [sys.executable, server_script],
            stdout=open(os.devnull, "w"),
            stderr=open(os.devnull, "w"),
            cwd=PROJECT_ROOT
        )
        time.sleep(0.5)
    except Exception:
        pass

if __name__ == "__main__":
    prefix = sys.argv[1] if len(sys.argv) >= 2 else ""
    if not prefix or len(prefix) < 2 or prefix not in BASE_FALLBACK:
        sys.exit(0)

    # 核心修复：优先用Zsh传递的剪贴板参数
    clipboard_arg = sys.argv[2] if len(sys.argv) >= 3 else ""
    # 读取剪贴板（优先用参数，其次用脚本自己读）
    clipboard = get_clipboard_content()
    if clipboard_arg:
        # 覆盖脚本自己读取的内容
        if '.' in clipboard_arg and not os.path.exists(clipboard_arg):
            clipboard = {"type": "ip", "value": clipboard_arg}
        elif clipboard_arg.startswith('/') or clipboard_arg.startswith('~'):
            clipboard = {"type": "path", "value": clipboard_arg}
        else:
            clipboard = {"type": "text", "value": clipboard_arg}
    # 读取历史
    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = f.read().splitlines()[-MAX_HISTORY_LINES:]
    
    suggestion = request_server(prefix, clipboard, history, timeout=3.0)
    print(f"DEBUG: server suggestion = {suggestion}")
    if not suggestion and os.environ.get("ZSH_AI_AUTO_START_SERVER", "1") != "0":
        print(f"DEBUG: auto start server")
        ensure_server_started()
        suggestion = request_server(prefix, clipboard, history, timeout=6.0)
    if not suggestion:
        print(f"DEBUG: load local model")
        llm = load_ai_model()
        suggestion = generate_general_suggestion(prefix, clipboard, history, llm)
    
    # 输出最终建议
    print(suggestion)
    sys.exit(0)
