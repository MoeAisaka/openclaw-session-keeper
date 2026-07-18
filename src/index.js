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

export async function activatePendingRollover(options, sessionKey) {
  const { stdout } = await execFileAsync(
    options.pythonPath,
    [
      options.managerScriptPath,
      "--config",
      options.managerConfigPath,
      "activate-pending",
      "--session-key",
      sessionKey,
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
      api.logger?.info?.("openclaw-session-keeper: deferred rollover hook active");
    }
  },
};
