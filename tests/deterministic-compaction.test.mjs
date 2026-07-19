import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import plugin, {
  COMPACTION_PROVIDER_ID,
  createDeferredRolloverHook,
  createFirstDispatchEndHook,
  createFirstDispatchStartHook,
  readPendingFirstDispatch,
  readPendingRollover,
  resolveDeferredRolloverOptions,
} from "../src/index.js";
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

test("deferred rollover options are disabled by default and bounded", () => {
  const options = resolveDeferredRolloverOptions({
    deferredRollover: { enabled: true, timeoutMs: 999999 },
  });
  assert.equal(options.enabled, true);
  assert.equal(options.timeoutMs, 60000);
  assert.match(options.managerScriptPath, /session_rollover\.py$/);
});

test("reads only a pending or partially committed generation", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "session-keeper-"));
  const statePath = path.join(root, "current.json");
  fs.writeFileSync(statePath, JSON.stringify({
    sessions: {
      "agent:main:project-example": {
        status: "pending_next_user",
        oldSessionId: "old-session",
      },
      "agent:main:test-active": {
        status: "active",
        oldSessionId: "old-session",
      },
      "agent:main:project-test": {
        status: "prepared",
        oldSessionId: "old-session",
      },
    },
  }));
  assert.equal(
    readPendingRollover(statePath, "agent:main:project-example").oldSessionId,
    "old-session",
  );
  assert.equal(readPendingRollover(statePath, "agent:main:test-active"), null);
  assert.equal(
    readPendingRollover(statePath, "agent:main:project-test").oldSessionId,
    "old-session",
  );
  fs.rmSync(root, { recursive: true, force: true });
});

test("before_dispatch activates pending rollover and lets the original task continue", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "session-keeper-"));
  const statePath = path.join(root, "current.json");
  fs.writeFileSync(statePath, JSON.stringify({
    sessions: {
      "agent:main:project-example": {
        status: "pending_next_user",
        oldSessionId: "old-session",
      },
    },
  }));
  const calls = [];
  const hook = createDeferredRolloverHook(
    { statePath },
    { info() {}, error() {} },
    async (_options, sessionKey) => {
      calls.push(sessionKey);
      return { action: "pending_rollover_activated" };
    },
  );
  const result = await hook(
    { sessionKey: "agent:main:project-example" },
    {},
  );
  assert.deepEqual(calls, ["agent:main:project-example"]);
  assert.deepEqual(result, { handled: false });
  fs.rmSync(root, { recursive: true, force: true });
});

test("before_dispatch fails closed without logging or echoing the user prompt", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "session-keeper-"));
  const statePath = path.join(root, "current.json");
  fs.writeFileSync(statePath, JSON.stringify({
    sessions: {
      "agent:main:project-example": {
        status: "pending_next_user",
        oldSessionId: "old-session",
      },
    },
  }));
  const errors = [];
  const hook = createDeferredRolloverHook(
    { statePath },
    { info() {}, error(message) { errors.push(message); } },
    async () => { throw new Error("gateway unavailable"); },
  );
  const result = await hook(
    { sessionKey: "agent:main:project-example", content: "private user task" },
    {},
  );
  assert.equal(result.handled, true);
  assert.match(result.text, /本条任务未执行/);
  assert.ok(!errors.join("\n").includes("private user task"));
  fs.rmSync(root, { recursive: true, force: true });
});

test("tracks the first new-generation agent run without reading prompt content", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "session-keeper-"));
  const statePath = path.join(root, "current.json");
  const writeState = (status, runId) => fs.writeFileSync(statePath, JSON.stringify({
    sessions: {
      "agent:main:project-example": {
        status: "active",
        newSessionId: "new-session",
        firstDispatch: {
          status,
          ...(runId ? { runId } : {}),
        },
      },
    },
  }));
  writeState("awaiting_agent_start");
  assert.equal(
    readPendingFirstDispatch(statePath, "agent:main:project-example").firstDispatch.status,
    "awaiting_agent_start",
  );

  const calls = [];
  const startHook = createFirstDispatchStartHook(
    { statePath },
    { info() {}, error() {} },
    async (_options, sessionKey, runId, sessionId) => {
      calls.push(["start", sessionKey, runId, sessionId]);
      writeState("started", runId);
      return { action: "first_dispatch_started" };
    },
  );
  await startHook(
    { prompt: "must-not-be-read" },
    {
      sessionKey: "agent:main:project-example",
      sessionId: "new-session",
      runId: "run-1",
    },
  );

  const endHook = createFirstDispatchEndHook(
    { statePath },
    { info() {}, error() {} },
    async (_options, sessionKey, runId, success) => {
      calls.push(["end", sessionKey, runId, success]);
      return { action: "first_dispatch_completed" };
    },
  );
  await endHook(
    { runId: "run-1", success: true, messages: ["must-not-be-read"] },
    { sessionKey: "agent:main:project-example", runId: "run-1" },
  );
  assert.deepEqual(calls, [
    ["start", "agent:main:project-example", "run-1", "new-session"],
    ["end", "agent:main:project-example", "run-1", true],
  ]);
  fs.rmSync(root, { recursive: true, force: true });
});

test("plugin registers rollover activation and first-dispatch lifecycle hooks", () => {
  const hooks = [];
  plugin.register({
    pluginConfig: {
      deferredRollover: { enabled: true },
    },
    registerCompactionProvider() {},
    on(name) { hooks.push(name); },
    logger: { info() {}, error() {} },
  });
  assert.deepEqual(hooks, ["before_dispatch", "before_agent_run", "agent_end"]);
});
