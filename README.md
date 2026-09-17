# AutoUnpacker

监听下载目录，自动智能解压。把压缩包丢进监听文件夹，剩下的交给它：自动识别格式、试密码、穿透多层嵌套、处理分卷、移入回收站，全程无需人工干预。

> **v1.1.2** · [更新日志](CHANGELOG.md) · MIT License

## 它能做什么

| 场景 | 说明 |
|---|---|
| 自动解压 | 监听指定文件夹（如网盘/下载器目录），新压缩包出现即自动解压 |
| 密码自动尝试 | 长期密码本加临时密码捕获，自动逐一尝试直到命中 |
| 多层穿透 | 压缩包内还有压缩包？自动递归解压到最内层成品 |
| 分卷支持 | `.7z.001/.002`、`.part1.rar`、`.z01/.002` 等分卷归拢到齐后再解压 |
| 伪装格式识别 | 后缀被改（如 .mp4 实为 7z）时按真实格式（magic bytes）解压 |
| 二维码/提取码捕获 | 监控剪贴板，识别二维码图片里的提取码并写入密码本 |
| 拖放临时解压 | 把文件拖进主界面即可解压，不要求它在监听目录里 |
| 下载/占用保护 | 下载未完成或文件被占用的包自动延迟等待，不误判失败 |
| 删除回溯 | 源文件移入回收站而非永久删除，可一键还原、全程可追溯 |
| 托盘与热键 | 系统托盘常驻，全局快捷键随时唤起主界面 |
| 检查更新 | 手动点击才联网查询 GitHub 版本，并支持一键自动更新 |
| 翻译 JSON 归位 | 小于 10MB 的单个翻译 json 自动移入同名大文件夹，保持目录整洁 |

## 实验性功能

与百度网盘客户端联动的分享下载、网盘任务库看板，以及按网盘任务清单处理子目录（「百度清单」监听模式）仍在实验阶段，不够稳定、仍在修。它们集中在设置页的「实验性」分区，由「开启实验性功能」总开关统一控制，**默认关闭**，可按需开启；相关配置键与快捷键也都在该分区内，这里不再展开。

## 工作流程

1. 轮询监听目录，发现新压缩包后先做格式探测与到齐判断
2. 依次尝试长期密码本、临时密码与文件名中提取出的密码
3. 解压并穿透嵌套，必要时归拢分卷、剥离伪装
4. 成功后按配置把源文件回收进回收站，并留下可还原的记录

## 快速开始

环境要求：

- Windows 10/11（仅支持 Windows）
- Python 3.10+（64 位）
- [7-Zip](https://www.7-zip.org/) 18.00 或更高。首次启动可引导安装隔离版到 `%APPDATA%\AutoUnpacker\7z`，不污染系统
- 二维码识别依赖 pyzbar，需要 `libzbar-64.dll` / `libiconv.dll` 放在项目根目录（可从 pyzbar / zbar 发布包获取，程序启动时会将其加入 DLL 搜索路径）。缺失时二维码功能自动禁用，其余功能不受影响

```bash
pip install -r requirements.txt

# 启动（图形界面）
python main.py
# 或
python -m autounpacker
```

启动参数：

- `--autostart`：后台静默启动（不显示窗口，配合开机自启/任务计划）
- `--force`：跳过单实例检测强制新开（旧实例无响应时清理用）

首次启动在界面里添加监听路径即可；隐藏到托盘后程序仍继续工作。

## 配置

`config.json` 首次启动自动生成，可参照 `config.example.json`。常用配置键：

| 键 | 说明 |
|---|---|
| `watch_paths` | 监听路径列表，每条含 `path`、`output_dir`（解压到指定目录）与 `delete_source`（解压后是否删源） |
| `passwords` / 密码本 | 长期密码存于 `toolbox.db`，临时密码（本次开机内有效）存于 `temp_passwords.json` |
| `hotkey` / `hotkey_enabled` | 主界面全局快捷键（默认 `Ctrl+Alt+W`）及其开关 |
| `url_trust` | 网址信任：`open`（链接自动开浏览器）与 `fetch`（复制网址下载识别二维码）两套独立名单 |
| `close_action` | 点右上角关闭的行为：`ask` 询问 / `tray` 隐藏到托盘 / `exit` 退出 |
| `poll_interval` | 目录轮询间隔（秒） |
| `qr_clipboard_action` | 识别二维码后对剪贴板的处理：`none` / `code`（抬升提取码）/ `url`（写回内容） |
| `notify_*` | 各类托盘通知的独立开关（总开关为 `notify_enabled`） |

以上各项均可在设置页编辑，无需手动改文件。

## 项目结构

```
AutoUnpacker/
├── main.py                    # 兼容启动入口（转发到包内 app.main）
├── autounpacker/              # 核心包
│   ├── app.py                 # 入口：单实例检测、Qt 插件注入、后台线程与 GUI 组装
│   ├── paths.py               # 路径常量（数据文件、日志、单实例事件名）
│   ├── utils.py               # 通用工具：开机时间、文件占用检测、崩溃日志
│   ├── config.py              # 配置默认值/净化/迁移、原子保存、快捷键解析
│   ├── state.py               # AppState：线程安全配置读写、长期与临时密码本
│   ├── hub.py                 # 消息中枢：日志落盘与上限、通知过滤、stdout 捕获
│   ├── db.py                  # toolbox.db：密码本、密码字典、粘性记忆
│   ├── monitors.py            # 后台监控：目录轮询解压 + 剪贴板/二维码
│   ├── extract.py             # 解压核心：格式探测、密码候选、多层穿透、分卷
│   ├── sevenzip.py            # 7-Zip 检测、隔离版安装与调用封装
│   ├── trail.py               # 删除回溯：回收站删除与一键还原
│   ├── password_book.py       # 密码本对话框（长期密码编辑）
│   ├── qr_decode.py           # 二维码解码（多引擎，供子进程调用）
│   ├── trust.py               # 网址信任门禁（白/黑名单、内置私网拦截）
│   ├── updater.py             # 版本检查与自动更新（GitHub Releases）
│   ├── baidu_task.py          # 百度网盘任务库门面（实验性）
│   ├── baidu_db.py            # 只读访问百度客户端本地任务库
│   ├── baidu_manifest.py      # 批次/分卷还原与任务跟踪（纯逻辑）
│   ├── baidu_share.py         # 分享链接「拉起客户端下载」全链路
│   ├── baidu_watch.py         # 任务库轮询线程、诊断与启动探测
│   ├── ui/                    # PyQt5 界面（主窗口、对话框、控件、主题）
│   └── workers/               # 子进程（二维码解码、剪贴板写入，隔离原生库崩溃）
├── config.example.json        # 配置模板
└── requirements.txt
```

## 数据与隐私

- 数据文件都在项目根目录：`config.json`、`toolbox.db`（含 `-wal/-shm`）、`temp_passwords.json`、`deletion_trail.json`、`logs/`（按天）、`crash.log`、`cache/`（可重建的运行时缓存）、`backup/`（自动更新的旧代码备份）
- 除 `config.example.json` 外，以上文件全部列入 `.gitignore`，不会进入版本库
- 敏感内容只存在本机：密码本与临时密码、删除回溯记录、日志都可能含明文密码或真实路径，请勿随意外发
- 7-Zip 隔离版装在 `%APPDATA%\AutoUnpacker\7z`，不污染项目目录

## 技术要点

- **Qt 平台插件兼容**：启动时自动定位并注入 `QT_QPA_PLATFORM_PLUGIN_PATH`，venv 部署不会闪退
- **子进程隔离**：cv2/pyzbar/PIL 等原生库在独立子进程解码，段错误不拖垮主程序
- **单实例**：命名事件检测重复启动，`--force` 清理僵尸实例
- **配置向后兼容**：新增配置键自动用默认值补齐，旧配置升级不崩
- **原子写入**：配置/临时密码先写临时文件再 `os.replace`，崩溃不留半截 JSON
- **日志有上限**：单日日志封顶 200MB、单行截断，并自动清理 14 天前的旧日志
- **密码安全**：密码经 7-Zip stdin 传输（7-Zip ≥ 18.00），不进命令行参数

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
