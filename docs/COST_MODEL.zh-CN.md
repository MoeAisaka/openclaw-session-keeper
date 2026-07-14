# Token 与阶梯计费影响

Session Keeper 不只是降低超长会话的故障风险。对于上下文越长、请求越贵的模型，它还能减少重复输入 Token，并降低请求进入长上下文加价区间的概率。

## 参考场景

内置估算器采用完全公开、可修改的合成负载：

- 连续 40 轮
- 首轮输入上下文 48,000 Token
- 每轮上下文增加 8,000 Token
- 每轮输出 2,000 Token
- 已完成请求达到 235,000 总 Token 后换代
- 换代后首轮输入上下文恢复为 48,000 Token

默认价格参数引用 2026-07-14 的 OpenAI 官方公开信息：GPT-5.6 Sol 的 Codex 计费为每 100 万未缓存输入 Token 125 credits、缓存输入 12.5 credits、输出 750 credits；当单次请求输入超过 272,000 Token 时，整次请求的输入按 2 倍、输出按 1.5 倍计算。

| 指标 | 不使用 Keeper | 使用 Keeper | 差异 |
|---|---:|---:|---:|
| 处理的总 Token | 8,240,000 | 5,240,000 | -36.4% |
| 进入长上下文加价的请求 | 11 | 0 | -11 |
| 估算 credits | 264.65 | 169.50 | -36.0% |
| 物理会话换代次数 | 0 | 1 | +1 |

估算器把每次换代后的首个请求视为未命中缓存的冷启动，因此已经计入换代的主要成本，没有假设完美缓存。

## 复算与替换假设

```bash
python3 cost_estimator.py
python3 cost_estimator.py --json
```

可以通过命令行替换轮数、上下文增长速度、换代后上下文、各类 Token 单价、长上下文阈值和倍数。对于没有阶梯加价的模型，将两个 multiplier 设为 `1` 即可。

## 结论边界

- 36% 是参考场景结果，不是所有用户的承诺值。
- 实际结果受系统提示词、工具 Schema、缓存命中率、每轮增长速度和空闲换代时机影响。
- 估算不包含回答质量、恢复能力和避免任务中断带来的业务价值。
- 这不是账单；作预算决策前应使用真实用量导出进行复算。

## 官方依据

- [GPT-5.6 Sol 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
- [Codex Token 与 credits 计费](https://learn.chatgpt.com/docs/pricing#what-are-tokens-and-credits)
- [OpenAI 关于延长用量额度的建议](https://learn.chatgpt.com/docs/pricing#what-can-i-do-to-make-my-usage-limits-last-longer)

以上来源核验于 2026-07-14。后续价格变化时，应通过命令行显式传入最新参数。
