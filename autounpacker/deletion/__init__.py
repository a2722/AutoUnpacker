# -*- coding: utf-8 -*-
"""删除/隔离单一职责包：回收站删除、隔离区、回溯记录存储与删除引擎。

分层（自底向上，禁止反向依赖）：
  records     记录存储与唯一互斥锁（包内最底层，不导入 deletion.* 其它模块）
  recycle     回收站移入/还原          -> records
  quarantine  隔离区移入/还原/清理      -> records
  engine      删除引擎（回收站失败时的回退编排）-> recycle + quarantine

对外约定：in_quarantine() 是整个包唯一的隔离区判定；trail.py 仅为旧路径兼容 shim。
"""
