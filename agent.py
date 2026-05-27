"""
AI 浏览器助手 - Agent 核心模块
在独立 QThread 中运行 Playwright 浏览器 + LLM 推理循环
支持多标签页感知，默认操作用户当前正在看的标签页
"""

import queue
import json
import re
import time
import traceback
import threading
import base64
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
截图观察: {"action":"screenshot","question":"想了解什么"}
总结页面内容: {"action":"summarize"}
切换标签页: {"action":"switch_tab","tab":0}
完成任务: {"action":"done","message":"结果"}
询问用户: {"action":"ask","message":"问题"}

## 铁律（必须遵守）
1. 严格只做用户要求的操作！不要自作主张多走任何一步。
2. 用户说"前往/打开xxx网站"→ 只用goto导航，完成后立即done。不要在搜索框输入网址！
3. 用户说"搜索xxx"→ 输入搜索词+回车，然后立即done。不要点击搜索建议、推荐分类、热搜标签等！
4. 用户说"总结/读一下"→ 用summarize，返回页面正文内容。
5. 只有用户明确说"找一下/看看有没有/探索"时，才可以点击链接浏览。
6. type的enter=true自动按回车，搜索时必须加。
7. 不要点击搜索框下方的推荐词、分类标签、热搜等内容。
8. 默认操作用户当前正在看的标签页。页面状态中会标注"[当前标签]"。
9. 只有用户明确指定"在第X个标签页操作"时才用switch_tab切换。
10. 截图(screenshot)是最后手段！只有当页面元素列表的文字信息不足以完成任务时才截图（例如：需要看图片/验证码/复杂布局、元素无法定位等）。如果文字信息已经足够，绝对不要截图！每次只能截1张。"""

# 总结页面内容时的专用提示词
SUMMARIZE_PROMPT = """用户要求总结当前页面内容。请阅读以下页面正文，用中文简洁总结要点。直接输出总结文本，不需要JSON。"""


class AgentThread(QThread):
    """
    Agent 工作线程 - 拥有 Playwright 和 LLM 实例
    通过命令队列接收指令，通过信号向 UI 报告状态
    支持多标签页感知，默认操作用户正在看的标签页
    """

    # ── Qt 信号 ──
    message = pyqtSignal(str, str)       # (role, text)
    state_changed = pyqtSignal(str)      # "ready" / "busy" / "idle" / "closed"
    agent_done = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._queue = queue.Queue()
        self._running = True
        self._stop_task_flag = False
        self._stop_event = threading.Event()
        self._last_active_page = None  # 追踪最近确认的活跃标签页

    # ── 外部调用接口 ──

    def launch_browser(self):
        self._queue.put({"type": "launch_browser"})

    def send_task(self, task: str):
        self._queue.put({"type": "task", "task": task})

    def stop_task(self):
        self._stop_task_flag = True
        self._stop_event.set()

    def shutdown(self):
        self._running = False
        self._stop_event.set()
        self._queue.put({"type": "shutdown"})

    # ── 线程主循环 ──

    def run(self):
        from playwright.sync_api import sync_playwright
        from openai import OpenAI

        pw = sync_playwright().start()
        browser = None
        context = None
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

                    # 注入 visibility 追踪脚本到所有新页面
                    context.add_init_script("""
                        document.addEventListener('visibilitychange', () => {
                            document.documentElement.setAttribute(
                                'data-ai-visible',
                                document.visibilityState
                            );
                        });
                        document.documentElement.setAttribute(
                            'data-ai-visible',
                            document.visibilityState
                        );
                    """)

                    # 监听新标签页打开
                    context.on("page", self._on_new_page)

                    page = context.new_page()
                    page.goto(config.BROWSER_START_URL, timeout=15000)
                    self._last_active_page = page
                    self.message.emit("system", "浏览器已启动！现在可以输入任务了。")
                    self.state_changed.emit("idle")
                except Exception as e:
                    self.message.emit("error", f"启动浏览器失败: {e}")
                    self.state_changed.emit("closed")

            # ── 执行任务 ──
            elif cmd_type == "task":
                if context is None:
                    self.message.emit("error", "请先打开浏览器")
                    continue
                self.state_changed.emit("busy")
                self._stop_task_flag = False
                self._stop_event.clear()

                # 获取用户当前正在看的标签页
                active_page = self._get_active_page(context)
                if active_page is None:
                    self.message.emit("error", "没有可用的标签页")
                    self.state_changed.emit("idle")
                    self.agent_done.emit()
                    continue

                # 提示用户 AI 识别到的当前标签页（含调试信息）
                try:
                    pages = context.pages
                    if len(pages) > 1:
                        # 显示所有标签页的可见性状态
                        vis_info = []
                        for i, p in enumerate(pages):
                            try:
                                vis = p.evaluate("document.visibilityState")
                            except:
                                vis = "?"
                            is_current = " ←当前" if p is active_page else ""
                            vis_info.append(f"  标签{i}: vis={vis}{is_current}")
                        self.message.emit("system",
                            "标签页检测:\n" + "\n".join(vis_info))
                except:
                    pass

                self._run_agent_loop(active_page, context, llm, history, cmd["task"])
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

    # ── 标签页管理 ──

    def _on_new_page(self, new_page):
        """新标签页打开时的回调"""
        try:
            title = new_page.title() or "新标签页"
            url = new_page.url or ""
            self.message.emit("system", f"新标签页已打开: {title[:50]}")
        except:
            self.message.emit("system", "新标签页已打开")

    def _get_active_page(self, context):
        """
        获取用户当前正在看的标签页。
        多重检测策略，从最可靠到兜底：
        1. document.visibilityState + data-ai-visible 属性
        2. document.hasFocus()（浏览器窗口有系统焦点时）
        3. 记录的最近活跃页面 _last_active_page
        4. 最后一个标签页

        关键：document.hasFocus() 要求浏览器窗口本身有系统焦点，
        当用户在 PyQt5 窗口输入时浏览器没有焦点，所有标签页都返回 false。
        document.visibilityState === "visible" 只需要该标签页是浏览器中
        当前显示的那个，不受系统焦点影响。我们还通过 add_init_script
        注入了 visibilitychange 监听器，将状态同步到 data-ai-visible 属性，
        作为双重保障。
        """
        try:
            pages = context.pages
            if not pages:
                return None

            # 只有一个标签页，直接返回
            if len(pages) == 1:
                self._last_active_page = pages[0]
                return pages[0]

            # 收集每个标签页的可见性信息
            page_vis = []
            for i, page in enumerate(pages):
                try:
                    # 方法1: 直接检查 visibilityState
                    vis = page.evaluate("document.visibilityState")
                    # 方法2: 检查注入的 data-ai-visible 属性（双重保障）
                    attr_vis = page.evaluate(
                        "document.documentElement.getAttribute('data-ai-visible') || 'unknown'"
                    )
                    # 如果任一方法返回 visible，就认为该标签页是可见的
                    is_visible = (vis == "visible") or (attr_vis == "visible")
                    page_vis.append((i, page, vis, attr_vis, is_visible))
                except:
                    page_vis.append((i, page, "error", "error", False))

            # 策略1: 找到唯一 visible 的标签页
            visible_pages = [p for p in page_vis if p[4]]
            if len(visible_pages) == 1:
                self._last_active_page = visible_pages[0][1]
                return visible_pages[0][1]
            elif len(visible_pages) > 1:
                # 多个 visible，用 hasFocus 区分
                for i, page, vis, attr_vis, _ in visible_pages:
                    try:
                        if page.evaluate("document.hasFocus()"):
                            self._last_active_page = page
                            return page
                    except:
                        continue
                # 都没焦点，返回 visible 列表中的最后一个
                self._last_active_page = visible_pages[-1][1]
                return visible_pages[-1][1]

            # 策略2: 用 hasFocus 找有焦点的标签页
            for i, page, vis, attr_vis, _ in page_vis:
                try:
                    if page.evaluate("document.hasFocus()"):
                        self._last_active_page = page
                        return page
                except:
                    continue

            # 策略3: 使用记录的最近活跃页面
            if self._last_active_page:
                try:
                    if self._last_active_page in pages:
                        return self._last_active_page
                except:
                    pass

            # 策略4: 兜底，最后一个标签页
            self._last_active_page = pages[-1]
            return pages[-1]
        except:
            return None

    def _get_tabs_info(self, context) -> str:
        """获取所有标签页信息，标注当前活跃的，同时显示可见性状态"""
        try:
            pages = context.pages
            if not pages:
                return "标签页: 无"

            lines = []
            active_page = self._get_active_page(context)

            for i, page in enumerate(pages):
                try:
                    title = page.title() or "空白页"
                    url = page.url or ""
                    is_current = " [当前标签]" if page is active_page else ""
                    # 获取可见性状态用于调试
                    try:
                        vis = page.evaluate("document.visibilityState")
                    except:
                        vis = "?"
                    vis_mark = "👁" if vis == "visible" else "🔸"
                    lines.append(f"  {vis_mark} 标签{i}: {title[:40]} - {url[:60]}{is_current}")
                except:
                    lines.append(f"  🔸 标签{i}: (无法读取)")

            return "打开的标签页:\n" + "\n".join(lines)
        except:
            return "标签页信息获取失败"

    # ── Agent 主循环 ──

    def _run_agent_loop(self, page, context, llm, history, task):
        """核心 Agent 循环"""
        self.message.emit("ai", f"开始执行: {task}")

        # 检测是否是总结/阅读类任务
        if self._is_summarize_task(task):
            active = self._get_active_page(context) or page
            self._do_summarize(active, llm, task)
            return

        # 首轮 prompt：包含标签页信息 + 当前页面元素
        tabs_info = self._get_tabs_info(context)
        page_state = self._get_page_state(page)
        first_prompt = f"用户任务: {task}\n\n{tabs_info}\n\n{page_state}"
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
                    tabs_info = self._get_tabs_info(context)
                    page_state = self._get_page_state(page)
                    history.append({"role": "user", "content": f"请用JSON格式输出操作。\n\n{tabs_info}\n\n{page_state}"})
                    continue

                action_type = action.get("action", "")

                # 切换标签页
                if action_type == "switch_tab":
                    tab_idx = int(action.get("tab", 0))
                    pages = context.pages
                    if 0 <= tab_idx < len(pages):
                        page = pages[tab_idx]
                        page.bring_to_front()
                        self._last_active_page = page  # 更新追踪
                        self.message.emit("ai", f"已切换到标签{tab_idx}: {page.title()[:40]}")
                        if self._interruptible_sleep(0.5):
                            return
                    else:
                        self.message.emit("error", f"标签页 {tab_idx} 不存在，共 {len(pages)} 个标签")
                    # 切换后继续循环让 AI 看新页面
                    tabs_info = self._get_tabs_info(context)
                    page_state = self._get_page_state(page)
                    history.append({"role": "user", "content": f"已切换标签页。\n\n{tabs_info}\n\n{page_state}"})
                    continue

                # summarize 动作
                if action_type == "summarize":
                    active = self._get_active_page(context) or page
                    self._do_summarize(active, llm, "总结当前页面内容")
                    return

                # screenshot 动作：截图发给视觉模型理解
                if action_type == "screenshot":
                    question = action.get("question", "请描述当前页面内容")
                    vision_result = self._do_screenshot_vision(page, llm, question)
                    if vision_result:
                        self.message.emit("ai", f"👀 截图观察结果: {vision_result[:500]}")
                        # 把观察结果加入对话，让 AI 继续决策
                        tabs_info = self._get_tabs_info(context)
                        page_state = self._get_page_state(page)
                        history.append({"role": "user",
                            "content": f"截图观察结果: {vision_result}\n\n{tabs_info}\n\n{page_state}"})
                    else:
                        self.message.emit("ai", "截图观察失败，继续基于文字信息操作")
                    continue

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

                # 每步操作后重新检测活跃标签页（用户可能手动切了标签）
                new_active = self._get_active_page(context)
                if new_active and new_active is not page:
                    page = new_active
                    self._last_active_page = page  # 更新追踪
                    self.message.emit("system", f"检测到切换标签页: {page.title()[:40]}")

                tabs_info = self._get_tabs_info(context)
                page_state = self._get_page_state(page)
                history.append({"role": "user", "content": f"操作完成。\n\n{tabs_info}\n\n{page_state}"})

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
        keywords = ["总结", "读一下", "看看内容", "内容是什么", "说了什么",
                     "阅读", "读读", "概括", "摘要", "总结一下",
                     "summarize", "read", "内容"]
        return any(kw in task.lower() for kw in keywords)

    # ── 总结页面内容 ──

    def _do_summarize(self, page, llm, task):
        self.message.emit("ai", "正在读取页面内容...")

        page_text = self._get_page_text(page)
        if not page_text or len(page_text.strip()) < 20:
            self.message.emit("ai", "✅ 当前页面没有可读取的正文内容")
            return

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

    # ── 截图视觉理解 ──

    def _do_screenshot_vision(self, page, llm, question: str) -> str:
        """
        截取当前页面截图，发送给 LLM 视觉模型理解。
        每次只截1张，返回视觉理解结果。
        """
        self.message.emit("ai", "📷 正在截图...")

        try:
            # 截图并压缩为 JPEG 以减少 token 消耗
            screenshot_bytes = page.screenshot(type="jpeg", quality=75)
            b64_image = base64.b64encode(screenshot_bytes).decode("utf-8")

            self.message.emit("ai", "👀 正在分析截图...")

            # 构造多模态消息（OpenAI 兼容格式）
            vision_messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"你正在操作浏览器。用户问题: {question}\n\n请根据截图简要描述页面内容，重点关注：1)页面显示了什么 2)有哪些可操作元素 3)当前状态。用中文回答，简洁为主。"
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}"
                            }
                        }
                    ]
                }
            ]

            result = [None]
            error = [None]

            def _vision_worker():
                try:
                    resp = llm.chat.completions.create(
                        model=config.LLM_MODEL,
                        messages=vision_messages,
                        max_tokens=500,
                        temperature=0.3,
                    )
                    result[0] = resp.choices[0].message.content
                except Exception as e:
                    error[0] = e

            t = threading.Thread(target=_vision_worker, daemon=True)
            t.start()

            while t.is_alive():
                if self._stop_task_flag or not self._running:
                    return ""
                t.join(timeout=0.5)

            if error[0]:
                self.message.emit("error", f"视觉分析失败: {error[0]}")
                return ""

            return result[0] or ""

        except Exception as e:
            self.message.emit("error", f"截图失败: {e}")
            return ""

    # ── 页面正文提取 ──

    def _get_page_text(self, page) -> str:
        try:
            return page.evaluate("""() => {
                const selectors = ['article', 'main', '[role="main"]', '.content', '.article', '.post'];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.textContent.trim().length > 100) {
                        return el.textContent.trim().substring(0, 8000);
                    }
                }
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
        return self._stop_event.wait(timeout=seconds)

    # ── 可中断的 LLM 调用 ──

    def _call_llm_interruptible(self, llm, history):
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
