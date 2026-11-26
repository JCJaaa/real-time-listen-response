# Teams Live Copilot (Antigravity Skill & Standalone Tool)

监听 Windows 系统扬声器/耳机输出音频（Teams 语音），通过 **Gemini Live API (`gemini-3.8-live`)** 实时流式识别并在浏览器悬浮窗或终端中自动输出文本答案。

---

## 快速安装（零环境配置，双击即用）

### 1. 下载项目
从 GitHub 克隆或点击 **Code → Download ZIP** 解压到本地。

### 2. 直接双击 **`start.bat`**
不需要手动配环境，双击 **`start.bat`** 会全自动完成以下所有步骤：
1. 自动检测 Python 3（若未安装则尝试通过 `winget` 自动安装并刷新环境）；
2. 自动创建 `.venv` 虚拟环境并安装所有依赖；
3. 自动把本工具注册为全局 Antigravity Skill；
4. 弹出 AI 选择菜单（`[1] Gemini` / `[2] GPT` / `[3] Claude`），提示输入对应 API Key 并保存到本地 `.env`；
5. 自动加载 `gbsh0010_context.md` 并打开实时问答网页窗口。

> **说明**：`Gemini` 使用原生 Live Audio WebSocket（最低延迟）；`GPT` 使用内置静音检测（VAD）+ OpenAI Whisper 转录 + GPT 流式回答；`Claude` 因官方接口暂不支持直接音频输入，会自动借用 `.env` 里的 `GEMINI_API_KEY` / `OPENAI_API_KEY` 完成转录后再由 Claude 流式回答。

---

## macOS 组员快速安装（macOS 13+ 免装虚拟声卡）

macOS 版使用原生 **ScreenCaptureKit**（`mac/AudioTap.swift`）直接捕获 **Microsoft Teams** 音频，无需安装 BlackHole 等第三方驱动：

```bash
git clone <你的仓库地址>
cd teams-live-copilot
bash install_mac.sh
bash start_mac.sh
```

> 首次运行会弹出 **“录屏与系统录音（Screen & System Audio Recording）”** 权限请求，在系统设置中允许终端 / Python 即可。如果需要捕获全部系统音频而不只限 Teams，可运行 `bash start_mac.sh --all-audio`。

---

## 安装为 Antigravity Skill（可选）

如果你想让 Antigravity Agent 也识别并使用这个 Skill，在安装时加上 `-RegisterGlobalSkill`：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -RegisterGlobalSkill
```

或者直接把本仓库克隆到 Antigravity 全局 Skills 目录：

```powershell
git clone <你的仓库地址> "$env:USERPROFILE\.gemini\antigravity\skills\teams-live-copilot"
```

---

## 常用启动参数

* **查看所有音频输出设备编号**：
  ```powershell
  .\.venv\Scripts\python.exe teams_live_copilot.py --list-devices
  ```
* **指定 Teams 使用的耳机/扬声器设备**：
  ```powershell
  .\start.ps1 -Device 28
  ```
* **使用自定义问答背景知识库**：
  ```powershell
  .\start.ps1 -Context "my_notes.md"
  ```
* **纯终端流式输出模式**：
  ```powershell
  .\start.ps1 -UI terminal
  ```
