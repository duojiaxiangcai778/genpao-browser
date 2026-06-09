# 跟跑浏览器 / 跟跑助手

一个面向 Windows 桌面的跟跑视频浏览器工具，支持 B 站等网页视频的全局热键控制、窗口透明度调整、鼠标穿透和悬浮快捷栏。

## 功能

- 全局热键控制播放/暂停、快进/快退、倍速、透明度。
- 基于 WebView2 打开网页视频。
- 顶部快捷栏和左侧边栏。
- 鼠标穿透模式，方便叠在其他窗口上使用。
- 配置保存到 `hotkeys.json`。

## 本地开发

推荐使用固定的打包环境：

```powershell
D:\D\Python311\python.exe -m venv D:\D\PythonBuildEnv\venvs\genpao-browser
D:\D\PythonBuildEnv\venvs\genpao-browser\Scripts\python.exe -m pip install -r requirements.txt
```

## 打包

```powershell
D:\D\PythonBuildEnv\venvs\genpao-browser\Scripts\python.exe -m PyInstaller --clean --noconfirm 跟跑助手.spec
```

输出文件：

```text
dist\跟跑助手.exe
```

## 注意

- `data/` 是 WebView2 运行时数据，可能包含缓存、Cookie、登录态和浏览记录，不应提交到 GitHub。
- `*.exe`、`build/`、`dist/`、`debug.log` 均为本地产物，不提交到源码仓库。
