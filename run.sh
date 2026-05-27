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
python3 main.py
