# AutoUnpacker

监听下载目录，自动智能解压。把压缩包丢进监听文件夹，剩下的交给它：自动识别格式、试密码、穿透多层嵌套、处理分卷、移入回收站——全程无需人工干预。

> **v1.0.0** — 首个正式版 · [更新日志](CHANGELOG.md) · MIT License

## 它能做什么

| 场景 | 说明 |
|---|---|
| 自动解压 | 监听指定文件夹（如百度网盘/IDM 下载目录），新压缩包出现即自动解压 |
| 密码自动尝试 | 内置密码本（SQLite）+ 临时密码捕获，自动尝试正确密码 |
| 多层穿透 | 压缩包内还有压缩包？自动递归解压到最内层成品 |
| 分卷支持 | `.7z.001/.002`、`.part1.rar`、`.z01/.002` 等分卷自动归拢到齐后再解压 |
| 伪装格式识别 | 改名后缀迷惑（如 .mp4 实为 7z）按真实格式（magic bytes）解压 |
| 二维码/剪贴板密码捕获 | 监控剪贴板，识别二维码图片里的提取码，自动加入密码本 |
| 删除回溯 | 解压完成后源文件移入回收站（可一键还原），全程可追溯 |
| 断点保护 | 下载未完成/文件被占用的压缩包自动延迟等待，不误判失败 |

## 快速开始

```bash
# 环境要求：Windows 10/11，Python 3.10+，系统安装 7-Zip（或首次启动引导安装隔离版）
pip install -r requirements.txt

# 启动（图形界面）
python main.py
# 或
python -m autounpacker
```

首次启动在界面里添加监听路径即可。启动参数：

- `--autostart`：后台静默启动（无窗口，配合开机自启/任务计划）
- `--force`：强制新开实例（旧实例无响应/卡死时清理用）

## 配置

`config.json`（首次启动自动生成，参照 `config.example.json`）：

- `watch_paths`：监听路径列表，每条可单独设置 `output_dir`（解压到指定目录）和 `delete_source`（解压后是否删除源文件）
- `passwords` / 密码本：长期密码存于 `toolbox.db`，临时密码（本次开机内）存于 `temp_passwords.json`
- `hotkey`：全局快捷键唤起主界面（默认 `Alt+1`）
- `url_trust`：网址信任机制——二维码/剪贴板 URL 的自动访问与打开策略（黑名单/白名单/内置私网拦截）
- `qr_clipboard_action`：识别二维码后是否把最近复制的提取码写回剪贴板
- `close_action`：点右上角关闭的行为（每次询问 / 隐藏到托盘 / 直接退出）

## 项目结构

```
AutoUnpacker/
├── main.py                    # 兼容启动入口
├── autounpacker/              # 核心包
│   ├── app.py                 # 入口：单实例检测、Qt 插件注入、GUI 组装
│   ├── monitors.py            # 监听线程：目录轮询 + 剪贴板/二维码监控
│   ├── extract.py             # 智能解压核心：嵌套/密码/分卷/伪装/隐写
│   ├── sevenzip.py            # 7-Zip 管理：版本检测、隔离版安装
│   ├── trust.py               # 网址信任门卫（防 SSRF/私网访问）
│   ├── trail.py               # 删除回溯（回收站还原）
│   ├── db.py                  # SQLite：密码本 + 密码字典
│   ├── config.py              # 配置加载/净化/保存
│   ├── ui/                    # PyQt5 界面
│   └── workers/               # 子进程（QR 解码/剪贴板写入，隔离原生库崩溃）
├── config.example.json        # 配置模板
└── requirements.txt
```

## 数据与隐私

- 数据文件都在项目根目录：`config.json`、`toolbox.db`（密码）、`temp_passwords.json`、`deletion_trail.json`、`logs/`、`crash.log`
- 7-Zip 隔离版装在 `%APPDATA%\AutoUnpacker\7z`，不污染项目目录

## 技术要点

- **Qt 平台插件兼容**：启动时自动定位并注入 `QT_QPA_PLATFORM_PLUGIN_PATH`，venv 部署不会闪退
- **子进程隔离**：cv2/pyzbar/PIL 等原生库在独立子进程解码，段错误不拖垮主程序
- **单实例**：命名事件检测重复启动，`--force` 清理僵尸实例
- **配置向后兼容**：新增配置键自动用默认值补齐，旧配置升级不崩
- **原子写入**：配置/临时密码先写临时文件再 `os.replace`，崩溃不留半截 JSON

## 第三方依赖与致谢

本项目代码为原创（MIT 许可）。运行时依赖以下开源项目，各自版权归其作者所有，
使用方式均为通过 pip / 独立安装获取，未修改、未捆绑其代码：

| 依赖 | 用途 | 许可 |
|---|---|---|
| [PyQt5](https://riverbankcomputing.com/software/pyqt/) | 图形界面 | GPL v3 / Riverbank 商业双许可 |
| [pywin32](https://github.com/mhammond/pywin32) | Windows API（剪贴板/注册表/快捷键） | PSF |
| [Pillow](https://python-pillow.org/) | 图像处理 | HPND（MIT 兼容） |
| [pyzbar](https://github.com/NaturalHistoryMuseum/pyzbar) | 二维码解码 | MIT（底层 [zbar](https://github.com/mchehab/zbar) 为 LGPL-2.1） |
| [numpy](https://numpy.org/) | 数值计算 | BSD 3-Clause |
| [opencv-python](https://github.com/opencv/opencv-python) | 图像增强 | Apache 2.0（部分组件 LGPL） |
| [7-Zip](https://www.7-zip.org/) | 压缩/解压引擎 | LGPL（外部调用，隔离安装到 `%APPDATA%\AutoUnpacker\7z`，不链接不修改） |

> 关于 PyQt5 许可：本项目通过 `requirements.txt` 声明依赖、由用户自行安装
> （不捆绑分发 PyQt5 二进制），按 Riverbank 官方说明属于与 GPL 代码分开分发，
> 项目本身可保持 MIT 许可。若未来改为打包分发（PyInstaller 等），需按 GPL v3
> 要求重新评估（开源你的应用或购买商业许可）。

## License

MIT