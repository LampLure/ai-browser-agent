# ============================================================
# AI 浏览器助手 - 配置文件
# ============================================================
# 所有配置都在这里修改，无需改动其他文件

# --- LLM 配置 ---
# llama-server 的地址（你的 Gemma 4 启动脚本里 --port 8080）
LLM_BASE_URL = "http://localhost:8080/v1"
LLM_API_KEY = "not-needed"
LLM_MODEL = "gemma-4"

# --- 浏览器配置 ---
BROWSER_HEADLESS = False          # False = 有头模式，你能看到浏览器窗口
BROWSER_START_URL = "https://www.bing.com"  # 浏览器启动后打开的首页

# --- Agent 配置 ---
MAX_STEPS = 20                    # 单次任务最大操作步数
MAX_DOM_ELEMENTS = 50             # 提取的最大页面元素数
ACTION_DELAY = 1.5                # 每步操作后的等待秒数（让页面加载）
USE_VISION = False                # 是否发送截图给AI（4B小模型建议关闭，省token）
