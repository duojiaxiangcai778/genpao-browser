# 跟跑浏览器 / 跟跑助手

一个面向 Windows 桌面的跟跑视频浏览器工具，支持 B 站等网页视频的全局热键控制、窗口透明度调整、鼠标穿透和悬浮快捷栏。

当前版本：**V1.0.3**  
更新日期：**2026-06-09**

## 功能

- 全局热键控制播放/暂停、快进/快退、倍速、透明度。
- 基于 WebView2 打开网页视频。
- 顶部快捷栏和左侧边栏。
- 鼠标穿透模式，方便叠在其他窗口/游戏上使用。
- 配置保存到 `hotkeys.json`。
- 关闭时自动保存窗口位置、大小和最大化状态，下次打开自动恢复。
- 运行时自动创建 `data/` 保存 WebView2 用户数据。

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
跟跑浏览器正式版（pc 金金村专用 群号：950610825）.zip
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
