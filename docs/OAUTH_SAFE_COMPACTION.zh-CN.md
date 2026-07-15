# OAuth 安全压缩兼容性

## 故障模型

OpenClaw 可以用 OAuth 认证托管 Codex Runtime，但本地转录回退链路可能解析到只接受 API Key Profile 的 `openai-responses` 摘要器。在 OpenClaw `2026.7.1` 中，原生 Codex app-server 会话自行接管手动压缩，并忽略自定义压缩 Provider。当自动轮前压缩发生时，原生后端可以拒绝非手动请求，随后本地回退链路会在扩展 Provider 执行前先解析认证，最终把 OAuth Profile 交给只接受 API Key 的路径并失败。

这是宿主生命周期的顺序与兼容问题。全局指定 `openclaw-session-keeper-deterministic` 无法修复原生托管 Codex 会话，还会产生“忽略压缩覆盖”告警。

## 托管 Codex 安全配置

存在原生托管 Codex OAuth 会话时，不要全局设置 `agents.defaults.compaction.provider`。关闭辅助模型调用，并为物理会话换代留出足够空间：

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

每个被 Keeper 管理的托管 Codex 会话应满足：

```text
contextTokens - max(reserveTokens, reserveTokensFloor) - rolloverTokens >= 50000
```

这是调度余量，不是新增的模型容量。更大的新 Prompt 或系统 Prompt 都会消耗它，因此修改模型上下文窗口或系统 Prompt 后必须重新检查。

部署前执行只读检查：

```bash
python3 compatibility_check.py \
  --openclaw-config "$HOME/.openclaw/openclaw.json" \
  --keeper-config "$HOME/.config/openclaw-session-keeper/config.json" \
  --json
```

检查器只输出数量和阈值，绝不返回 Auth Profile 值。

## 确定性 Provider 的适用边界

插件仍会注册 `openclaw-session-keeper-deterministic`。它会以有界单遍方式提取近期目标、结果、失败和不透明引用，不调用模型，不读取 Provider 凭证。只能在确实支持 `registerCompactionProvider` 的内嵌或非原生 Runtime 中使用，并且启用前必须用一次性会话验证当前 OpenClaw 版本。

基于规则的脱敏只能尽力而为，生成的任何摘要仍应按敏感会话数据保护。

## 生产部署

1. 把活动 OpenClaw 配置、Keeper 配置与会话索引备份到仅当前用户可读的目录。
2. 对生产文件执行 `compatibility_check.py` 并审查全部发现。
3. 应用托管 Codex 配置与安全换代阈值。
4. 执行 `openclaw config validate` 与 Keeper `scan --dry-run`。
5. 仅重启一次 Gateway，复核健康、插件加载与进程稳定性。
6. 确认没有新增 API Key 压缩错误或 Provider 被忽略告警。
7. 在一次性 OAuth 会话中执行普通轮次；若验证手动压缩，必须保持 Codex 原生路径。
8. 执行仓库全量测试与密钥门禁。

## 应急恢复

对已经超大的空闲会话，`emergency_recovery.py` 会校验空闲状态，备份转录与会话条目，验证 SHA-256，再调用 Gateway 管理的 `sessions.compact --max-lines` 生命周期 API。它不会直接改写活动转录或 `sessions.json`。

恢复 CLI 面向 POSIX 系统，使用建议锁 `flock`。输入必须是当前用户拥有、且组用户和其他用户不可写的普通文件。陈旧的 `running` 字段只会被报告为 `storedStatusStale`，Gateway 运行态仍是唯一权威来源。

## 回滚

恢复上一版 OpenClaw 与 Keeper 配置；若插件版本发生变化，一并恢复上一版插件；仅重启一次 Gateway 并复核健康。恢复备份包含私密转录，禁止附加到 Issue 或提交到 Git。
