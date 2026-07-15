# OpenClaw Session Keeper

在稳定 OpenClaw 项目入口背后安全轮换物理会话。

它会在会话空闲时生成带校验的交接包，再通过 Gateway 官方接口重置物理 `sessionId`，同时保留稳定 `sessionKey`、项目标签、用户手动模型选择、思考等级与非 Fast 设置。

## Token 与费用对比

仓库新增了可配置的阶梯计费估算器。在文档化的 40 轮参考场景中，Keeper 在计入换代后冷启动成本的情况下，将处理 Token 降低 **36.4%**，将 GPT-5.6 Sol 的估算 Codex credits 降低 **36.0%**。这是一组可复算的场景数据，不是对所有工作负载的固定承诺。

```bash
python3 cost_estimator.py
python3 cost_estimator.py --json
```

完整假设、公式、适用边界与官方来源见 [Token 与阶梯计费影响](docs/COST_MODEL.zh-CN.md)。

当前版本已在 OpenClaw 稳定版 `2026.7.1` 上完成兼容性验证。OpenClaw 内部 Gateway API 后续可能变化，升级 OpenClaw 后应先执行 dry-run 与完整预检。

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
