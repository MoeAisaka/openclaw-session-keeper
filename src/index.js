import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { buildDeterministicSummary, resolveOptions, safeFallbackSummary } from "./deterministic-compaction.js";

export const COMPACTION_PROVIDER_ID = "openclaw-session-keeper-deterministic";
const execFileAsync = promisify(execFile);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

function expandHome(value) {
  const text = String(value ?? "").trim();
  if (!text) return text;
  return text === "~" ? os.homedir() : text.startsWith("~/") ? path.join(os.homedir(), text.slice(2)) : text;
}

function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  return Number.isInteger(parsed) ? Math.min(maximum, Math.max(minimum, parsed)) : fallback;
}

export function resolveDeferredRolloverOptions(pluginConfig = {}) {
  const value = pluginConfig?.deferredRollover;
  const config = value && typeof value === "object" ? value : {};
  return {
    enabled: config.enabled === true,
    pythonPath: expandHome(config.pythonPath || "/usr/bin/python3"),
    managerScriptPath: expandHome(config.managerScriptPath || path.join(ROOT, "session_rollover.py")),
    managerConfigPath: expandHome(
      config.managerConfigPath || "~/.config/openclaw-session-keeper/config.json",
    ),
    statePath: expandHome(config.statePath || "~/.openclaw/session-rollover/current.json"),
    timeoutMs: boundedInteger(config.timeoutMs, 30000, 1000, 60000),
  };
}

function readJson(filePath, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

export function readPendingRollover(statePath, sessionKey) {
  if (!sessionKey) return null;
  const record = readJson(statePath, null)?.sessions?.[sessionKey];
  if (
    !record
    || !["pending_next_user", "prepared"].includes(record.status)
    || !record.oldSessionId
  ) return null;
  return record;
}

export function readPendingFirstDispatch(statePath, sessionKey) {
  if (!sessionKey) return null;
  const record = readJson(statePath, null)?.sessions?.[sessionKey];
  const firstDispatch = record?.firstDispatch;
  if (
    record?.status !== "active"
    || !record?.newSessionId
    || !firstDispatch
    || !["awaiting_agent_start", "started"].includes(firstDispatch.status)
  ) return null;
  return { record, firstDispatch };
}

async function runManagerCommand(options, commandArgs) {
  const { stdout } = await execFileAsync(
    options.pythonPath,
    [
      options.managerScriptPath,
      "--config",
      options.managerConfigPath,
      ...commandArgs,
    ],
    {
      encoding: "utf8",
      timeout: options.timeoutMs,
      maxBuffer: 1024 * 1024,
      env: {
        ...process.env,
        PATH: "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
      },
    },
  );
  const payload = JSON.parse(stdout);
  if (payload?.ok === false) throw new Error(String(payload.error || "activation_failed"));
  return payload;
}

export async function activatePendingRollover(options, sessionKey) {
  return runManagerCommand(options, [
    "activate-pending",
    "--session-key",
    sessionKey,
  ]);
}

export async function recordFirstDispatchStart(options, sessionKey, runId, sessionId) {
  const args = [
    "record-first-dispatch-start",
    "--session-key",
    sessionKey,
    "--run-id",
    runId,
  ];
  if (sessionId) args.push("--session-id", sessionId);
  return runManagerCommand(options, args);
}

export async function recordFirstDispatchEnd(options, sessionKey, runId, success) {
  const args = [
    "record-first-dispatch-end",
    "--session-key",
    sessionKey,
    "--run-id",
    runId,
    "--success",
    success ? "true" : "false",
  ];
  if (!success) args.push("--error-code", "agent_run_failed");
  return runManagerCommand(options, args);
}

async function recordLifecycleWithRetry(operation, attempts = 3) {
  let lastResult;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    lastResult = await operation();
    if (lastResult?.action !== "lock_busy") return lastResult;
    if (attempt < attempts) {
      await new Promise((resolve) => setTimeout(resolve, attempt * 100));
    }
  }
  throw new Error(`lifecycle_state_lock_busy_after_${attempts}_attempts`);
}

export function createDeferredRolloverHook(options, logger, activate = activatePendingRollover) {
  return async (event, ctx) => {
    const sessionKey = String(event?.sessionKey || ctx?.sessionKey || "").trim();
    if (!sessionKey || !readPendingRollover(options.statePath, sessionKey)) {
      return { handled: false };
    }
    try {
      const result = await activate(options, sessionKey);
      if (
        ![
          "pending_rollover_activated",
          "pending_rollover_reconciled",
          "no_pending_rollover",
        ].includes(result?.action)
      ) {
        throw new Error(`unexpected_activation_result:${result?.action || "missing"}`);
      }
      logger.info?.(
        `openclaw-session-keeper: deferred rollover activation ${result.action} session=${sessionKey}`,
      );
      return { handled: false };
    } catch (error) {
      logger.error?.(
        `openclaw-session-keeper: deferred rollover activation failed session=${sessionKey}: ${error?.message ?? error}`,
      );
      return {
        handled: true,
        text: "⚠️ 会话已达安全换代阈值，但换代未能完成。本条任务未执行，也不会重复执行；请稍后重试。",
      };
    }
  };
}

export function createFirstDispatchStartHook(
  options,
  logger,
  recordStart = recordFirstDispatchStart,
) {
  return async (_event, ctx) => {
    const sessionKey = String(ctx?.sessionKey || "").trim();
    const runId = String(ctx?.runId || "").trim();
    const pending = readPendingFirstDispatch(options.statePath, sessionKey);
    if (!pending || pending.firstDispatch.status !== "awaiting_agent_start" || !runId) return;
    try {
      const result = await recordLifecycleWithRetry(
        () => recordStart(options, sessionKey, runId, ctx?.sessionId),
      );
      if (!["first_dispatch_started", "first_dispatch_start_idempotent", "first_dispatch_already_finished"].includes(result?.action)) {
        throw new Error(`unexpected_first_dispatch_start_result:${result?.action || "missing"}`);
      }
      logger.info?.(
        `openclaw-session-keeper: first dispatch started session=${sessionKey} run=${runId}`,
      );
    } catch (error) {
      // Observability must never consume a user task after rollover committed.
      logger.error?.(
        `openclaw-session-keeper: first dispatch start tracking failed session=${sessionKey}: ${error?.message ?? error}`,
      );
    }
  };
}

export function createFirstDispatchEndHook(
  options,
  logger,
  recordEnd = recordFirstDispatchEnd,
) {
  return async (event, ctx) => {
    const sessionKey = String(ctx?.sessionKey || "").trim();
    const runId = String(event?.runId || ctx?.runId || "").trim();
    const pending = readPendingFirstDispatch(options.statePath, sessionKey);
    if (!pending || pending.firstDispatch.status !== "started" || !runId) return;
    if (String(pending.firstDispatch.runId || "") !== runId) return;
    try {
      const result = await recordLifecycleWithRetry(
        () => recordEnd(options, sessionKey, runId, event?.success === true),
      );
      if (!["first_dispatch_completed", "first_dispatch_failed", "first_dispatch_end_idempotent"].includes(result?.action)) {
        throw new Error(`unexpected_first_dispatch_end_result:${result?.action || "missing"}`);
      }
      logger.info?.(
        `openclaw-session-keeper: first dispatch finished session=${sessionKey} run=${runId} success=${event?.success === true}`,
      );
    } catch (error) {
      logger.error?.(
        `openclaw-session-keeper: first dispatch end tracking failed session=${sessionKey}: ${error?.message ?? error}`,
      );
    }
  };
}

export default {
  id: "openclaw-session-keeper",
  name: "OpenClaw Session Keeper",
  description: "Auth-independent deterministic compaction for long-running sessions",
  register(api) {
    const options = resolveOptions(api.pluginConfig ?? {});
    api.registerCompactionProvider({
      id: COMPACTION_PROVIDER_ID,
      label: "Session Keeper deterministic compaction",
      async summarize(params) {
        try {
          return buildDeterministicSummary(params, options);
        } catch (error) {
          let aborted = error?.name === "AbortError";
          try {
            aborted ||= params?.signal?.aborted === true;
          } catch {
            // A malformed signal object must not make the provider fall through
            // to OpenClaw's model-based compaction fallback.
          }
          if (aborted) throw error;
          return safeFallbackSummary(params, options);
        }
      },
    });
    const deferredOptions = resolveDeferredRolloverOptions(api.pluginConfig ?? {});
    if (deferredOptions.enabled) {
      api.on(
        "before_dispatch",
        createDeferredRolloverHook(deferredOptions, api.logger ?? console),
      );
      api.on(
        "before_agent_run",
        createFirstDispatchStartHook(deferredOptions, api.logger ?? console),
      );
      api.on(
        "agent_end",
        createFirstDispatchEndHook(deferredOptions, api.logger ?? console),
      );
      api.logger?.info?.("openclaw-session-keeper: deferred rollover hook active");
    }
  },
};
