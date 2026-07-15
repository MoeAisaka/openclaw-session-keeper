# OpenClaw Session Keeper

在稳定 OpenClaw 项目入口背后安全轮换物理会话。

它会在会话空闲时生成带校验的交接包，再通过 Gateway 官方接口重置物理 `sessionId`，同时保留稳定 `sessionKey`、项目标签、用户手动模型选择、思考等级与非 Fast 设置。

V0.2 新增不依赖模型认证的确定性压缩 Provider：避免把 Codex OAuth 会话交给仅接受 API Key 的摘要器，并将超大会话恢复纳入 OpenClaw Gateway 生命周期锁。

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
- 确定性摘要采用有界单遍处理，不会为整段超大转录再复制一份标准化消息数组。
- 应急恢复先完成私有备份和哈希校验，再调用官方 `sessions.compact --max-lines`；不会直接改写活动转录或 `sessions.json`。

## OAuth 安全的确定性压缩

从干净仓库安装插件：

```bash
openclaw plugins install .
```

随后将 OpenClaw 压缩链路配置为全程不调用模型：

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "provider": "openclaw-session-keeper-deterministic",
        "reserveTokensFloor": 100000,
        "keepRecentTokens": 30000,
        "maxActiveTranscriptBytes": "16mb",
        "truncateAfterCompaction": true,
        "memoryFlush": { "enabled": false },
        "qualityGuard": { "enabled": false }
      }
    }
  }
}
```

即使主摘要 Provider 是确定性的，`memoryFlush` 与 safeguard 质量审计仍可能额外调用模型。除非已明确为它们配置兼容的非 OAuth 模型，否则必须关闭。当前若由 nmem 等插件承担长期记忆，关闭 OpenClaw 的模型式 memory flush 还能避免重复写入。

完整部署、验证、故障恢复与回滚方法见 [OAuth 安全的确定性压缩](docs/OAUTH_SAFE_COMPACTION.zh-CN.md)。

## 快速开始

```bash
mkdir -p "$HOME/.config/openclaw-session-keeper"
cp config.example.json "$HOME/.config/openclaw-session-keeper/config.json"
chmod 600 "$HOME/.config/openclaw-session-keeper/config.json"
python3 session_rollover.py scan --dry-run
```

先用自己的稳定会话替换示例项，确认 dry-run 正常后再执行真实扫描。

应急恢复默认只预览：

```bash
python3 emergency_recovery.py --config "$HOME/.config/openclaw-session-keeper/config.json" \
  --session-key agent:main:project-example --retain-records 50

python3 emergency_recovery.py --config "$HOME/.config/openclaw-session-keeper/config.json" \
  --session-key agent:main:project-example --retain-records 50 --execute
```

提交或推送前必须执行：

```bash
./scripts/preflight.sh
```

该命令会运行测试、自定义脱敏扫描和 gitleaks；缺少 gitleaks 时直接阻断。
