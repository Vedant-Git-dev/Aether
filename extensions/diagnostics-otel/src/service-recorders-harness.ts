import { SpanStatusCode } from "@opentelemetry/api";
import {
  normalizeDiagnosticValue,
  normalizeDiagnosticLane,
} from "aether/plugin-sdk/diagnostic-runtime";
import type {
  DiagnosticEventMetadata,
  DiagnosticEventPayload,
  DiagnosticEventPrivateData,
} from "../api.js";
import { normalizeOtelErrorMessage } from "./service-content-normalization.js";
import type { DiagnosticsRecorderRuntime } from "./service-recorder-runtime.js";
import type { HarnessRunDiagnosticEvent, ModelFailoverDiagnosticEvent } from "./service-types.js";

export function createHarnessRecorders(runtime: DiagnosticsRecorderRuntime) {
  const {
    harnessDurationHistogram,
    modelFailoverCounter,
    activeTrustedSpans,
    spanWithDuration,
    trustedTraceContext,
    activeTrustedParentContext,
    trackTrustedSpan,
    setSpanAttrs,
    completeTrackedLifecycleSpan,
    addRunAttrs,
    tracesEnabled,
  } = runtime;

  const harnessRunMetricAttrs = (evt: HarnessRunDiagnosticEvent) => ({
    "aether.harness.id": normalizeDiagnosticValue(evt.harnessId, "unknown"),
    "aether.harness.plugin": normalizeDiagnosticValue(evt.pluginId),
    ...(evt.type === "harness.run.started"
      ? {}
      : {
          "aether.outcome": evt.type === "harness.run.error" ? "error" : evt.outcome,
        }),
    "aether.provider": normalizeDiagnosticValue(evt.provider, "unknown"),
    "aether.model": normalizeDiagnosticValue(evt.model, "unknown"),
    ...(evt.channel ? { "aether.channel": normalizeDiagnosticValue(evt.channel) } : {}),
  });

  const recordHarnessRunStarted = (
    evt: Extract<DiagnosticEventPayload, { type: "harness.run.started" }>,
    metadata: DiagnosticEventMetadata,
  ) => {
    if (!tracesEnabled || !metadata.trusted) {
      return;
    }
    trackTrustedSpan(
      evt,
      metadata,
      spanWithDuration("aether.harness.run", harnessRunMetricAttrs(evt), undefined, {
        parentContext: activeTrustedParentContext(evt, metadata),
        startTimeMs: evt.ts,
      }),
    );
  };

  const recordHarnessRunCompleted = (
    evt: Extract<DiagnosticEventPayload, { type: "harness.run.completed" }>,
    metadata: DiagnosticEventMetadata,
    privateData: DiagnosticEventPrivateData,
  ) => {
    harnessDurationHistogram.record(evt.durationMs, harnessRunMetricAttrs(evt));
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number | boolean> = {
      ...harnessRunMetricAttrs(evt),
    };
    if (evt.resultClassification) {
      spanAttrs["aether.harness.result_classification"] = normalizeDiagnosticValue(
        evt.resultClassification,
      );
    }
    if (typeof evt.yieldDetected === "boolean") {
      spanAttrs["aether.harness.yield_detected"] = evt.yieldDetected;
    }
    if (evt.itemLifecycle) {
      spanAttrs["aether.harness.items.started"] = evt.itemLifecycle.startedCount;
      spanAttrs["aether.harness.items.completed"] = evt.itemLifecycle.completedCount;
      spanAttrs["aether.harness.items.active"] = evt.itemLifecycle.activeCount;
    }
    // Redacted message goes on the span only, never the low-cardinality metric attrs.
    const redactedError = normalizeOtelErrorMessage(privateData.errorMessage);
    if (redactedError) {
      spanAttrs["aether.error"] = redactedError;
    }
    const trustedTrace = trustedTraceContext(evt, metadata);
    const trackedSpan = trustedTrace?.spanId
      ? activeTrustedSpans.get(trustedTrace.spanId)
      : undefined;
    const span =
      trackedSpan ??
      spanWithDuration("aether.harness.run", spanAttrs, evt.durationMs, {
        parentContext: activeTrustedParentContext(evt, metadata),
        endTimeMs: evt.ts,
      });
    setSpanAttrs(span, spanAttrs);
    if (evt.outcome === "error") {
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: redactedError ?? "error",
      });
    }
    if (trackedSpan && trustedTrace?.spanId) {
      completeTrackedLifecycleSpan(trustedTrace, trackedSpan, evt.ts);
      return;
    }
    span.end(evt.ts);
  };

  const recordHarnessRunError = (
    evt: Extract<DiagnosticEventPayload, { type: "harness.run.error" }>,
    metadata: DiagnosticEventMetadata,
    privateData: DiagnosticEventPrivateData,
  ) => {
    const errorType = normalizeDiagnosticValue(evt.errorCategory, "other");
    const attrs = {
      ...harnessRunMetricAttrs(evt),
      "aether.harness.phase": evt.phase,
      "aether.errorCategory": errorType,
    };
    harnessDurationHistogram.record(evt.durationMs, attrs);
    if (!tracesEnabled) {
      return;
    }
    // Redacted message goes on the span only; attrs above feed the metric.
    const redactedError = normalizeOtelErrorMessage(privateData.errorMessage);
    const spanAttrs: Record<string, string | number | boolean> = {
      ...attrs,
      "error.type": errorType,
      ...(redactedError ? { "aether.error": redactedError } : {}),
      ...(evt.cleanupFailed ? { "aether.harness.cleanup_failed": true } : {}),
    };
    const trustedTrace = trustedTraceContext(evt, metadata);
    const trackedSpan = trustedTrace?.spanId
      ? activeTrustedSpans.get(trustedTrace.spanId)
      : undefined;
    const span =
      trackedSpan ??
      spanWithDuration("aether.harness.run", spanAttrs, evt.durationMs, {
        parentContext: activeTrustedParentContext(evt, metadata),
        endTimeMs: evt.ts,
      });
    setSpanAttrs(span, spanAttrs);
    span.setStatus({
      code: SpanStatusCode.ERROR,
      message: redactedError ?? errorType,
    });
    // Retain on the error path too: for the aether harness this span is the only
    // ancestor a late child can attach to, and aborted turns emit no run.completed.
    if (trackedSpan && trustedTrace?.spanId) {
      completeTrackedLifecycleSpan(trustedTrace, trackedSpan, evt.ts);
      return;
    }
    span.end(evt.ts);
  };

  const recordContextAssembled = (
    evt: Extract<DiagnosticEventPayload, { type: "context.assembled" }>,
    metadata: DiagnosticEventMetadata,
  ) => {
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number | boolean> = {
      "aether.context.message_count": evt.messageCount,
      "aether.context.history_text_chars": evt.historyTextChars,
      "aether.context.history_image_blocks": evt.historyImageBlocks,
      "aether.context.max_message_text_chars": evt.maxMessageTextChars,
      "aether.context.system_prompt_chars": evt.systemPromptChars,
      "aether.context.prompt_chars": evt.promptChars,
      "aether.context.prompt_images": evt.promptImages,
    };
    addRunAttrs(spanAttrs, evt);
    if (evt.contextTokenBudget !== undefined) {
      spanAttrs["aether.context.token_budget"] = evt.contextTokenBudget;
    }
    if (evt.reserveTokens !== undefined) {
      spanAttrs["aether.context.reserve_tokens"] = evt.reserveTokens;
    }
    const span = spanWithDuration("aether.context.assembled", spanAttrs, 0, {
      parentContext: activeTrustedParentContext(evt, metadata),
      endTimeMs: evt.ts,
    });
    span.end(evt.ts);
  };

  const recordModelFailover = (
    evt: ModelFailoverDiagnosticEvent,
    metadata: DiagnosticEventMetadata,
  ) => {
    const metricAttrs: Record<string, string> = {
      "aether.failover.reason": normalizeDiagnosticValue(evt.reason, "unknown"),
      "aether.failover.suspended":
        evt.suspended === undefined ? "unknown" : String(evt.suspended),
      "aether.lane": normalizeDiagnosticLane(evt.lane, "unknown"),
      "aether.model": normalizeDiagnosticValue(evt.fromModel),
      "aether.provider": normalizeDiagnosticValue(evt.fromProvider),
      "aether.failover.to_model": normalizeDiagnosticValue(evt.toModel),
      "aether.failover.to_provider": normalizeDiagnosticValue(evt.toProvider),
    };
    modelFailoverCounter.add(1, metricAttrs);
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number | boolean> = {
      "aether.failover.reason": normalizeDiagnosticValue(evt.reason, "unknown"),
    };
    if (evt.fromProvider) {
      spanAttrs["aether.provider"] = evt.fromProvider;
    }
    if (evt.fromModel) {
      spanAttrs["aether.model"] = evt.fromModel;
    }
    if (evt.toProvider) {
      spanAttrs["aether.failover.to_provider"] = evt.toProvider;
    }
    if (evt.toModel) {
      spanAttrs["aether.failover.to_model"] = evt.toModel;
    }
    if (evt.lane) {
      spanAttrs["aether.lane"] = normalizeDiagnosticLane(evt.lane, "unknown");
    }
    if (evt.suspended !== undefined) {
      spanAttrs["aether.failover.suspended"] = evt.suspended;
    }
    if (evt.cascadeDepth !== undefined) {
      spanAttrs["aether.failover.cascade_depth"] = evt.cascadeDepth;
    }
    const span = spanWithDuration("aether.model.failover", spanAttrs, 0, {
      parentContext: activeTrustedParentContext(evt, metadata),
      endTimeMs: evt.ts,
    });
    span.end(evt.ts);
  };

  return {
    recordHarnessRunStarted,
    recordHarnessRunCompleted,
    recordHarnessRunError,
    recordContextAssembled,
    recordModelFailover,
  };
}
