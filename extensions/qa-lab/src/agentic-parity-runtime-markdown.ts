import {
  formatRuntimeCacheCount,
  formatRuntimeCacheHitPercent,
} from "./agentic-parity-cache-usage.js";
import type { QaRuntimeParityReport } from "./agentic-parity-runtime-report-contract.js";
import type { RuntimeParityCacheDiagnostics } from "./runtime-parity-cache-diagnostics.js";
import { formatRuntimeSpeedComparison, formatRuntimeWallClockMs } from "./runtime-parity-timing.js";

function formatRuntimeCacheMisses(diagnostics: RuntimeParityCacheDiagnostics | undefined): string {
  if (!diagnostics) {
    return "N/A";
  }
  if (diagnostics.cacheTelemetryTurns === 0) {
    return diagnostics.unmeasuredPostWarmTurns.length > 0
      ? `N/A (unmeasured turns ${diagnostics.unmeasuredPostWarmTurns.join(", ")})`
      : "N/A";
  }
  const measuredMisses =
    diagnostics.cacheMisses.length === 0
      ? "none"
      : diagnostics.cacheMisses
          .map((miss) => `turn ${miss.turn} (${miss.inputTokens} uncached input)`)
          .join(", ");
  if (diagnostics.unmeasuredPostWarmTurns.length === 0) {
    return measuredMisses;
  }
  const unknownTurns = `unmeasured turns ${diagnostics.unmeasuredPostWarmTurns.join(", ")}`;
  return measuredMisses === "none" ? `N/A (${unknownTurns})` : `${measuredMisses}; ${unknownTurns}`;
}

export function renderQaRuntimeParityMarkdownReport(report: QaRuntimeParityReport): string {
  const lines = [
    `# Aether Runtime Parity Report — ${report.runtimePair[0]} vs ${report.runtimePair[1]}`,
    "",
    `- Compared at: ${report.comparedAt}`,
    `- Provider mode: ${report.providerMode ?? "unknown"}`,
    `- Primary model: ${report.primaryModel ?? "unknown"}`,
    `- Verdict: ${report.pass ? "pass" : "fail"}`,
    "",
    "## Aggregate Metrics",
    "",
    "| Metric | Value |",
    "| --- | ---: |",
    `| Total scenarios | ${report.totalScenarios} |`,
    `| Passed scenarios | ${report.passedScenarios} |`,
    `| Failed scenarios | ${report.failedScenarios} |`,
    `| No drift | ${report.driftCounts.none} |`,
    `| Text-only drift | ${report.driftCounts["text-only"]} |`,
    `| Tool-call-shape drift | ${report.driftCounts["tool-call-shape"]} |`,
    `| Tool-result-shape drift | ${report.driftCounts["tool-result-shape"]} |`,
    `| Structural drift | ${report.driftCounts.structural} |`,
    `| Failure-mode drift | ${report.driftCounts["failure-mode"]} |`,
    "",
    "## Prompt Cache",
    "",
    "| Runtime | Gross input | Uncached input | Cached input | Cache writes | Output | Total tokens | Cache hit |",
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    `| aether | ${formatRuntimeCacheCount(report.usage.aether?.grossInputTokens)} | ${formatRuntimeCacheCount(report.usage.aether?.uncachedInputTokens)} | ${formatRuntimeCacheCount(report.usage.aether?.cachedInputTokens)} | ${formatRuntimeCacheCount(report.usage.aether?.cacheWriteTokens)} | ${formatRuntimeCacheCount(report.usage.aether?.outputTokens)} | ${formatRuntimeCacheCount(report.usage.aether?.totalTokens)} | ${formatRuntimeCacheHitPercent(report.usage.aether?.cacheHitPercent)} |`,
    `| codex | ${formatRuntimeCacheCount(report.usage.codex?.grossInputTokens)} | ${formatRuntimeCacheCount(report.usage.codex?.uncachedInputTokens)} | ${formatRuntimeCacheCount(report.usage.codex?.cachedInputTokens)} | ${formatRuntimeCacheCount(report.usage.codex?.cacheWriteTokens)} | ${formatRuntimeCacheCount(report.usage.codex?.outputTokens)} | ${formatRuntimeCacheCount(report.usage.codex?.totalTokens)} | ${formatRuntimeCacheHitPercent(report.usage.codex?.cacheHitPercent)} |`,
    "",
    "## Runtime Timing",
    "",
    "| Runtime | Total wall time | p50 per scenario | p90 per scenario |",
    "| --- | ---: | ---: | ---: |",
    `| aether | ${formatRuntimeWallClockMs(report.timing.aether.totalWallClockMs)} | ${formatRuntimeWallClockMs(report.timing.aether.p50WallClockMs)} | ${formatRuntimeWallClockMs(report.timing.aether.p90WallClockMs)} |`,
    `| codex | ${formatRuntimeWallClockMs(report.timing.codex.totalWallClockMs)} | ${formatRuntimeWallClockMs(report.timing.codex.p50WallClockMs)} | ${formatRuntimeWallClockMs(report.timing.codex.p90WallClockMs)} |`,
    "",
    `- Faster runtime: ${formatRuntimeSpeedComparison(report.timing)}`,
    "",
  ];
  if (report.timing.bootstrap) {
    lines.push(
      "## Gateway Bootstrap (Excluded From Runtime Timing)",
      "",
      "| Runtime | Total bootstrap | p50 per scenario | p90 per scenario |",
      "| --- | ---: | ---: | ---: |",
      `| aether | ${formatRuntimeWallClockMs(report.timing.bootstrap.aether.totalWallClockMs)} | ${formatRuntimeWallClockMs(report.timing.bootstrap.aether.p50WallClockMs)} | ${formatRuntimeWallClockMs(report.timing.bootstrap.aether.p90WallClockMs)} |`,
      `| codex | ${formatRuntimeWallClockMs(report.timing.bootstrap.codex.totalWallClockMs)} | ${formatRuntimeWallClockMs(report.timing.bootstrap.codex.p50WallClockMs)} | ${formatRuntimeWallClockMs(report.timing.bootstrap.codex.p90WallClockMs)} |`,
      "",
    );
  }
  if (report.failures.length > 0) {
    lines.push("## Gate Failures", "");
    for (const failure of report.failures) {
      lines.push(`- ${failure}`);
    }
    lines.push("");
  }
  lines.push("## Scenario Comparison", "");
  for (const scenario of report.scenarios) {
    const usageNotApplicable = scenario.runtimeParityUsage.expectation === "not-applicable";
    const aetherTokens = usageNotApplicable ? "N/A" : String(scenario.aetherTokens);
    const codexTokens = usageNotApplicable ? "N/A" : String(scenario.codexTokens);
    lines.push(`### ${scenario.name}`, "");
    lines.push(`- status: ${scenario.status}`);
    lines.push(`- drift: ${scenario.drift}`);
    lines.push(
      `- aether: ${scenario.aetherStatus} (${scenario.aetherToolCalls} tool calls, ${aetherTokens} tokens)`,
    );
    lines.push(
      `- codex: ${scenario.codexStatus} (${scenario.codexToolCalls} tool calls, ${codexTokens} tokens)`,
    );
    lines.push(
      `- wall time: aether ${formatRuntimeWallClockMs(scenario.aetherWallClockMs)}; codex ${formatRuntimeWallClockMs(scenario.codexWallClockMs)}; ${formatRuntimeSpeedComparison(scenario)}`,
    );
    if (
      scenario.aetherBootstrapWallClockMs !== undefined ||
      scenario.codexBootstrapWallClockMs !== undefined
    ) {
      lines.push(
        `- gateway bootstrap (excluded): aether ${formatRuntimeWallClockMs(scenario.aetherBootstrapWallClockMs ?? null)}; codex ${formatRuntimeWallClockMs(scenario.codexBootstrapWallClockMs ?? null)}`,
      );
    }
    lines.push(
      `- prompt cache: aether ${formatRuntimeCacheHitPercent(scenario.aetherUsage?.cacheHitPercent)} (${formatRuntimeCacheCount(scenario.aetherUsage?.cachedInputTokens)} cached, ${formatRuntimeCacheCount(scenario.aetherUsage?.uncachedInputTokens)} uncached input); codex ${formatRuntimeCacheHitPercent(scenario.codexUsage?.cacheHitPercent)} (${formatRuntimeCacheCount(scenario.codexUsage?.cachedInputTokens)} cached, ${formatRuntimeCacheCount(scenario.codexUsage?.uncachedInputTokens)} uncached input)`,
    );
    lines.push(
      `- post-warm cache misses: aether ${formatRuntimeCacheMisses(scenario.aetherCacheDiagnostics)}; codex ${formatRuntimeCacheMisses(scenario.codexCacheDiagnostics)}`,
    );
    if (scenario.runtimeParityUsage.expectation === "not-applicable") {
      lines.push(`- assistant-message usage: N/A (${scenario.runtimeParityUsage.reason})`);
    }
    if (scenario.driftDetails) {
      lines.push(`- details: ${scenario.driftDetails}`);
    }
    lines.push("");
  }
  lines.push("## Notes", "");
  for (const note of report.notes) {
    lines.push(`- ${note}`);
  }
  lines.push("");
  return lines.join("\n");
}
