# 桌宠「大肥鱼」—— Live2D 桌面宠物 + DSH 桥插件

一只住在你桌面上的 Live2D 胖鲸鱼女仆。可以当**独立 AI 桌宠**直接聊天，
也可以接入 **DSH（DeepSeek Harness）**，变成 DSH 智能体的「外显伴侣」——
DSH 在干活，她在桌面上汇报。

## 功能一览

- **Live2D 渲染**（QOpenGLWidget + live2d-py），带呼吸/眨眼/点头/摇头动作和 4 种表情；
  显卡不支持时自动回退到序列帧动画，功能不受影响
- **双 AI 模式**：
  - 独立 AI：直连 DeepSeek API（也支持 OpenAI 兼容中转 / 本地 Ollama）
  - **DSH 会话模式**：和 DSH 共用同一个会话，DSH 的记忆、工具、历史全部继承；
    DSH 调工具时气泡实时提示，回复流式显示，思考过程自动过滤；
    **DSH 没开时桌宠会自动帮你把它拉起来**（后台启动并等桥就绪，无需手动操作）
- **任务打断与接续**：她在干活时右键「停！打断当前任务」是真取消（通知 DSH 中止回合），
  之后说一句「继续」就能接着做
- **长期记忆**：SQLite + FTS5，每句话入库；聊久了自动压缩成摘要，
  提问时把相关旧片段召回来——不再是聊完就忘
- **音乐播放**：把歌丢进 `music/` 就能放（mp3/wav/flac/m4a/wma 等），
  自动监听目录增删、支持随机/单曲循环
- 右键菜单：表情/动作切换、人设切换、回复长度、缩放、记忆管理、AI 设置……

## 快速开始（Windows）

### 方式一：下载即用（推荐，不需要装 Python）

1. 到本仓库的 **[Releases](../../releases)** 页面下载最新 `桌宠大肥鱼-windows-x64.zip`（约 70MB）
2. 解压到任意目录（**请解压整个文件夹**，exe 依赖旁边的 `_internal`，单独拷 exe 无法运行）
3. 双击 `桌宠大肥鱼.exe`
4. 首次启动会弹出「AI 设置」窗口 → 填你自己的 DeepSeek API Key →「测试连接」→ 保存

> **API Key 只保存在解压目录的 `config.json` 里，不会上传到任何地方。**
> 压缩包内的 `config.json` 是空模板（`ai_api_key` 为空），不含任何真实密钥。

### 方式二：从源码运行（开发者）

需要 Python 3.10+ 和 PyQt6。`启动桌宠.bat` 走的就是这条路线——
**没装 Python / 没装 PyQt6 的电脑点它必然失败**，普通用户请用方式一。

### 独立 AI 模式

```bash
# Python 3.10+，安装依赖
pip install -r requirements.txt

# 配置：复制模板后填入你自己的 API Key（或首次启动在弹出的设置窗口里填）
copy config.example.json config.json

# 启动
python setup_and_run.py
```

> **API Key 只保存在本机 `config.json`，绝不上传、绝不联网到第三方。**
> 模板 `config.example.json` 里的 `ai_api_key` 是空的——请填你自己的
> [DeepSeek Key](https://platform.deepseek.com/api_keys)。
> 本仓库不含任何真实密钥。

第一次启动会自动弹「AI 设置」窗口，填 Key →「测试连接」→ 保存即可。
不需要打字界面，双击桌宠直接聊。

### DSH 会话模式（把她接到你的 DSH 上）

前提：已安装 DSH 桌面版（DeepSeek Harness）。

```bash
# 1) 把桥插件安装到 DSH 的插件目录（Windows）
mkdir "%USERPROFILE%\.dsh\plugins\pet-bridge"
xcopy /E /I dsh-bridge "%USERPROFILE%\.dsh\plugins\pet-bridge"

# 2) 重启 DSH，插件会在 127.0.0.1:47110 开一个本地桥
#    （只监听本机回环，不对外网开放）

# 3) 启动桌宠，右键 →「聊天模式」→「DSH 会话」
```

接入后：她说的话写进 DSH 的**她自己的那条会话**，DSH 的回复流式推回气泡；
**DSH 桌面版没开时桌宠会自动启动它**（也可在 `config.json` 的
`dsh_exe_path` 指定 DSH 的 exe 位置；留空则按常见安装位置自动查找）；
任务进度都在 DSH 会话历史里，「停」一下，下次说「继续」即可接上。

#### 两条会话是独立的（常见误区）

- 桌宠说话用 `config.json` 里的 `dsh_session_id` 那条会话，**不是你此刻在
  DSH 界面里打开的那条**，只是同样会出现在 DSH 的会话列表里
- 两边上下文互不串台；想让桌宠接着界面那条聊，右键 →
  「聊天模式」→「DSH 里换个会话…」选过去即可

#### 思考泡跟哪条会话走

- **跟她说话**：DSH 一进入思考阶段，头上立刻冒「💭 思考中…」
- **在 DSH 界面里聊**：她盯的是桥的「最近活跃会话」（`lastSessionId`），
  你新建或切到别的会话，2~3 秒内自动跟过去，同样冒「💭 想事情中…」
- 同时开多条会话时，只跟最近有动静的那条；正文一到思考泡就收掉

## 项目结构

```
├── main.py            # 启动入口（托盘 + 单实例锁 + 主线程看门狗）
├── setup_and_run.py   # 一键环境检查 + 启动（也是 PyInstaller 打包入口）
├── pet_window.py      # 透明窗口、气泡、聊天线程、右键菜单、打断
├── live2d_view.py     # Live2D 渲染层（GL），失败自动回退序列帧
├── pet_widget.py      # 序列帧 / 立绘 / 代码绘制三档渲染
├── llm_client.py      # LLM 通信层（DeepSeek / OpenAI 兼容 / Ollama）
├── dsh_client.py      # DSH 桥客户端（端口发现 / prompt / SSE 流解析 / 取消）
├── memory.py          # 长期记忆：FTS5 混合打分召回 + 摘要压缩
├── ai_settings.py     # 「AI 设置」对话框
├── trace.py           # 运行日志 + 卡死看门狗（>3s 无心跳自动抓全线程栈）
├── dsh-bridge/        # DSH 桥插件（Node 零依赖，127.0.0.1:47110）
├── tests/             # 离线测试集（假 DSH 服务器，不需要真 Key）
├── assets/            # 序列帧回退资源（8 动作 × 4 帧）
├── live2d/            # Live2D 模型（主用 model_st1280，回退 model）
├── personas/          # 人设（大肥鱼.md / 小蓝.md）
└── music/             # 放音乐的地方（往里拖歌即可）
```

## 运行测试

```bash
python tests/test_dsh_client.py   # 桥客户端协议（26 项）
python tests/test_dsh_mode.py    # DSH 模式集成（16 项）
python tests/test_interrupt.py    # 任务打断链路（11 项）
python tests/test_memory.py      # 记忆库召回/摘要
python tests/test_watchdog.py    # 卡死看门狗
# 或全跑一遍 tests/ 下的 test_*.py
```

全部测试使用内置的**假 DSH 服务器**（`dsh-bridge/test_server.mjs`）和假 LLM，
不需要真实 API Key。

## 打包成 exe

```bash
pip install pyinstaller
打包Release.bat   # PyInstaller 出 dist\桌宠大肥鱼\，并自动生成 Release 压缩包
```

> 注意：`--specpath` 会改变 `--add-data` 相对路径的解析基准，所以 spec 必须放在
> 仓库根目录（`--specpath .`），否则会报 `Unable to find ...assets`。

## 发布新版本（维护者）

改完代码后两件事：

**1. 更新代码**

双击 `更新到GitHub.bat`（输入一句更新说明即可），或者手动：

```bash
git add -A
git commit -m "这次改了什么"
git push origin main
```

**2. 更新 Release 压缩包**（只在改了程序逻辑、界面或立绘时需要）

1. 双击 `打包Release.bat`：打包 → 桌面生成 `桌宠大肥鱼-windows-x64.zip`
2. GitHub → Releases → 编辑最新那个 Release → 删掉旧 zip → 上传新 zip
3. **资产名保持 `桌宠大肥鱼-windows-x64.zip` 不变**，README 里的下载链接才不会失效

脚本会自动做三件事，不用手动检查：

- 用 `config.example.json` 覆盖 `config.json`，保证 `ai_api_key` 为空
- 剔除所有 `*.mp3`（版权音乐）、`memory.db`（个人记忆）、`*.log`（运行日志）
- 桌面上同名旧包改名为 `桌宠大肥鱼-windows-x64-<日期>.zip` 归档，不覆盖、不删除


## 常见问题

- **放歌没声音**：程序会自动设 `QT_MEDIA_BACKEND=windows`（PyQt6 自带的 ffmpeg
  后端缺依赖，Qt 不会自动回退），日志 `_trace.log` 里 `music backend:` 一行可确认
- **任务做着做着想停**：右键 →「停！打断当前任务」，是真取消不是装死
- **程序未响应/闪退**：`_trace.log` 会留下完整轨迹；若卡死，看门狗会自动写入
  全线程调用栈，把日志尾部发出来即可定位
- **隐私**：所有数据（Key、记忆、配置）只存在程序目录本机文件中，无任何遥测

## 开源许可

- 代码：**MIT**（见 LICENSE）
- Live2D Cubism Core：Live2D Inc. 专有许可（免费再分发，条款见 LICENSE 附录）
- Live2D 模型与美术资产：仅限本项目使用，不得单独提取商用

## 截图

<!-- 建议在这里放一张桌宠 + 聊天气泡的 GIF，市场的卡片/搜索会抓 README 首图 -->
