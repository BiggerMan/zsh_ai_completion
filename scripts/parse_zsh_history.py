#!/usr/bin/env python3
import os
import re

# 全局变量：指定历史命令输出的临时文件路径
# 默认值为当前工作目录下的 history.txt
# 可手动修改此变量，或通过设置环境变量 ZSH_AI_HISTORY_FILE 覆盖
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_HISTORY_FILE = os.path.join(PROJECT_ROOT, "data", "history.txt")
HISTORY_OUTPUT_FILE = os.environ.get("ZSH_AI_HISTORY_FILE", DEFAULT_HISTORY_FILE)

def load_zsh_history():
    """读取并清理 Zsh 历史文件（适配带空格的时间戳格式 + 过滤多行命令）"""
    history_path = os.path.expanduser("~/.zsh_history")
    if not os.path.exists(history_path):
        return []
    
    # 修正正则：匹配格式为 ": 数字:数字;" 的时间戳前缀（含空格）
    timestamp_pattern = re.compile(r'^:\s*\d+:\d+;')
    clean_commands = []
    
    with open(history_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line_stripped = line.strip()
            # 移除时间戳前缀
            cleaned = timestamp_pattern.sub('', line_stripped)
            # 过滤条件新增：排除包含反斜杠（\）的内容（多行命令）
            if (cleaned and 
                not cleaned.startswith("#") and 
                len(cleaned) > 1 and 
                '\\' not in cleaned):  # 核心新增：过滤带\的行
                clean_commands.append(cleaned)
    
    # 去重（保留最新的顺序）+ 限制数量（避免数据量过大）
    unique_commands = list(dict.fromkeys(reversed(clean_commands)))[:1000]
    # 再反转回来，恢复时间从旧到新的顺序
    return reversed(unique_commands)

if __name__ == "__main__":
    history = load_zsh_history()
    # 转换为列表便于计数和写入
    history_list = list(history)
    
    # 确保输出目录存在（如果指定的路径包含子目录）
    output_dir = os.path.dirname(HISTORY_OUTPUT_FILE)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
    
    # 将历史命令写入配置的临时文件
    with open(HISTORY_OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(history_list))
    print(f"已解析 {len(history_list)} 条有效 Shell 历史命令")
    print(f"历史命令已保存至：{HISTORY_OUTPUT_FILE}")
