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
# 系统提示词 - 严格约束 + 高自由度
# ============================================================
SYSTEM_PROMPT = """你是浏览器操作AI。严格按用户指令操作，不多做一步。

## 可用操作（每次只输出一个JSON）
导航到网站: {"action":"goto","url":"https://example.com"}
在输入框输入: {"action":"type","idx":1,"text":"内容","enter":true}
点击元素: {"action":"click","idx":0}
按键: {"action":"press","key":"Enter"}
滚动页面: {"action":"scroll","direction":"down"}
等待加载: {"action":"wait","seconds":2}
总结页面内容: {"action":"summarize"}
完成任务: {"action":"done","message":"结果"}
询问用户: {"action":"ask","message":"问题"}

## 铁律（必须遵守）
1. 严格只做用户要求的操作！不要自作主张多走任何一步。
2. 用户说"前往/打开xxx网站"→ 只用goto导航，完成后立即done。不要在搜索框输入网址！
3. 用户说"搜索xxx"→ 输入搜索词+回车，然后立即done。不要点击搜索建议、推荐分类、热搜标签等！
4. 用户说"总结/读一下"→ 用summarize，返回页面正文内容。
5. 只有用户明确说"找一下/看看有没有/探索"时，才可以点击链接浏览。
6. type的enter=true自动按回车，搜索时必须加。
7. 不要点击搜索框下方的推荐词、分类标签、热搜等内容。"""

# 总结页面内容时的专用提示词
SUMMARIZE_PROMPT = """用户要求总结当前页面内容。请阅读以下页面正文，用中文简洁总结要点。直接输出总结文本，不需要JSON。"""


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
        self._stop_event = threading.Event()

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
        self._stop_event.set()

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
                        args=["--start-maximized"]
                    )
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
                history.clear()
                if self._stop_task_flag:
                    self.message.emit("ai", "任务已停止")
                self.state_changed.emit("idle")
                self.agent_done.emit()

            # ── 关闭 ──
            elif cmd_type == "shutdown":
                break

        if browser:
            try:
                browser.close()
            except:
                pass
        pw.stop()
        self.state_changed.emit("closed")

    # ── Agent 主循环 ──

    def _run_agent_loop(self, page, llm, history, task):
        """核心 Agent 循环"""
        self.message.emit("ai", f"开始执行: {task}")

        # 检测是否是总结/阅读类任务 → 走特殊路径
        if self._is_summarize_task(task):
            self._do_summarize(page, llm, task)
            return

        first_prompt = f"用户任务: {task}\n\n{self._get_page_state(page)}"
        history.append({"role": "user", "content": first_prompt})

        for step in range(config.MAX_STEPS):
            if not self._running or self._stop_task_flag:
                return

            try:
                self.message.emit("ai", "思考中...")
                response = self._call_llm_interruptible(llm, history)
                if response is None:
                    return
                if not response:
                    self.message.emit("error", "LLM 返回为空，请检查 llama-server 是否运行")
                    return

                history.append({"role": "assistant", "content": response})

                action = self._parse_action(response)
                if action is None:
                    self.message.emit("ai", response[:500])
                    if self._stop_task_flag:
                        return
                    history.append({"role": "user", "content": f"请用JSON格式输出操作。当前页面状态:\n{self._get_page_state(page)}"})
                    continue

                action_type = action.get("action", "")

                # summarize 动作：提取正文并总结
                if action_type == "summarize":
                    self._do_summarize(page, llm, "总结当前页面内容")
                    return

                # 终止类动作
                if action_type in ("done", "ask"):
                    msg = action.get("message", "操作完成")
                    icon = "✅" if action_type == "done" else "❓"
                    self.message.emit("ai", f"{icon} {msg}")
                    return

                # 执行动作
                self._execute_action(page, action)

                if self._interruptible_sleep(config.ACTION_DELAY):
                    return

                new_state = self._get_page_state(page)
                history.append({"role": "user", "content": f"操作完成。当前页面状态:\n{new_state}"})

                if len(history) > 16:
                    history[:] = history[-12:]

            except Exception as e:
                self.message.emit("error", f"步骤{step+1}出错: {e}")
                traceback.print_exc()
                if self._interruptible_sleep(1):
                    return
        else:
            self.message.emit("ai", f"已达到最大步数({config.MAX_STEPS})，任务可能未完成")

    # ── 判断是否总结类任务 ──

    def _is_summarize_task(self, task: str) -> bool:
        """检测用户任务是否为总结/阅读类"""
        keywords = ["总结", "读一下", "看看内容", "内容是什么", "说了什么",
                     "阅读", "读读", "概括", "摘要", "总结一下",
                     "summarize", "read", "内容"]
        task_lower = task.lower()
        return any(kw in task_lower for kw in keywords)

    # ── 总结页面内容 ──

    def _do_summarize(self, page, llm, task):
        """提取页面正文并让 LLM 总结"""
        self.message.emit("ai", "正在读取页面内容...")

        page_text = self._get_page_text(page)
        if not page_text or len(page_text.strip()) < 20:
            self.message.emit("ai", "✅ 当前页面没有可读取的正文内容")
            return

        # 截取正文，防止超出上下文
        max_chars = 6000
        if len(page_text) > max_chars:
            page_text = page_text[:max_chars] + "\n...(内容过长，已截断)"

        prompt = f"{SUMMARIZE_PROMPT}\n\n页面标题: {page.title()}\nURL: {page.url}\n\n页面正文:\n{page_text}"

        self.message.emit("ai", "正在总结...")

        result = [None]
        error = [None]

        def _worker():
            try:
                resp = llm.chat.completions.create(
                    model=config.LLM_MODEL,
                    messages=[
                        {"role": "system", "content": SUMMARIZE_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=800,
                    temperature=0.3,
                )
                result[0] = resp.choices[0].message.content
            except Exception as e:
                error[0] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        while t.is_alive():
            if self._stop_task_flag or not self._running:
                return
            t.join(timeout=0.2)

        if error[0]:
            self.message.emit("error", f"总结失败: {error[0]}")
            return

        if result[0]:
            self.message.emit("ai", f"✅ {result[0]}")
        else:
            self.message.emit("ai", "✅ 无法总结该页面")

    # ── 页面正文提取 ──

    def _get_page_text(self, page) -> str:
        """提取页面正文文本（去除导航、广告等无关内容）"""
        try:
            return page.evaluate("""() => {
                // 尝试提取 main/article 正文区域
                const selectors = ['article', 'main', '[role="main"]', '.content', '.article', '.post'];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.textContent.trim().length > 100) {
                        return el.textContent.trim().substring(0, 8000);
                    }
                }
                // 回退：提取 body，但排除 script/style/nav/footer/header
                const body = document.body.cloneNode(true);
                const removeTags = ['script', 'style', 'nav', 'footer', 'header', 'aside', 'noscript'];
                removeTags.forEach(tag => {
                    body.querySelectorAll(tag).forEach(el => el.remove());
                });
                return body.textContent.trim().substring(0, 8000);
            }""")
        except Exception as e:
            return f"内容提取失败: {e}"

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
        m = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        m = re.search(r'```\s*(.*?)\s*```', response, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

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
