import assert from "node:assert/strict";
import test from "node:test";

import plugin, { COMPACTION_PROVIDER_ID } from "../src/index.js";
import {
  buildDeterministicSummary,
  redactSecrets,
  resolveOptions,
  safeFallbackSummary,
} from "../src/deterministic-compaction.js";

test("redacts common credentials without printing their values", () => {
  const fakeKey = `sk-${"A".repeat(32)}`;
  const privateKey = `${"-----BEGIN"} PRIVATE KEY-----\nexample-secret\n${"-----END"} PRIVATE KEY-----`;
  const credentialUrl = ["https://", "user", ":", "private-password", "@", "example.invalid/path"].join("");
  const result = redactSecrets(
    `api_key=${fakeKey}\nhttps://example.invalid/?access_token=${"B".repeat(32)}\n`
      + `${credentialUrl}\nBasic ${"Q".repeat(32)}\n${privateKey}`,
  );
  assert.match(result, /\[REDACTED\]/);
  assert.ok(!result.includes(fakeKey));
  assert.ok(!result.includes("example-secret"));
  assert.ok(!result.includes("B".repeat(32)));
  assert.ok(!result.includes("private-password"));
  assert.ok(!result.includes("Q".repeat(32)));
  assert.ok(!/\d+\[REDACTED\]/.test(result));
});

test("redacts truncated OpenAI-style credentials at compaction boundaries", () => {
  const partialKey = `sk-${"P".repeat(12)}`;
  const result = redactSecrets(`Retained tail contains ${partialKey}`);
  assert.ok(!result.includes(partialKey));
  assert.match(result, /\[REDACTED\]/);
});

test("redacts JSON credential fields and authentication headers", () => {
  const jsonSecret = "json-value-that-must-not-survive";
  const cookieSecret = "session-cookie-that-must-not-survive";
  const headerSecret = "header-value-that-must-not-survive";
  const result = redactSecrets(
    `{"client_secret":"${jsonSecret}","password":"short-secret"}\n`
      + `Cookie: sessionid=${cookieSecret}; theme=dark\n`
      + `X-API-Key: ${headerSecret}`,
  );
  assert.ok(!result.includes(jsonSecret));
  assert.ok(!result.includes(cookieSecret));
  assert.ok(!result.includes(headerSecret));
  assert.ok(!result.includes("short-secret"));
  assert.match(result, /\[REDACTED\]/);
});

test("builds a bounded deterministic handoff", () => {
  const messages = [
    { role: "user", content: "Keep the stable session key and preserve model choices." },
    { role: "assistant", content: "Created a verified handoff at /tmp/example/handoff.json." },
    { role: "toolResult", content: "Error: stream disconnected before completion." },
  ];
  const first = buildDeterministicSummary({ messages }, { maxSummaryChars: 8000, maxItemsPerSection: 8, maxItemChars: 500 });
  const second = buildDeterministicSummary({ messages }, { maxSummaryChars: 8000, maxItemsPerSection: 8, maxItemChars: 500 });
  assert.equal(first, second);
  assert.match(first, /User goals and constraints/);
  assert.match(first, /Failures and operational evidence/);
  assert.match(first, /\/tmp\/example\/handoff\.json/);
  assert.ok(first.length <= 8000);
});

test("returns a nonempty summary for malformed or empty input", () => {
  const malformed = {};
  Object.defineProperty(malformed, "content", { get() { throw new Error("unreadable"); } });
  const result = buildDeterministicSummary({ messages: [null, 1, {}, malformed] });
  assert.match(result, /No readable conversation messages/);
});

test("uses bounded one-pass retention for a very large message list", () => {
  const messages = Array.from({ length: 25000 }, (_, index) => ({
    role: index % 2 ? "assistant" : "user",
    content: `message-${index} ${"x".repeat(80)}`,
  }));
  const result = buildDeterministicSummary({ messages }, {
    maxSummaryChars: 8000,
    maxItemsPerSection: 4,
    maxItemChars: 300,
  });
  assert.ok(result.length <= 8000);
  assert.match(result, /message-24999/);
  assert.ok(!result.includes("message-0 "));
});

test("bounds oversized prior summaries before carrying them forward", () => {
  const result = buildDeterministicSummary({
    messages: [{ role: "user", content: "Continue safely." }],
    previousSummary: `${"p".repeat(2_000_000)} api_key=${`sk-${"S".repeat(32)}`}`,
  }, { maxSummaryChars: 8000 });
  assert.ok(result.length <= 8000);
  assert.ok(!result.includes(`sk-${"S".repeat(32)}`));
  assert.match(result, /\[REDACTED\]/);
});

test("redacts secrets from custom focus and fallback summaries", () => {
  const fakeKey = `sk-${"Z".repeat(32)}`;
  const summary = buildDeterministicSummary({
    messages: [],
    customInstructions: `Focus on ${fakeKey}`,
  });
  const fallback = safeFallbackSummary({ previousSummary: `Preserve ${fakeKey}` });
  assert.ok(!summary.includes(fakeKey));
  assert.ok(!fallback.includes(fakeKey));
  assert.match(summary, /Operator focus/);
  assert.match(fallback, /\[REDACTED\]/);
});

test("respects cancellation instead of falling through to model compaction", () => {
  const controller = new AbortController();
  controller.abort();
  assert.throws(
    () => buildDeterministicSummary({ messages: [], signal: controller.signal }),
    (error) => error?.name === "AbortError",
  );
});

test("plugin registers the expected compaction provider", async () => {
  let registered;
  plugin.register({
    pluginConfig: { maxSummaryChars: 9000 },
    registerCompactionProvider(provider) {
      registered = provider;
    },
  });
  assert.equal(registered.id, COMPACTION_PROVIDER_ID);
  const summary = await registered.summarize({ messages: [{ role: "user", content: "Continue safely." }] });
  assert.match(summary, /Continue safely/);
  const fallback = await registered.summarize(null);
  assert.match(fallback, /Recovery note/);
});

test("option coercion stays within safe bounds", () => {
  assert.deepEqual(resolveOptions({ maxSummaryChars: 1, maxItemsPerSection: 999, maxItemChars: "no" }), {
    maxSummaryChars: 8000,
    maxItemsPerSection: 40,
    maxItemChars: 1600,
  });
});
