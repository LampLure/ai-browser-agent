#!/bin/bash
set -e

echo "========================================="
echo "   AI 浏览器助手 - 一键安装"
echo "========================================="
echo ""

# 检查 Python3
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "检测到 Python $PYTHON_VERSION"

# 创建虚拟环境
if [ ! -d ".venv" ]; then
    echo ""
    echo "[1/3] 创建虚拟环境..."
    python3 -m venv .venv
else
    echo ""
    echo "[1/3] 虚拟环境已存在，跳过"
fi

# 激活并安装依赖
echo ""
echo "[2/3] 安装 Python 依赖..."
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt

# 安装 Playwright 浏览器
echo ""
echo "[3/3] 安装 Playwright Chromium（首次安装较慢）..."
playwright install chromium
playwright install-deps chromium 2>/dev/null || true

echo ""
echo "========================================="
echo "   安装完成！"
echo "   运行方式: bash run.sh"
echo "========================================="
