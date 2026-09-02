# -*- coding: utf-8 -*-
import webview

import config
from app_server import start_server

SERVER_PORT = 8765


class WindowApi:
    """前端通过 window.pywebview.api.minimize() / .close() / .resize(w, h) 调用。"""

    def __init__(self):
        self._window = None
        self._min_size = (860, 600)

    def set_window(self, window):
        self._window = window

    def set_min_size(self, size):
        self._min_size = size

    def minimize(self):
        if self._window:
            self._window.minimize()

    def close(self):
        if self._window:
            self._window.destroy()

    def resize(self, width: int, height: int, fix: str = "nw"):
        """自绘缩放手柄调用：调整窗口尺寸（带最小值下限）。

        fix 指定"固定哪个角不动"（窗口另一角随鼠标拉伸）：
        - "nw" 固定左上（拖右下角）
        - "ne" 固定右上（拖左下角）
        - "sw" 固定左下（拖右上角）
        - "se" 固定右下（拖左上角）
        """
        if not self._window:
            return
        w = max(int(width), self._min_size[0])
        h = max(int(height), self._min_size[1])
        from webview.window import FixPoint
        fix_map = {
            "nw": FixPoint.NORTH | FixPoint.WEST,
            "ne": FixPoint.NORTH | FixPoint.EAST,
            "sw": FixPoint.SOUTH | FixPoint.WEST,
            "se": FixPoint.SOUTH | FixPoint.EAST,
        }
        self._window.resize(w, h, fix_map.get(fix, FixPoint.NORTH | FixPoint.WEST))


def main():
    _server, _thread = start_server(SERVER_PORT)

    webview.settings['DRAG_REGION_DIRECT_TARGET_ONLY'] = True

    api = WindowApi()
    api.set_min_size((860, 600))
    window = webview.create_window(
        f"ItzReal · {config.CHAT_TITLE}",
        f"http://127.0.0.1:{SERVER_PORT}/",
        width=config.WINDOW_WIDTH,
        height=config.WINDOW_HEIGHT,
        min_size=(860, 600),
        resizable=True,
        frameless=True,
        easy_drag=True,
        js_api=api,
    )
    api.set_window(window)
    webview.start()


if __name__ == "__main__":
    main()
