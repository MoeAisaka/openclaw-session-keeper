const DEFAULTS = Object.freeze({
  maxSummaryChars: 40000,
  maxItemsPerSection: 18,
  maxItemChars: 1600,
});

const SECRET_PATTERNS = [
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----/g,
  /\b((?:authorization|proxy-authorization|x-api-key|api-key)\s*:\s*)[^\r\n]+/gi,
  /\b((?:cookie|set-cookie)\s*:\s*)[^\r\n]+/gi,
  /(https?:\/\/)[^/\s:@]+:[^@\s/]+@/gi,
  /\bsk-[A-Za-z0-9_-]{20,}\b/g,
  /\bsk-ant-[A-Za-z0-9_-]{20,}\b/g,
  /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/g,
  /\b(?:gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\bAIza[0-9A-Za-z_-]{20,}\b/g,
  /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g,
  /\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}/gi,
  /\b(Basic\s+)[A-Za-z0-9+/=]{12,}/gi,
  /([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)=)[^&#\s]+/gi,
  /((?:["']?)(?:api[_-]?key|access[_-]?token|auth[_-]?token|session[_-]?token|client[_-]?secret|secret|password)(?:["']?)\s*[:=]\s*(?:["']?))[^"'\s,;}{]{4,}/gi,
];

const IDENTIFIER_PATTERNS = [
  /https?:\/\/[^\s<>()\[\]"']+/g,
  /(?:~\/|\/)[A-Za-z0-9._@%+,:=\-/]+/g,
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/gi,
  /\bagent:[A-Za-z0-9._-]+:[A-Za-z0-9._:-]+\b/g,
  /\b[A-Za-z0-9._-]+\/[A-Za-z0-9._:-]+\b/g,
];

function abortError() {
  const error = new Error("Compaction aborted");
  error.name = "AbortError";
  return error;
}

function safeString(value) {
  try {
    return String(value ?? "");
  } catch {
    return "";
  }
}

function bounded(value, limit) {
  const raw = safeString(value);
  if (raw.length <= limit) return raw.trim();
  const head = Math.max(1, Math.floor(limit * 0.72));
  const tail = Math.max(1, limit - head - 24);
  return `${raw.slice(0, head).trim()}\n…[truncated]…\n${raw.slice(-tail).trim()}`;
}

function boundedRedacted(value, inputLimit, outputLimit = inputLimit) {
  // Sample before redaction so a malformed or unexpectedly huge previous
  // summary cannot force another full-transcript-sized allocation.
  return bounded(redactSecrets(bounded(value, inputLimit)), outputLimit);
}

export function redactSecrets(value) {
  let text = safeString(value);
  for (const pattern of SECRET_PATTERNS) {
    pattern.lastIndex = 0;
    text = text.replace(pattern, (match, prefix) => prefix ? `${prefix}[REDACTED]` : "[REDACTED]");
  }
  return text;
}

function objectText(value, limit) {
  try {
    if (!value || typeof value !== "object") return "";
    if (typeof value.text === "string") return bounded(value.text, limit);
    if (typeof value.content === "string") return bounded(value.content, limit);
  } catch {
    return "";
  }
  return "";
}

function contentToText(content, limit) {
  if (typeof content === "string") return bounded(content, limit);
  if (Array.isArray(content)) {
    // Read from the tail and enforce a strict character budget. This prevents a
    // large tool-result array from being copied in full during compaction.
    const values = [];
    let remaining = limit;
    for (let index = content.length - 1; index >= 0 && remaining > 0; index -= 1) {
      const item = content[index];
      const text = typeof item === "string" ? bounded(item, remaining) : objectText(item, remaining);
      if (!text) continue;
      values.push(text);
      remaining -= text.length + 1;
    }
    return values.reverse().join("\n");
  }
  return objectText(content, limit);
}

function normalizedMessage(message, inputLimit) {
  try {
    if (!message || typeof message !== "object") return null;
    const nested = message.message && typeof message.message === "object" ? message.message : message;
    const role = safeString(nested.role ?? message.role ?? "unknown").toLowerCase();
    const text = redactSecrets(contentToText(nested.content ?? message.content, inputLimit))
      .replace(/\r\n/g, "\n")
      .replace(/[ \t]+/g, " ")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    return text ? { role, text } : null;
  } catch {
    return null;
  }
}

function pushRecentUnique(items, value, limit) {
  const key = safeString(value).replace(/\s+/g, " ").trim();
  if (!key) return;
  const existing = items.findIndex((item) => item.key === key);
  if (existing >= 0) items.splice(existing, 1);
  items.push({ key, value });
  while (items.length > limit) items.shift();
}

function values(items) {
  return items.map((item) => item.value);
}

function section(title, items, itemLimit) {
  if (!items.length) return "";
  return [`## ${title}`, ...items.map((value) => `- ${bounded(value, itemLimit)}`)].join("\n");
}

function collectIdentifiers(text, identifiers, limit = 40) {
  for (const pattern of IDENTIFIER_PATTERNS) {
    pattern.lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
      const value = match[0].replace(/[.,;:!?]+$/, "");
      if (value && !value.includes("[REDACTED]")) pushRecentUnique(identifiers, value, limit);
    }
  }
}

function isFailure(message) {
  if (/tool(result)?/i.test(message.role)) {
    return /\b(error|failed|failure|exception|timeout|disconnect|denied|invalid)\b/i.test(message.text);
  }
  return /\b(error|failed before reply|compaction failed|stream disconnected)\b/i.test(message.text);
}

export function safeFallbackSummary(params = {}, config = {}) {
  const options = resolveOptions(config);
  let previous = "";
  try {
    previous = boundedRedacted(
      params.previousSummary ?? "",
      Math.min(240000, options.maxSummaryChars * 2),
      options.maxSummaryChars - 512,
    );
  } catch {
    previous = "";
  }
  const recovery = "## Recovery note\n- Deterministic parsing failed; continue from preserved recent turns and verify external state before repeating side effects.";
  return bounded(
    previous ? `# Deterministic session handoff\n\n${previous}\n\n${recovery}` : `# Deterministic session handoff\n\n${recovery}`,
    options.maxSummaryChars,
  );
}

export function buildDeterministicSummary(params = {}, config = {}) {
  if (params.signal?.aborted) throw abortError();
  const options = resolveOptions({ ...DEFAULTS, ...config });
  const inputLimit = Math.min(24000, Math.max(4000, options.maxItemChars * 4));
  const users = [];
  const assistants = [];
  const failures = [];
  const identifiers = [];
  let readableMessages = 0;
  let sourceMessages = [];
  try {
    sourceMessages = Array.isArray(params.messages) ? params.messages : [];
  } catch {
    sourceMessages = [];
  }

  // One bounded pass: memory use is independent of transcript message count.
  for (let index = 0; index < sourceMessages.length; index += 1) {
    if ((index & 255) === 0 && params.signal?.aborted) throw abortError();
    const message = normalizedMessage(sourceMessages[index], inputLimit);
    if (!message) continue;
    readableMessages += 1;
    if (message.role === "user") pushRecentUnique(users, message.text, options.maxItemsPerSection);
    if (message.role === "assistant") pushRecentUnique(assistants, message.text, options.maxItemsPerSection);
    if (isFailure(message)) {
      pushRecentUnique(failures, `${message.role}: ${message.text}`, Math.min(12, options.maxItemsPerSection));
    }
    collectIdentifiers(message.text, identifiers);
  }

  let previous = "";
  let focus = "";
  try {
    const previousLimit = Math.min(10000, Math.floor(options.maxSummaryChars * 0.3));
    const focusLimit = Math.min(2000, options.maxItemChars * 2);
    previous = boundedRedacted(params.previousSummary ?? "", Math.min(240000, previousLimit * 2), previousLimit);
    focus = boundedRedacted(params.customInstructions ?? "", Math.min(16000, focusLimit * 2), focusLimit);
  } catch {
    // Continue from preserved turns even if optional metadata is malformed.
  }

  const parts = [
    "# Deterministic session handoff",
    "Generated locally without a model call. Treat copied identifiers as opaque and exact.",
  ];
  if (focus) parts.push("## Operator focus", focus);
  if (previous) parts.push("## Previous verified summary", previous);
  const userSection = section("User goals and constraints", values(users), options.maxItemChars);
  const assistantSection = section("Progress, decisions, and outcomes", values(assistants), options.maxItemChars);
  const failureSection = section("Failures and operational evidence", values(failures), Math.min(options.maxItemChars, 1200));
  const identifierSection = section("Opaque identifiers and references", values(identifiers), 500);
  for (const value of [userSection, assistantSection, failureSection, identifierSection]) {
    if (value) parts.push(value);
  }
  if (!readableMessages && !previous) {
    parts.push("## Recovery state", "- No readable conversation messages were supplied; preserve the unsummarized tail and continue cautiously.");
  }
  parts.push("## Continuation rule", "- Continue from the most recent preserved turns. Verify external state before repeating side effects.");
  if (params.signal?.aborted) throw abortError();
  return bounded(parts.join("\n\n"), options.maxSummaryChars);
}

export function resolveOptions(config = {}) {
  const integer = (value, fallback, min, max) => {
    const parsed = Number(value);
    return Number.isInteger(parsed) ? Math.min(max, Math.max(min, parsed)) : fallback;
  };
  return {
    maxSummaryChars: integer(config.maxSummaryChars, DEFAULTS.maxSummaryChars, 8000, 120000),
    maxItemsPerSection: integer(config.maxItemsPerSection, DEFAULTS.maxItemsPerSection, 4, 40),
    maxItemChars: integer(config.maxItemChars, DEFAULTS.maxItemChars, 300, 5000),
  };
}
