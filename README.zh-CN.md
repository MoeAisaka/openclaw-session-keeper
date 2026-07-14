# OpenClaw Session Keeper

在稳定 OpenClaw 项目入口背后安全轮换物理会话。

它会在会话空闲时生成带校验的交接包，再通过 Gateway 官方接口重置物理 `sessionId`，同时保留稳定 `sessionKey`、项目标签、用户手动模型选择、思考等级与非 Fast 设置。

## 安全保证

- 运行中的会话绝不重置。
- 转录备份、哈希、nmem、Gateway 回读或模型继承任一失败即停止换代。
- 状态文件原子写入并限制为当前用户可读。
- 不需要任何模型 API Key，也不应把 Key 写入配置。
- 真实配置、转录、日志、数据库、备份和密钥文件均被 Git 忽略与秘密扫描器拦截。

## 快速开始

```bash
mkdir -p "$HOME/.config/openclaw-session-keeper"
cp config.example.json "$HOME/.config/openclaw-session-keeper/config.json"
chmod 600 "$HOME/.config/openclaw-session-keeper/config.json"
python3 session_rollover.py scan --dry-run
```

先用自己的稳定会话替换示例项，确认 dry-run 正常后再执行真实扫描。

提交或推送前必须执行：

```bash
./scripts/preflight.sh
```

该命令会运行测试、自定义脱敏扫描和 gitleaks；缺少 gitleaks 时直接阻断。
