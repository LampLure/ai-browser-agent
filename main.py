#!/usr/bin/env python3
"""
AI 浏览器助手 - 入口
"""

import sys
from PyQt5.QtWidgets import QApplication
from ui_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AI 浏览器助手")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
