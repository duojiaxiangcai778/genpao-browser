# 跟跑浏览器 / 跟跑助手

一个面向 Windows 桌面的跟跑视频浏览器工具，支持 B 站等网页视频的全局热键控制、窗口透明度调整、鼠标穿透和悬浮快捷栏。

当前版本：**V1.0.6**  
更新日期：**2026-09-18**

## 下载

Release 页面：<https://github.com/duojiaxiangcai778/genpao-browser/releases/latest>

| 文件 | 说明 |
|---|---|
| `genpao-assistant-v1.0.6.exe` | 主程序，双击运行（本地原始文件名 `跟跑助手.exe`） |
| `genpao-browser-v1.0.6-release.zip` | 正式版完整包，解压即用，内附 `操作说明.txt` |

> GitHub 的 Release 附件名不支持中文字符，因此上传时使用英文文件名，内容与本地发布包完全一致。

## 功能

- 全局热键控制播放/暂停、快进/快退、倍速、透明度。
- 基于 WebView2 打开网页视频。
- 顶部快捷栏和左侧边栏。
- 鼠标穿透模式，方便叠在其他窗口/游戏上使用。
- 配置保存到 `hotkeys.json`。
- 关闭时自动保存窗口位置、大小和最大化状态，下次打开自动恢复。
- 运行时自动创建 `data/` 保存 WebView2 用户数据。

## V1.0.6 更新

- 修复首次启动白屏：窗口保存状态为空时窗口从未收到尺寸变化，WebView2 渲染表面停在创建时的旧尺寸，整窗只剩背景色（页面其实已加载）。现在无论有无保存状态都会应用一次窗口几何，尺寸未变则抖动 2px 强制刷新。
- 白屏问题可诊断：把 pywebview 自身日志接入 `debug.log`（此前只写 stderr，打包后白屏没有任何线索）。

## V1.0.5 更新

- 修复打包版黑屏：WebView2 用户数据目录前置到 `%LOCALAPPDATA%\GenPaoBrowser_Data`，不再落在 PyInstaller 解压临时目录。
- 移除强制 GPU 光栅化/硬件解码参数，交给 WebView2 按显卡自动降级，避免部分驱动下渲染进程挂起。
- 关闭 `private_mode`，崩溃后登录状态可持久化。

## V1.0.4 更新

- 修复首次启动崩溃/卡死：WebView2 初始化失败不再静默崩溃，弹出提示并引导安装。
- 防止多实例冲突：添加全局互斥锁，禁止同时运行多个实例。
- 解决配置损坏问题：改用原子写入（先写临时文件再重命名），写入崩溃不会丢失设置。
- 优化 seek 合并定时器线程安全，退出时正确清理定时器。
- COM 初始化防重复，兼容更多 Windows 环境。

## V1.0.3 更新

- 新增保存/恢复窗口位置、大小和最大化状态。
- 设置保存时保留已有窗口状态配置。

## V1.0.2 更新

- 使用干净 Python 打包环境重新构建，exe 体积从约 63MB 降到约 17MB。
- 修复页面跳转后控制脚本可能不重新注入的问题。
- 加强热键格式校验，不支持的热键会在设置保存时提示。
- 加强窗口宽高、透明度级别输入校验。
- 修复管理员重启时参数拼接不稳的问题。
- 更新正式版 `操作说明.txt`。

## 本地开发环境

固定 Python/打包环境位置：

- Python：`D:\D\Python311\python.exe`
- 虚拟环境：`D:\D\PythonBuildEnv\venvs\genpao-browser`

首次准备：

```powershell
D:\D\Python311\python.exe -m venv D:\D\PythonBuildEnv\venvs\genpao-browser
D:\D\PythonBuildEnv\venvs\genpao-browser\Scripts\python.exe -m pip install -r requirements.txt
```

## 打包 exe

```powershell
D:\D\PythonBuildEnv\venvs\genpao-browser\Scripts\python.exe -m PyInstaller --clean --noconfirm 跟跑助手.spec
```

输出文件：

```text
dist\跟跑助手.exe
```

正式发布时，将 `dist\跟跑助手.exe` 复制为根目录 `跟跑助手.exe`，再制作正式版 zip。

## 正式版 zip 内容

正式版压缩包：

```text
跟跑浏览器正式版vV1.0.6（pc 金金村专用 群号：950610825）.zip
```

压缩包内只放：

```text
跟跑助手.exe
hotkeys.json
操作说明.txt
```

不内置 `data/`，因为它可能包含 Cookie、登录态、浏览历史等隐私数据。用户首次运行后会自动生成。

## 不提交到 GitHub 的内容

- `data/`：WebView2 用户数据，可能包含缓存、Cookie、登录态和浏览记录。
- `*.exe`、`*.zip`：本地发布产物。
- `build/`、`dist/`：PyInstaller 构建中间目录。
- `debug.log`、`*.log`：本地运行日志。
