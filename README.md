# Android应用清除助手

无需安装的 Windows 单文件 GUI 工具，通过 ADB 管理安卓设备中由用户安装的应用。

## 功能

- 扫描用户安装的应用，显示应用名称和包名。
- 先读取手机 APK 标签，再使用内嵌的 Google 官方 AAPT2 补充解析。
- 对仍无法识别的名称，可由用户主动点击“联网补全名称”，从 Google Play 公共详情页获取名称。
- 两个功能页均可按应用名称或包名实时模糊搜索。
- 勾选一个或多个应用后统一确认并卸载。
- 勾选需要保留的应用，一键卸载所有未保留应用。
- 内嵌 ADB 与 AAPT2，无需另行安装或配置环境变量。

## 名称补全与隐私

- 联网补全不会自动执行，仅查询当前仍显示为包名的项目。
- 首次使用时会明确询问是否同意发送包名及 Windows 语言、地区。
- 不发送设备序列号、应用数据或个人信息。
- 查询结果会缓存在 EXE 同目录的配置文件中。
- 本机 APK 标签优先级始终高于联网缓存，联网名称不会覆盖已正确识别的本机名称。

## 使用

1. 在手机中开启“开发者选项”和“USB 调试”。
2. 用 USB 连接手机，首次连接时在手机上允许调试。
3. 运行 `Android应用清除助手.exe`，刷新设备并扫描手机应用。
4. 在“勾选应用进行卸载”中勾选应用并卸载，或在“勾选保留，清理其余”中勾选需要保留的应用。

卸载会删除应用及其数据，请先做好备份。

## 从源码运行

需要 Windows 10/11 和 Python 3.11 或更高版本：

```powershell
python -m pip install -r requirements.txt
python app.py
```

开发环境可以使用系统中的 `adb.exe`。打包版本会内嵌 ADB。

## 构建单文件

```powershell
.\build.ps1
```

脚本会安装构建依赖，并在缺少本地 ADB 时从 Google 官方下载 Android Platform Tools。
生成文件位于 `dist\Android应用清除助手.exe`。

可选环境变量：

- `PYTHON`：指定 Python 可执行文件。
- `ADB_DIR`：指定包含 `adb.exe`、`AdbWinApi.dll` 和 `AdbWinUsbApi.dll` 的目录。
- `OUTPUT_DIR`：指定构建输出目录。

## 第三方组件

- Android Debug Bridge 来自 Google Android Platform Tools。
- AAPT2 来自 Google Android Build Tools，相关通知见 `assets/aapt2/NOTICE`。
