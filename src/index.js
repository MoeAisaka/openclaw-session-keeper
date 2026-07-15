import { buildDeterministicSummary, resolveOptions, safeFallbackSummary } from "./deterministic-compaction.js";

export const COMPACTION_PROVIDER_ID = "openclaw-session-keeper-deterministic";

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
  },
};
