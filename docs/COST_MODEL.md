# Token and tiered-pricing impact

Session Keeper can reduce both repeated context tokens and the chance that a
request enters a provider's long-context price tier. The exact impact depends
on how quickly a session grows, how much context the handoff carries forward,
cache behavior, model rates, and when rollover is allowed to run.

## Reference scenario

The bundled estimator uses a transparent synthetic workload:

- 40 turns
- 48,000 prompt tokens on the first turn
- 8,000 additional prompt tokens per turn
- 2,000 output tokens per turn
- rollover after a completed request reaches 235,000 total tokens
- 48,000 prompt tokens on the first request after rollover

The default pricing inputs mirror the public GPT-5.6 Sol Codex credit card and
the model's long-context note as published on 2026-07-14:

- 125 credits per 1M uncached input tokens
- 12.5 credits per 1M cached input tokens
- 750 credits per 1M output tokens
- prompts above 272,000 input tokens: 2x input and 1.5x output for the full request

| Metric | Without Keeper | With Keeper | Difference |
|---|---:|---:|---:|
| Total tokens processed | 8,240,000 | 5,240,000 | -36.4% |
| Long-context requests | 11 | 0 | -11 |
| Estimated credits | 264.65 | 169.50 | -36.0% |
| Physical rollovers | 0 | 1 | +1 |

The estimator charges the first request after each rollover as an uncached
cold start. This deliberately includes the main rollover penalty rather than
assuming perfect cache reuse.

## Reproduce or replace the assumptions

```bash
python3 cost_estimator.py
python3 cost_estimator.py --json
```

Every important assumption is configurable:

```bash
python3 cost_estimator.py \
  --turns 60 \
  --initial-prompt-tokens 80000 \
  --growth-per-turn 6000 \
  --output-per-turn 1500 \
  --rollover-total-tokens 235000 \
  --post-rollover-prompt-tokens 64000 \
  --long-context-threshold 272000 \
  --input-rate 125 \
  --cached-input-rate 12.5 \
  --output-rate 750 \
  --long-input-multiplier 2 \
  --long-output-multiplier 1.5 \
  --unit credits
```

For another provider, replace the threshold, rates, multipliers, and unit. A
provider without a long-context tier can use multipliers of `1`; rollover can
still save repeated context tokens, but the percentage will usually be lower.

## What this estimate does and does not prove

- It compares the same logical workload with and without physical-session rollover.
- It models prompt growth, cached and uncached input, output, cold starts, and full-request long-context multipliers.
- It does not claim that every workload saves 36%.
- It does not estimate answer-quality changes or the operational value of recoverability.
- It is not a billing statement. Validate results against provider usage exports before making financial commitments.

## Official sources

- [GPT-5.6 Sol model page](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
- [Codex pricing and credits](https://learn.chatgpt.com/docs/pricing#what-are-tokens-and-credits)
- [OpenAI guidance for extending usage limits](https://learn.chatgpt.com/docs/pricing#what-can-i-do-to-make-my-usage-limits-last-longer)

Sources were checked on 2026-07-14. Pricing changes over time; pass current
rates explicitly when reproducing the estimate later.
