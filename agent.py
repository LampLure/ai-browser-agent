"""
AI 浏览器助手 - Agent 核心模块
在独立 QThread 中运行 Playwright 浏览器 + LLM 推理循环
"""

import queue
import json
import re
import time
import traceback
import threading
from PyQt5.QtCore import QThread, pyqtSignal

import config


# ============================================================
# 系统提示词 - 专为小模型设计，尽量简洁
# ============================================================
SYSTEM_PROMPT = """你是浏览器操作AI。根据页面元素列表执行操作。

## 操作格式（每次只输出一个JSON）
导航到网站: {"action":"goto","url":"https://example.com"}
在输入框输入文字: {"action":"type","idx":1,"text":"搜索词","enter":true}
点击元素: {"action":"click","idx":0}
按键: {"action":"press","key":"Enter"}
滚动: {"action":"scroll","direction":"down"}
等待: {"action":"wait","seconds":2}
完成: {"action":"done","message":"结果说明"}
需要更多信息: {"action":"ask","message":"问题"}

## 关键规则
1. 用户说"前往/打开/去 xxx网站"时，必须用goto导航，不要在当前页面输入框输入网址！
2. type操作的enter=true会自动按回车提交，搜索时务必加上！
3. 每次只输出一个操作，不要一次输出多个。
4. idx 从页面元素列表中获取。

先简短思考1句话，再输出JSON，用```json和```包裹。"""


class AgentThread(QThread):
    """
    Agent 工作线程 - 拥有 Playwright 和 LLM 实例
    通过命令队列接收指令，通过信号向 UI 报告状态
    """

    # ── Qt 信号 ──
    message = pyqtSignal(str, str)       # (role, text)  role: user/ai/system/error
    state_changed = pyqtSignal(str)      # "ready" / "busy" / "idle" / "closed"
    agent_done = pyqtSignal()            # 单次任务完成

    def __init__(self):
        super().__init__()
        self._queue = queue.Queue()
        self._running = True
        self._stop_task_flag = False
        self._stop_event = threading.Event()  # 用于立即中断 LLM 等待

    # ── 外部调用接口（主线程调用） ──

    def launch_browser(self):
        """请求启动浏览器"""
        self._queue.put({"type": "launch_browser"})

    def send_task(self, task: str):
        """发送一个任务"""
        self._queue.put({"type": "task", "task": task})

    def stop_task(self):
        """立即停止当前任务"""
        self._stop_task_flag = True
        self._stop_event.set()  # 立即唤醒所有等待

    def shutdown(self):
        """关闭整个 Agent"""
        self._running = False
        self._stop_event.set()
        self._queue.put({"type": "shutdown"})

    # ── 线程主循环 ──

    def run(self):
        from playwright.sync_api import sync_playwright
        from openai import OpenAI

        pw = sync_playwright().start()
        browser = None
        page = None
        llm = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
        history = []

        while self._running:
            # 取命令（带超时，避免死锁）
            try:
                cmd = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            cmd_type = cmd.get("type")

            # ── 启动浏览器 ──
            if cmd_type == "launch_browser":
                try:
                    browser = pw.chromium.launch(
                        headless=config.BROWSER_HEADLESS,
                        args=["--start-maximized"]  # 最大化窗口
                    )
                    # no_viewport=True 让页面内容自动填满整个浏览器窗口
                    context = browser.new_context(no_viewport=True)
                    page = context.new_page()
                    page.goto(config.BROWSER_START_URL, timeout=15000)
                    self.message.emit("system", "浏览器已启动！现在可以输入任务了。")
                    self.state_changed.emit("idle")
                except Exception as e:
                    self.message.emit("error", f"启动浏览器失败: {e}")
                    self.state_changed.emit("closed")

            # ── 执行任务 ──
            elif cmd_type == "task":
                if page is None:
                    self.message.emit("error", "请先打开浏览器")
                    continue
                self.state_changed.emit("busy")
                self._stop_task_flag = False
                self._stop_event.clear()
                self._run_agent_loop(page, llm, history, cmd["task"])
                history.clear()  # 每次任务清空历史
                if self._stop_task_flag:
                    self.message.emit("ai", "任务已停止")
                self.state_changed.emit("idle")
                self.agent_done.emit()

            # ── 关闭 ──
            elif cmd_type == "shutdown":
                break

        # 清理
        if browser:
            try:
                browser.close()
            except:
                pass
        pw.stop()
        self.state_changed.emit("closed")

    # ── Agent 主循环 ──

    def _run_agent_loop(self, page, llm, history, task):
        """核心 Agent 循环：获取页面状态 → 发给 LLM → 解析动作 → 执行"""
        self.message.emit("ai", f"开始执行: {task}")

        # 首轮 prompt
        first_prompt = f"用户任务: {task}\n\n{self._get_page_state(page)}"

        # 构建历史
        history.append({"role": "user", "content": first_prompt})

        for step in range(config.MAX_STEPS):
            # ── 立即检查停止标志 ──
            if not self._running or self._stop_task_flag:
                return

            try:
                # 1. 调用 LLM（带中断检测）
                self.message.emit("ai", "思考中...")
                response = self._call_llm_interruptible(llm, history)
                if response is None:
                    # 被中断
                    return
                if not response:
                    self.message.emit("error", "LLM 返回为空，请检查 llama-server 是否运行")
                    return

                history.append({"role": "assistant", "content": response})

                # 2. 解析动作
                action = self._parse_action(response)
                if action is None:
                    # 没解析出 JSON，把 AI 的文本回复显示出来
                    self.message.emit("ai", response[:500])
                    # 再问一次
                    if self._stop_task_flag:
                        return
                    history.append({"role": "user", "content": f"请用JSON格式输出操作。当前页面状态:\n{self._get_page_state(page)}"})
                    continue

                action_type = action.get("action", "")

                # 3. 处理终止类动作
                if action_type in ("done", "ask"):
                    msg = action.get("message", "操作完成")
                    icon = "✅" if action_type == "done" else "❓"
                    self.message.emit("ai", f"{icon} {msg}")
                    return

                # 4. 执行动作
                self._execute_action(page, action)

                # 5. 可中断等待（让页面加载）
                if self._interruptible_sleep(config.ACTION_DELAY):
                    return

                # 6. 把新页面状态加入对话
                new_state = self._get_page_state(page)
                history.append({"role": "user", "content": f"操作完成。当前页面状态:\n{new_state}"})

                # 7. 控制历史长度（防止超上下文）
                if len(history) > 16:
                    history[:] = history[-12:]

            except Exception as e:
                self.message.emit("error", f"步骤{step+1}出错: {e}")
                traceback.print_exc()
                if self._interruptible_sleep(1):
                    return
        else:
            self.message.emit("ai", f"已达到最大步数({config.MAX_STEPS})，任务可能未完成")

    # ── 可中断的等待 ──

    def _interruptible_sleep(self, seconds):
        """可中断的 sleep，返回 True 表示被停止"""
        return self._stop_event.wait(timeout=seconds)

    # ── 可中断的 LLM 调用 ──

    def _call_llm_interruptible(self, llm, history):
        """在子线程中调用 LLM，主线程可中断"""
        result = [None]
        error = [None]

        def _worker():
            try:
                resp = llm.chat.completions.create(
                    model=config.LLM_MODEL,
                    messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
                    max_tokens=300,
                    temperature=0.3,
                )
                result[0] = resp.choices[0].message.content
            except Exception as e:
                error[0] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        # 等待 LLM 返回或停止信号
        while t.is_alive():
            if self._stop_task_flag or not self._running:
                return None
            t.join(timeout=0.2)

        if error[0]:
            self.message.emit("error", f"LLM调用失败: {error[0]}")
            return ""

        return result[0]

    # ── 页面状态提取 ──

    def _get_page_state(self, page) -> str:
        """提取当前页面的可交互元素列表"""
        try:
            # 注入 JS：给每个交互元素加 data-ai-idx 属性，然后提取信息
            page.evaluate("""() => {
                const els = document.querySelectorAll(
                    'a, button, input, textarea, select, [role="button"], [onclick], [tabindex]:not([tabindex="-1"])'
                );
                els.forEach((el, i) => el.setAttribute('data-ai-idx', String(i)));
            }""")

            dom = page.evaluate("""() => {
                const result = [];
                const els = document.querySelectorAll('[data-ai-idx]');
                const vh = window.innerHeight;
                for (const el of els) {
                    const rect = el.getBoundingClientRect();
                    if (rect.bottom < 0 || rect.top > vh) continue;
                    if (rect.width === 0 && rect.height === 0) continue;
                    const idx = el.getAttribute('data-ai-idx');
                    const tag = el.tagName.toLowerCase();
                    const text = (el.textContent || '').trim().substring(0, 80);
                    const type_ = el.type || '';
                    const placeholder = el.placeholder || '';
                    const href = (el.href || '').substring(0, 100);
                    const ariaLabel = el.getAttribute('aria-label') || '';
                    result.push({ idx, tag, text, type: type_, placeholder, href, ariaLabel });
                }
                return result.slice(0, """ + str(config.MAX_DOM_ELEMENTS) + """);
            }""")

            url = page.url
            title = page.title()

            lines = [f"页面: {title}", f"URL: {url}", ""]
            for el in dom:
                desc = f"[{el['idx']}] <{el['tag']}>"
                if el.get('type'):
                    desc += f" type={el['type']}"
                if el.get('text'):
                    desc += f' "{el["text"]}"'
                if el.get('placeholder'):
                    desc += f' placeholder="{el["placeholder"]}"'
                if el.get('ariaLabel'):
                    desc += f' aria="{el["ariaLabel"]}"'
                if el.get('href') and el['tag'] == 'a':
                    desc += f' href="{el["href"]}"'
                lines.append(desc)

            return "\n".join(lines)

        except Exception as e:
            return f"页面状态获取失败: {e}"

    # ── 动作解析 ──

    def _parse_action(self, response: str):
        """从 LLM 回复中解析 JSON 动作"""
        # 尝试1: ```json ... ``` 代码块
        m = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试2: ``` ... ``` 代码块（没有 json 标记）
        m = re.search(r'```\s*(.*?)\s*```', response, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试3: 直接找 JSON 对象
        m = re.search(r'\{[^{}]*\}', response, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        return None

    # ── 动作执行 ──

    def _execute_action(self, page, action: dict):
        """执行一个浏览器动作"""
        if self._stop_task_flag:
            return

        atype = action.get("action", "")

        try:
            if atype == "click":
                idx = action.get("idx", 0)
                selector = f'[data-ai-idx="{idx}"]'
                self.message.emit("ai", f"点击元素 [{idx}]")
                page.click(selector, timeout=5000)

            elif atype == "type":
                idx = action.get("idx", 0)
                text = action.get("text", "")
                do_enter = action.get("enter", False)
                selector = f'[data-ai-idx="{idx}"]'
                enter_hint = " + Enter" if do_enter else ""
                self.message.emit("ai", f'在 [{idx}] 输入 "{text}"{enter_hint}')
                page.fill(selector, text, timeout=5000)
                if do_enter:
                    page.keyboard.press("Enter")

            elif atype == "press":
                key = action.get("key", "Enter")
                self.message.emit("ai", f"按键: {key}")
                page.keyboard.press(key)

            elif atype == "scroll":
                direction = action.get("direction", "down")
                delta = 500 if direction == "down" else -500
                self.message.emit("ai", f"滚动: {direction}")
                page.evaluate(f"window.scrollBy(0, {delta})")

            elif atype == "goto":
                url = action.get("url", "")
                if not url.startswith("http"):
                    url = "https://" + url
                self.message.emit("ai", f"导航到: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=15000)

            elif atype == "wait":
                seconds = float(action.get("seconds", 2))
                self.message.emit("ai", f"等待 {seconds} 秒")
                self._interruptible_sleep(seconds)

            else:
                self.message.emit("ai", f"未知操作: {atype}")

        except Exception as e:
            self.message.emit("error", f"执行 {atype} 失败: {e}")
