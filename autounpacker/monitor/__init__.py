# -*- coding: utf-8 -*-
"""监控实现包（Stage 6b 拆分自 autounpacker.monitors）。

- monitor.watcher   → FolderWatcher（目录轮询智能解压）
- monitor.clipboard → QRMonitor（剪贴板/二维码 + 网址信任门卫）

本 __init__ 刻意不做任何导入与再导出：包根聚合会引入额外依赖边与导入副作用；
新代码请直接导入具体子模块。旧路径 autounpacker.monitors 由包根兼容 shim 保持可用。
"""
