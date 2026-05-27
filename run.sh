#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "未找到虚拟环境，请先运行: bash install.sh"
    exit 1
fi

# 激活虚拟环境并运行
source .venv/bin/activate

# 本地连接不走代理，避免 httpx 报 socks 代理错误
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

# 修复中文输入：自动检测输入法框架
if command -v fcitx5 &> /dev/null || command -v fcitx &> /dev/null; then
    export QT_IM_MODULE=fcitx
elif command -v ibus-daemon &> /dev/null; then
    export QT_IM_MODULE=ibus
fi

# Wayland 下强制 X11，避免 PyQt5 中文输入和显示问题
export QT_QPA_PLATFORM=xcb

python3 main.py
