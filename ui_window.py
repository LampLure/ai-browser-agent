"""
AI 浏览器助手 - PyQt5 主窗口
"""

import os
import urllib.request
import urllib.error
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextBrowser, QLineEdit, QPushButton, QApplication,
    QStatusBar, QMessageBox
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QTextCursor

from agent import AgentThread
import config


STYLESHEET = """
QMainWindow {
    background-color: #1e1e2e;
}
QTextBrowser {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 8px;
    padding: 10px;
    font-size: 14px;
}
QLineEdit {
    padding: 10px 14px;
    border: 2px solid #89b4fa;
    border-radius: 8px;
    background-color: #313244;
    color: #cdd6f4;
    font-size: 14px;
    selection-background-color: #89b4fa;
}
QLineEdit::placeholder {
    color: #6c7086;
}
QLineEdit:disabled {
    background-color: #1e1e2e;
    border-color: #45475a;
    color: #585b70;
}
QPushButton {
    padding: 10px 22px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: bold;
    border: none;
}
QPushButton:disabled {
    background-color: #45475a;
    color: #585b70;
}
QStatusBar {
    color: #a6adc8;
    background-color: #181825;
    font-size: 12px;
}
"""


def check_llm_server():
    """检查 llama-server 是否在运行"""
    try:
        url = config.LLM_BASE_URL.replace("/v1", "") + "/health"
        req = urllib.request.Request(url, method="GET")
        urllib.request.urlopen(req, timeout=3)
        return True
    except:
        try:
            # 尝试 /v1/models 端点
            url = config.LLM_BASE_URL + "/models"
            req = urllib.request.Request(url, method="GET")
            urllib.request.urlopen(req, timeout=3)
            return True
        except:
            return False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.agent = AgentThread()
        self._init_ui()
        self._connect_signals()
        self.agent.start()
        self._check_server_on_start()

    def _check_server_on_start(self):
        """启动时检查 AI 服务"""
        if check_llm_server():
            self._add_msg("system",
                "AI 服务已连接 ✅ (llama-server 运行中)<br><br>"
                "1. 点击「打开AI浏览器」启动浏览器<br>"
                "2. 输入任务，AI 将自动操作浏览器<br>"
                "3. 你可以实时看到浏览器的每一步操作"
            )
        else:
            self._add_msg("error",
                "未检测到 AI 服务！请先启动 llama-server<br>"
                f"预期地址: {config.LLM_BASE_URL}<br><br>"
                "启动后再点「打开AI浏览器」即可"
            )

    def _init_ui(self):
        self.setWindowTitle("AI 浏览器助手")
        self.setMinimumSize(750, 580)
        self.resize(820, 700)
        self.setStyleSheet(STYLESHEET)

        # ── 中央 Widget ──
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        layout.setContentsMargins(14, 14, 14, 8)

        # ── 对话展示区 ──
        self.chat = QTextBrowser()
        self.chat.setOpenExternalLinks(False)
        self.chat.setFont(QFont("Noto Sans SC", 10))
        layout.addWidget(self.chat, stretch=1)

        # ── 输入栏 ──
        input_row = QHBoxLayout()
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("输入任务，如：帮我搜索今天的天气")
        self.input_field.returnPressed.connect(self._on_send)
        input_row.addWidget(self.input_field, stretch=1)

        self.send_btn = QPushButton("发送")
        self.send_btn.setStyleSheet(
            "QPushButton{background:#a6e3a1;color:#1e1e2e;}"
            "QPushButton:hover{background:#94e2d5;}"
            "QPushButton:disabled{background:#45475a;color:#585b70;}"
        )
        self.send_btn.clicked.connect(self._on_send)
        input_row.addWidget(self.send_btn)
        layout.addLayout(input_row)

        # ── 按钮栏 ──
        btn_row = QHBoxLayout()

        self.browser_btn = QPushButton("打开AI浏览器")
        self.browser_btn.setStyleSheet(
            "QPushButton{background:#89b4fa;color:#1e1e2e;}"
            "QPushButton:hover{background:#74c7ec;}"
        )
        self.browser_btn.clicked.connect(self._on_toggle_browser)
        btn_row.addWidget(self.browser_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(
            "QPushButton{background:#f38ba8;color:#1e1e2e;}"
            "QPushButton:hover{background:#eba0ac;}"
            "QPushButton:disabled{background:#45475a;color:#585b70;}"
        )
        self.stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self.stop_btn)

        layout.addLayout(btn_row)

        # ── 状态栏 ──
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("就绪 - 请先打开AI浏览器")

    # ── 信号连接 ──

    def _connect_signals(self):
        self.agent.message.connect(self._add_msg)
        self.agent.state_changed.connect(self._on_state_changed)
        self.agent.agent_done.connect(self._on_agent_done)

    # ── 消息展示 ──

    def _add_msg(self, role: str, text: str):
        cursor = self.chat.textCursor()
        cursor.movePosition(QTextCursor.End)

        colors = {
            "user":   ("#89b4fa", "你"),
            "ai":     ("#a6e3a1", "AI"),
            "system": ("#cdd6f4", "系统"),
            "error":  ("#f38ba8", "错误"),
        }
        color, label = colors.get(role, ("#cdd6f4", role))

        if role == "system":
            cursor.insertHtml(
                f'<div style="margin:6px 0;padding:8px 12px;'
                f'background:#313244;border-radius:8px;">'
                f'<span style="color:{color};">{text}</span></div><br>'
            )
        elif role == "error":
            cursor.insertHtml(
                f'<div style="margin:6px 0;padding:8px 12px;'
                f'background:#45273a;border-radius:8px;">'
                f'<span style="color:{color};font-weight:bold;">{label}:</span> '
                f'<span style="color:#f5c2e7;">{text}</span></div><br>'
            )
        else:
            cursor.insertHtml(
                f'<div style="margin:4px 0;">'
                f'<span style="color:{color};font-weight:bold;">{label}:</span> '
                f'<span style="color:#cdd6f4;">{text}</span></div><br>'
            )

        self.chat.setTextCursor(cursor)
        self.chat.ensureCursorVisible()

    # ── 按钮事件 ──

    def _on_toggle_browser(self):
        if self.browser_btn.text().startswith("打开"):
            self.browser_btn.setEnabled(False)
            self.browser_btn.setText("启动中...")
            self.agent.launch_browser()
        else:
            self.agent.shutdown()
            self.agent = AgentThread()
            self.agent.message.connect(self._add_msg)
            self.agent.state_changed.connect(self._on_state_changed)
            self.agent.agent_done.connect(self._on_agent_done)
            self.agent.start()

    def _on_send(self):
        text = self.input_field.text().strip()
        if not text:
            return
        self.input_field.clear()
        self._add_msg("user", text)
        self.agent.send_task(text)
        self.send_btn.setEnabled(False)
        self.input_field.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def _on_stop(self):
        self.agent.stop_task()
        self.stop_btn.setEnabled(False)

    # ── Agent 状态回调 ──

    def _on_state_changed(self, state: str):
        if state == "idle":
            self.browser_btn.setText("关闭AI浏览器")
            self.browser_btn.setEnabled(True)
            self.browser_btn.setStyleSheet(
                "QPushButton{background:#fab387;color:#1e1e2e;}"
                "QPushButton:hover{background:#f9e2af;}"
            )
            self.status.showMessage("浏览器就绪 - 输入任务开始操作")
        elif state == "busy":
            self.status.showMessage("AI 正在操作浏览器...")
        elif state == "closed":
            self.browser_btn.setText("打开AI浏览器")
            self.browser_btn.setEnabled(True)
            self.browser_btn.setStyleSheet(
                "QPushButton{background:#89b4fa;color:#1e1e2e;}"
                "QPushButton:hover{background:#74c7ec;}"
            )
            self.status.showMessage("浏览器已关闭")

    def _on_agent_done(self):
        self.send_btn.setEnabled(True)
        self.input_field.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.input_field.setFocus()
        self.status.showMessage("任务完成 - 可以继续输入新任务")

    # ── 关闭清理 ──

    def closeEvent(self, event):
        self.agent.shutdown()
        self.agent.wait(3000)
        event.accept()
