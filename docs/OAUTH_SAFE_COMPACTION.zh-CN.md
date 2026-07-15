# OAuth 安全的确定性压缩

## 问题

OpenClaw 会话可能使用 OAuth 登录托管 Codex Runtime，而内置摘要器只接受 OpenAI API Key。若把 OAuth Profile 交给仅支持 API Key 的压缩路径，摘要尚未生成就会失败；超大转录仍映射到原会话，下一轮又会撞上上下文上限。

需要同时处理三处潜在模型调用：

1. 主压缩摘要 Provider；
2. 压缩前的 memory flush；
3. safeguard 摘要质量审计。

只替换第一处并不能彻底消除故障。

## 安全配置

将 Provider 设置为 `openclaw-session-keeper-deterministic`，同时关闭两条辅助模型调用链：

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

Provider 采用有界单遍方式提取近期用户目标、执行结果、故障和不透明引用；输出前会过滤常见凭证模式，但任何基于规则的脱敏都不可能绝对完备，因此生成的摘要仍应按敏感会话数据保护。它不会调用模型、读取 Provider 凭证或记录转录正文。

## 生产部署

1. 将活动 OpenClaw 配置与会话索引备份到仅当前用户可读的目录。
2. 从已审查的干净仓库执行 `openclaw plugins install .`。
3. 应用安全配置并执行 `openclaw config validate`。
4. 仅重启一次 Gateway，并确认 PID 跨过观察周期后保持稳定。
5. 核对插件已加载，Provider ID 可解析。
6. 创建一次性会话，保留真实 OAuth Profile，注入合成消息后执行 `openclaw sessions compact <key> --json`。
7. 验证压缩成功，且没有新增、改用 API Key。
8. 删除一次性会话，执行仓库全部密钥门禁。

本版本已在 OpenClaw `2026.7.1` 上使用一次性托管 Codex OAuth 会话完成真实验证：确定性 Provider 成功执行 `/compact`，同一个稳定会话 Key 随后继续调用原模型，且没有使用 API Key Profile 或 fallback。

## 应急恢复

已经超大的空闲会话可使用 `emergency_recovery.py`：

1. 通过 Gateway 确认没有活动 Run；
2. 校验转录链；
3. 把转录和会话条目复制到仅当前用户可读的恢复目录，并验证 SHA-256；
4. 调用 `openclaw sessions compact <key> --max-lines N --json`；
5. 重新读取 Gateway 管理的状态，确认行数和字节数均下降；
6. 写入只含元数据的清单。

工具不会直接改写活动转录或 `sessions.json`。直接写入会与 Gateway 竞争，可能破坏生命周期状态。

恢复 CLI 当前面向 POSIX 系统，因为它使用 `flock` 建立建议锁。配置文件、会话索引和转录必须是当前用户拥有的普通文件，且不得允许组用户或其他用户写入。若持久层残留陈旧的 `running` 字段，工具只报告 `storedStatusStale`，以 Gateway 运行态为准，绝不直接修补 OpenClaw 私有状态。

## 回滚

恢复原 OpenClaw 配置、禁用插件条目、重启一次 Gateway并复核健康状态。恢复备份包含私人转录，禁止附加到 Issue 或提交 Git。

若尚未安装并选中本 Provider，在 OpenClaw 明确修复认证路由兼容性前，不要对 OAuth 会话执行模型式 `/compact`。当本 Provider 已启用且两条辅助调用链均关闭时，`/compact` 会走本文所述的确定性路径。
