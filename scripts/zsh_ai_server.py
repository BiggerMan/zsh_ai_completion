#!/usr/bin/env python3
import os
import sys
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import threading
import signal
import atexit
import subprocess
import json as _json
import time
from llama_cpp import Llama

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MAX_HISTORY_LINES = 10
MAX_CMD_LENGTH = 20
N_CTX = 1024
RESERVED_TOKENS = 100
MAX_PROMPT_TOKENS = N_CTX - RESERVED_TOKENS

BASE_FALLBACK = {
    "ssh": "ssh root@192.168.1.1",
    "ls": "ls -l",
    "cd": "cd ~",
    "git": "git status",
    "docker": "docker ps -a",
    "kinit": "kinit yourname@DOMAIN.COM",
    "kubectl": "kubectl get pods"
}

def load_ai_model():
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
        n_threads=int(os.environ.get("ZSH_AI_THREADS", "4")),
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
    return prompt[:100]

def generate_general_suggestion(prefix, clipboard, history, llm):
    history_filtered = [
        truncate_text(cmd, MAX_CMD_LENGTH)
        for cmd in history if cmd.startswith(prefix)
    ][-MAX_HISTORY_LINES:]
    clip_value = clipboard["value"]
    custom_fallback = BASE_FALLBACK[prefix]
    if prefix == "ssh" and clipboard["type"] == "ip" and clip_value:
        custom_fallback = f"ssh root@{clip_value}"
    elif prefix == "cd" and clipboard["type"] == "path" and clip_value:
        custom_fallback = f"cd {clip_value}"
    elif prefix == "git" and clip_value:
        custom_fallback = f"git add {clip_value}"
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
        output = llm.create_completion(
            prompt=prompt,
            max_tokens=80,
            temperature=0.01,
            stop=["\n", "#", ";"],
            echo=False,
            top_p=1.0,
            repeat_penalty=1.2
        )
        suggestion = output["choices"][0]["text"].strip()
        if suggestion and suggestion.startswith(prefix):
            if prefix == "ssh" and clip_value and clip_value in suggestion:
                return suggestion
            return suggestion if suggestion != prefix else custom_fallback
        else:
            return custom_fallback
    except Exception as e:
        print(f"DEBUG: {str(e)}", file=sys.stderr)
        return custom_fallback

LLM = None
LLAMA_INIT_LOCK = threading.Lock()
INFER_LOCK = threading.Lock()

def get_llm():
    global LLM
    if LLM is None:
        with LLAMA_INIT_LOCK:
            if LLM is None:
                LLM = load_ai_model()
    return LLM

class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return
    def _json_response(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
        else:
            self._json_response(404, {"error": "not_found"})
    def do_POST(self):
        if self.path != "/complete":
            self._json_response(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception:
            self._json_response(400, {"error": "bad_json"})
            return
        prefix = req.get("prefix", "")
        clipboard = req.get("clipboard", {"type": "text", "value": ""})
        history = req.get("history", [])
        if not prefix or prefix not in BASE_FALLBACK:
            self._json_response(400, {"error": "bad_prefix"})
            return
        llm = get_llm()
        with INFER_LOCK:
            suggestion = generate_general_suggestion(prefix, clipboard, history, llm)
        self._json_response(200, {"suggestion": suggestion})

class ReuseAddrServer(ThreadingHTTPServer):
    allow_reuse_address = True

def check_server_alive(host, port, timeout=1.0):
    try:
        url = f"http://{host}:{port}/health"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
            obj = json.loads(data.decode("utf-8"))
            return obj.get("status") == "ok"
    except Exception:
        return False

PID_FILE = os.path.join(PROJECT_ROOT, "data", "zsh_ai_server.pid")
META_FILE = os.path.join(PROJECT_ROOT, "data", "zsh_ai_server.meta.json")
HTTPD = None

def _write_pid():
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        meta = {"pid": os.getpid(), "host": os.environ.get("ZSH_AI_SERVER_HOST", "127.0.0.1"), "port": int(os.environ.get("ZSH_AI_SERVER_PORT", "8765"))}
        with open(META_FILE, "w") as f:
            f.write(_json.dumps(meta))
    except Exception:
        pass

def _read_pid():
    try:
        with open(PID_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None

def _read_meta():
    try:
        with open(META_FILE, "r") as f:
            return _json.loads(f.read())
    except Exception:
        return {}

def _remove_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        if os.path.exists(META_FILE):
            os.remove(META_FILE)
    except Exception:
        pass

def _pid_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def _signal_handler(signum, frame):
    try:
        if HTTPD:
            HTTPD.shutdown()
    finally:
        _remove_pid()
        sys.exit(0)

def find_server_pids():
    try:
        out = subprocess.check_output(["ps", "-ef"], stderr=subprocess.DEVNULL).decode()
        pids = []
        for line in out.splitlines():
            if "scripts/zsh_ai_server.py" in line and "grep" not in line:
                parts = line.split()
                if len(parts) > 2:
                    try:
                        pid = int(parts[1])
                        if pid != os.getpid():
                            pids.append(pid)
                    except Exception:
                        pass
        return pids
    except Exception:
        return []

def _lsof_listen_pid(port):
    try:
        out = subprocess.check_output(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], stderr=subprocess.DEVNULL).decode().strip()
        return [int(x) for x in out.splitlines() if x.strip().isdigit()]
    except Exception:
        return []

def _collect_pids(host, port):
    s = set()
    pid = _read_pid()
    meta = _read_meta()
    if pid:
        s.add(pid)
    if meta.get("pid"):
        s.add(meta["pid"])
    for p in _lsof_listen_pid(port):
        s.add(p)
    for p in find_server_pids():
        s.add(p)
    return [p for p in s if _pid_running(p)]

def _terminate_pids(pids, sig):
    for p in pids:
        try:
            os.kill(p, sig)
        except Exception:
            pass

def _wait_stopped(host, port, pids, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = any(_pid_running(p) for p in pids)
        port_ok = check_server_alive(host, port, timeout=0.5)
        if not alive and not port_ok:
            return True
        time.sleep(0.2)
    return False

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    host = os.environ.get("ZSH_AI_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("ZSH_AI_SERVER_PORT", "8765"))
    if cmd == "status":
        pid = _read_pid()
        if pid and _pid_running(pid):
            print(f"RUNNING {pid} {host}:{port}")
            sys.exit(0)
        if check_server_alive(host, port, timeout=1.0):
            try:
                out = subprocess.check_output(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], stderr=subprocess.DEVNULL).decode().strip()
                pid2 = int(out.splitlines()[0]) if out else None
            except Exception:
                pid2 = None
            if pid2:
                print(f"RUNNING {pid2} {host}:{port}")
                sys.exit(0)
            pids = find_server_pids()
            if pids:
                print(f"RUNNING {pids[0]} {host}:{port}")
                sys.exit(0)
            print(f"RUNNING ? {host}:{port}")
            sys.exit(0)
        meta = _read_meta()
        if meta.get("pid") and _pid_running(meta.get("pid")):
            print(f"RUNNING {meta['pid']} {meta.get('host', host)}:{meta.get('port', port)}")
            sys.exit(0)
        pids = find_server_pids()
        if pids:
            print(f"RUNNING {pids[0]} {host}:{port}")
            sys.exit(0)
        print("STOPPED")
        sys.exit(1)
    if cmd == "stop":
        pids = _collect_pids(host, port)
        if pids:
            _terminate_pids(pids, signal.SIGTERM)
            ok = _wait_stopped(host, port, pids, timeout=3.0)
            if not ok:
                _terminate_pids(pids, signal.SIGKILL)
                _wait_stopped(host, port, pids, timeout=2.0)
        _remove_pid()
        if check_server_alive(host, port, timeout=0.5) or any(_pid_running(p) for p in pids):
            print("STOPPED_WITH_ISSUE")
            sys.exit(1)
        print("STOPPED")
        sys.exit(0)
    try:
        httpd = ReuseAddrServer((host, port), RequestHandler)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):
            if check_server_alive(host, port, timeout=1.0):
                print(f"INFO: 服务已在 {host}:{port} 运行")
                sys.exit(0)
            print(f"ERROR: 端口占用 {host}:{port}，但不是本服务。请更换端口或释放该端口。", file=sys.stderr)
            sys.exit(1)
        raise
    HTTPD = httpd
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    _write_pid()
    atexit.register(_remove_pid)
    httpd.serve_forever()
