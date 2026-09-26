# -*- coding: utf-8 -*-
"""精简窗历史栈控制器：浏览器语义的 push / back / forward / reset（纯逻辑，不碰 UI）。

与浏览器一致的约定：
- `push(key)`：先截断当前游标之后的记录，再追加新页；
- `back()` / `forward()`：游标移动，越界返回 False；
- `reset(key)`：历史重置为单页（CODE 页结束回 HOME 用的就是它）；
- `goto(key)`：若页已存在则把游标移到**最近的**一次出现（不截断历史），
  否则等价 push——供 `CompactWindow.show_home()`「切到 HOME 但不重置历史」用。

本模块只依赖标准库，测试可直接构造断言。
"""

DEFAULT_PAGE = "HOME"


class NavHistory:
    """历史栈：`_history` 列表 + `_index` 游标（与浏览器同构）。"""

    def __init__(self, default=DEFAULT_PAGE):
        self._history = [str(default or DEFAULT_PAGE)]
        self._index = 0

    # ---- 只读查询 ----
    def current(self):
        """当前页代号（永远是历史上的某一页）。"""
        return self._history[self._index]

    def index(self):
        """当前游标位置（从 0 起）。"""
        return self._index

    def entries(self):
        """历史记录的浅拷贝（测试 / 调试用；调用方不得就地修改）。"""
        return list(self._history)

    def can_back(self):
        return self._index > 0

    def can_forward(self):
        return self._index < len(self._history) - 1

    # ---- 变更 ----
    def push(self, key):
        """进入新页：截断游标之后的记录，再追加并停在末尾（标准浏览器语义）。"""
        key = str(key or "")
        del self._history[self._index + 1:]
        self._history.append(key)
        self._index = len(self._history) - 1
        return key

    def back(self):
        """后退一格；没有历史可退时返回 False（游标不动）。"""
        if not self.can_back():
            return False
        self._index -= 1
        return True

    def forward(self):
        """前进一格；没有前进记录时返回 False（游标不动）。"""
        if not self.can_forward():
            return False
        self._index += 1
        return True

    def reset(self, key=DEFAULT_PAGE):
        """重置为单页历史（游标归零）。"""
        key = str(key or DEFAULT_PAGE)
        self._history = [key]
        self._index = 0
        return key

    def goto(self, key):
        """切到指定页：页已在历史中 -> 游标移到其最近一次出现（不截断）；

        历史中没有该页 -> 等价 push。返回是否命中已有记录。
        """
        key = str(key or "")
        for pos in range(len(self._history) - 1, -1, -1):
            if self._history[pos] == key:
                self._index = pos
                return True
        self.push(key)
        return False
