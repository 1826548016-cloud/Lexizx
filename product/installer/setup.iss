; Lexizx 安装向导（Inno Setup 6）
; 向导流程：欢迎 → 许可协议（license.txt） → 选择安装路径 → 安装 → 完成
;
; 构建前置：
;   1. 已执行 product/word.spec 生成 product/dist/word.exe
;   2. 本机安装 Inno Setup 6（https://jrsoftware.org/isdl.php）
; 构建命令：ISCC.exe product\installer\setup.iss
;
; 维护约定：
;   - MyAppVersion 发布前与 vocab_project/settings.py 的 APP_VERSION 同步
;   - license.txt 与 words/templates/partials/privacy_modal.html 正文保持同步
;     （docs/legal/user-agreement-*.md 归档即对应纯文本）

#define MyAppName "Lexizx"
#define MyAppFullName "Lexizx 考研英语学习工具"
#define MyAppVersion "3.5.0"
#define MyAppPublisher "个人开发者"
#define MyAppURL "https://github.com/1826548016-cloud/Lexizx"

[Setup]
AppId={{7F3A2C5E-9B1D-4E6F-A8C2-5D3E7F9A1B4C}
AppName={#MyAppFullName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppFullName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases/latest
; 每用户安装：默认装到 %LOCALAPPDATA%\Programs\Lexizx，无需管理员权限。
; 软件的数据目录与自动更新都要求 exe 所在目录可写，故不装 Program Files。
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Lexizx
DefaultGroupName={#MyAppFullName}
DisableProgramGroupPage=yes
LicenseFile=license.txt
OutputDir=..\dist\installer
OutputBaseFilename=Lexizx-Setup-{#MyAppVersion}
SetupIconFile=..\..\ioc\图标.ico
UninstallDisplayIcon={app}\word.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; 覆盖安装前自动关闭运行中的实例
CloseApplications=yes
RestartApplications=no

[Languages]
; 简体中文语言文件随项目自带（源自 Inno 官方 issrc 仓库，6.5.0+ 版，
; 部分 Inno 发行包不内置中文，放项目内保证任何机器都能构建）
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: unchecked

[Files]
Source: "..\dist\word.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppFullName}"; Filename: "{app}\word.exe"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppFullName}"; Filename: "{app}\word.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\word.exe"; Description: "立即启动 {#MyAppFullName}"; Flags: nowait postinstall skipifsilent

; 卸载仅移除安装的程序文件与快捷方式；data/ 目录中的用户学习数据默认保留，不随卸载删除。
