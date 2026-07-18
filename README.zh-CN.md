# OpenClaw Session Keeper

在稳定 OpenClaw 项目入口背后安全轮换物理会话。

它会在会话空闲时生成带校验的交接包，再通过 Gateway 官方接口重置物理 `sessionId`，同时保留稳定 `sessionKey`、项目标签、用户手动模型选择、思考等级，以及显式启用后的当前 Standard/Fast 选择。

V0.3 支持把普通阈值换代延后到下一条用户消息：巡检器只生成并校验交接包，等待型 `before_dispatch` 钩子会在该消息交给 Agent 之前完成物理换代，再让原任务在新会话继续执行。这样刚完成的最终答复会一直留在当前会话，方便用户阅读；应急阈值仍立即换代。V0.2 新增了面向兼容内嵌 Runtime 的确定性压缩 Provider，并将超大会话恢复纳入 OpenClaw Gateway 生命周期锁。原生托管 Codex 会话由 OpenClaw `2026.7.1` 自行接管压缩，必须按下述兼容策略配置。

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
- 延迟换代钩子采用等待式、失败关闭语义；换代校验失败时，本条任务不会进入 Agent，也不会产生重复执行。
- 钩子不记录、不持久化用户本次输入内容。

## 托管 Codex OAuth 兼容策略

从干净仓库安装插件：

```bash
openclaw plugins install .
```

OpenClaw `2026.7.1` 会忽略原生托管 Codex 会话的自定义压缩 Provider。这类会话不应全局指定本插件 Provider；手动压缩交给 Codex 原生链路，同时关闭辅助模型调用，并让物理会话换代充分早于自动压缩预算：

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "reserveTokensFloor": 50000,
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

每个被 Keeper 管理的托管 Codex 会话至少保留 50,000 Token 安全余量：

```text
contextTokens - max(reserveTokens, reserveTokensFloor) - rolloverTokens >= 50000
```

这段余量用于确保每分钟扫描的换代器能在新 Prompt 进入不兼容的本地回退链路之前先完成物理换代。`memoryFlush` 与 safeguard 质量审计仍可能额外调用模型，除非已明确路由到兼容模型，否则应关闭。当前若由 nmem 等插件承担长期记忆，关闭 OpenClaw 的模型式 memory flush 还能避免重复写入。

修改配置前先执行只读兼容检查：

```bash
python3 compatibility_check.py \
  --openclaw-config "$HOME/.openclaw/openclaw.json" \
  --keeper-config "$HOME/.config/openclaw-session-keeper/config.json" \
  --json
```

确定性 Provider 仍可用于确实支持 `registerCompactionProvider` 的非原生或内嵌 Runtime，但启用前必须用一次性会话验证。

完整部署、验证、故障恢复与回滚方法见 [OAuth 安全的确定性压缩](docs/OAUTH_SAFE_COMPACTION.zh-CN.md)。

## 快速开始

```bash
mkdir -p "$HOME/.config/openclaw-session-keeper"
cp config.example.json "$HOME/.config/openclaw-session-keeper/config.json"
chmod 600 "$HOME/.config/openclaw-session-keeper/config.json"
python3 session_rollover.py scan --dry-run
```

先用自己的稳定会话替换示例项，确认 dry-run 正常后再执行真实扫描。

`allowManualFastMode` 需要按会话显式开启。配置中的 `fastMode` 仍是默认值；开启后，Keeper 会在巡检和物理换代时保留用户当前选择的布尔值，而不会再次把显式 Fast 选择强制改回默认 Standard。无人值守任务和必须固定 Standard 的角色 Agent 不应开启此选项。

`rolloverTiming.deferUntilNextUserMessage` 也需要显式开启。开启后，普通阈值只会进入 `pending_next_user` 状态；下一条用户消息到达时，插件的等待型 `before_dispatch` 钩子先激活换代，再让原消息在新物理会话中继续。应急阈值与已退役 Codex 绑定恢复仍立即执行。插件的 `deferredRollover` 配置必须指向本仓库管理器脚本、生产配置与状态文件。

常用命令：

```bash
python3 session_rollover.py scan --dry-run
python3 session_rollover.py scan
python3 session_rollover.py activate-pending --session-key agent:main:project-example --dry-run
python3 session_rollover.py activate-pending --session-key agent:main:project-example
python3 session_rollover.py status
```

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
