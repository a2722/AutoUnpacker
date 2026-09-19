# -*- coding: utf-8 -*-
"""解压核心子包（阶段6c 自 extract.py 纯搬移，函数/方法体未做任何拆分）。

子模块布局：
  formats.py  格式/分卷/伪装探测与文件名分析（analyze_file / detect_archive_format /
              分卷名与兄弟卷判定 / 未完成下载判定 / 7z 清单解析与隐写探测）；
  engines.py  7-Zip / Python zipfile 双引擎与子进程原语（run_silent / concise_error /
              PauseController / 错误短句归类）；只依赖标准库，是导入图的汇点；
  service.py  ExtractService 多层嵌套解压服务（_extract_inner 原样未拆）；
  post.py     后处理与回收站委托（提升/删除源/时间戳校准/中间产物 staging /
              _recycle_paths 薄委托 deletion.engine）。

旧路径 autounpacker.extract 由同名 shim 模块保持兼容（全量重导出 +
属性赋值转发到归属子模块，见 extract.py）。
"""
