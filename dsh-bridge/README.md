# dsh-pet-bridge

DSH（DeepSeek Harness）桥插件：在 DSH 进程里开一个**仅本机**的 HTTP 服务
（127.0.0.1:47110），把桌宠说的话投递进 DSH 会话，并把 DSH 的回复以 SSE
流式推回桌宠气泡。

## 安装

把本目录整体复制到 DSH 的插件目录：

```
Windows: %USERPROFILE%\.dsh\plugins\pet-bridge\
```

重启 DSH 后生效。零 npm 依赖（纯 Node 内置模块）。

## 接口

| 路径 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查（连不上 DSH 时桥仍应答，并给出原因） |
| `/sessions` | GET | 列出 DSH 会话 |
| `/session/new` | POST | 新建会话 |
| `/prompt` | POST | 投递一句话进当前/指定会话 |
| `/stream` | GET (SSE) | 跟随会话事件流（快照/增量/工具调用） |
| `/cancel` | POST | 取消 DSH 正在执行的回合（真打断） |

状态同时写入 `~/.dsh/pet-bridge.json`，方便排查。

## 测试

```bash
node test_server.mjs   # 起一个假 DSH（端口 47198），配合桌宠侧 tests/ 使用
node test_bridge.mjs   # 桥单元测试（18 项）
```

## 许可

MIT。仅监听 127.0.0.1，不对外网开放任何端口。
