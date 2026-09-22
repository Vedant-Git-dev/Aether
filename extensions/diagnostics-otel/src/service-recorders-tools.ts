import { ROOT_CONTEXT, SpanStatusCode } from "@opentelemetry/api";
import {
  isInternalDiagnosticEventMetadata,
  normalizeDiagnosticValue,
} from "aether/plugin-sdk/diagnostic-runtime";
import { redactSensitiveText } from "../api.js";
import type { DiagnosticEventMetadata, DiagnosticEventPayload } from "../api.js";
import { positiveFiniteNumber } from "./service-genai-attributes.js";
import {
  assignOtelToolContentAttributes,
  assignOtelToolIdentityAttributes,
} from "./service-genai-content.js";
import type { OtelToolCallContent } from "./service-genai-content.js";
import type { DiagnosticsRecorderRuntime } from "./service-recorder-runtime.js";
import type { TelemetryExporterDiagnosticEvent } from "./service-types.js";

export function createToolAndSystemRecorders(runtime: DiagnosticsRecorderRuntime) {
  const {
    gcDurationHistogram,
    gatewayEventLoopDelayMaxHistogram,
    gatewayEventLoopObservedCounter,
    queueDepthHistogram,
    skillUsedCounter,
    toolExecutionDurationHistogram,
    toolExecutionBlockedCounter,
    execProcessDurationHistogram,
    payloadLargeCounter,
    payloadLargeBytesHistogram,
    livenessWarningCounter,
    livenessEventLoopDelayP99Histogram,
    livenessEventLoopDelayMaxHistogram,
    livenessEventLoopUtilizationHistogram,
    livenessCpuCoreRatioHistogram,
    telemetryExporterCounter,
    spanWithDuration,
    activeTrustedParentContext,
    exportedInternalOrTrustedContext,
    trackTrustedSpan,
    getTrackedInternalOrTrustedSpan,
    takeTrackedTrustedSpan,
    setSpanAttrs,
    addRunAttrs,
    paramsSummaryAttrs,
    contentCapturePolicy,
    tracesEnabled,
  } = runtime;

  const toolExecutionBaseAttrs = (
    evt: Extract<
      DiagnosticEventPayload,
      {
        type:
          | "tool.execution.started"
          | "tool.execution.completed"
          | "tool.execution.error"
          | "tool.execution.blocked";
      }
    >,
  ): Record<string, string | number | boolean> => ({
    "aether.toolName": evt.toolName,
    "aether.tool.source": normalizeDiagnosticValue(evt.toolSource, "core"),
    "gen_ai.tool.name": evt.toolName,
    ...(evt.toolOwner ? { "aether.tool.owner": normalizeDiagnosticValue(evt.toolOwner) } : {}),
    ...paramsSummaryAttrs(evt.paramsSummary),
  });
  const toolTimestampMs = (evt: { sourceTimestampMs?: number; ts: number }) =>
    evt.sourceTimestampMs ?? evt.ts;

  const skillUsedAttrs = (
    evt: Extract<DiagnosticEventPayload, { type: "skill.used" }>,
  ): Record<string, string | number | boolean> => ({
    "aether.skill.name": normalizeDiagnosticValue(evt.skillName, "skill"),
    "aether.skill.source": normalizeDiagnosticValue(evt.skillSource),
    "aether.skill.activation": normalizeDiagnosticValue(evt.activation),
    ...(evt.agentId ? { "aether.agent": normalizeDiagnosticValue(evt.agentId) } : {}),
    ...(evt.toolName
      ? { "aether.toolName": normalizeDiagnosticValue(evt.toolName, "tool") }
      : {}),
  });

  const recordSkillUsed = (
    evt: Extract<DiagnosticEventPayload, { type: "skill.used" }>,
    metadata: DiagnosticEventMetadata,
  ) => {
    if (!metadata.trusted) {
      return;
    }
    const attrs = skillUsedAttrs(evt);
    skillUsedCounter.add(1, attrs);
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number | boolean> = { ...attrs };
    addRunAttrs(spanAttrs, evt);
    const span = spanWithDuration("aether.skill.used", spanAttrs, 0, {
      parentContext: activeTrustedParentContext(evt, metadata),
      endTimeMs: evt.ts,
    });
    setSpanAttrs(span, spanAttrs);
    span.end(evt.ts);
  };

  const recordToolExecutionStarted = (
    evt: Extract<DiagnosticEventPayload, { type: "tool.execution.started" }>,
    metadata: DiagnosticEventMetadata,
  ) => {
    if (!tracesEnabled || !metadata.trusted) {
      return undefined;
    }
    const trackedSpan = getTrackedInternalOrTrustedSpan(evt, metadata);
    if (trackedSpan) {
      return trackedSpan.spanContext();
    }
    const spanAttrs = toolExecutionBaseAttrs(evt);
    assignOtelToolIdentityAttributes(spanAttrs, evt);
    return trackTrustedSpan(
      evt,
      metadata,
      spanWithDuration("aether.tool.execution", spanAttrs, undefined, {
        parentContext: activeTrustedParentContext(evt, metadata),
        startTimeMs: toolTimestampMs(evt),
      }),
    ).spanContext();
  };

  const recordToolExecutionFinished = (
    evt: Extract<
      DiagnosticEventPayload,
      { type: "tool.execution.completed" | "tool.execution.error" }
    >,
    metadata: DiagnosticEventMetadata,
    toolContent?: OtelToolCallContent,
  ) => {
    const attrs = toolExecutionBaseAttrs(evt);
    if (evt.type === "tool.execution.error") {
      attrs["aether.errorCategory"] = normalizeDiagnosticValue(evt.errorCategory, "other");
    }
    toolExecutionDurationHistogram.record(evt.durationMs, attrs);
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number | boolean> = { ...attrs };
    addRunAttrs(spanAttrs, evt);
    assignOtelToolIdentityAttributes(spanAttrs, evt);
    if (evt.type === "tool.execution.error" && evt.errorCode) {
      spanAttrs["aether.errorCode"] = normalizeDiagnosticValue(evt.errorCode, "other");
    }
    assignOtelToolContentAttributes(spanAttrs, toolContent, contentCapturePolicy);
    const span =
      takeTrackedTrustedSpan(evt, metadata) ??
      spanWithDuration("aether.tool.execution", spanAttrs, evt.durationMs, {
        parentContext: activeTrustedParentContext(evt, metadata),
        endTimeMs: toolTimestampMs(evt),
      });
    setSpanAttrs(span, spanAttrs);
    if (evt.type === "tool.execution.error") {
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: redactSensitiveText(evt.errorCategory),
      });
    }
    span.end(toolTimestampMs(evt));
  };

  const recordToolExecutionBlocked = (
    evt: Extract<DiagnosticEventPayload, { type: "tool.execution.blocked" }>,
    metadata: DiagnosticEventMetadata,
  ) => {
    toolExecutionBlockedCounter.add(1, {
      ...toolExecutionBaseAttrs(evt),
      "aether.deniedReason": normalizeDiagnosticValue(evt.deniedReason, "other"),
    });
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number | boolean> = {
      ...toolExecutionBaseAttrs(evt),
      "aether.outcome": "blocked",
      "aether.deniedReason": normalizeDiagnosticValue(evt.deniedReason, "other"),
    };
    addRunAttrs(spanAttrs, evt);
    assignOtelToolIdentityAttributes(spanAttrs, evt);
    const span =
      takeTrackedTrustedSpan(evt, metadata) ??
      spanWithDuration("aether.tool.execution", spanAttrs, 0, {
        parentContext: activeTrustedParentContext(evt, metadata),
        endTimeMs: toolTimestampMs(evt),
      });
    setSpanAttrs(span, spanAttrs);
    span.end(toolTimestampMs(evt));
  };

  const recordPayloadLarge = (evt: Extract<DiagnosticEventPayload, { type: "payload.large" }>) => {
    const attrs = {
      "aether.payload.action": evt.action,
      "aether.payload.surface": normalizeDiagnosticValue(evt.surface, "unknown"),
      "aether.channel": normalizeDiagnosticValue(evt.channel, "none"),
      "aether.plugin": normalizeDiagnosticValue(evt.pluginId, "none"),
      "aether.reason": normalizeDiagnosticValue(evt.reason, "none"),
    };
    payloadLargeCounter.add(1, attrs);
    const bytes = positiveFiniteNumber(evt.bytes);
    if (bytes !== undefined) {
      payloadLargeBytesHistogram.record(bytes, attrs);
    }
  };

  const recordExecProcessCompleted = (
    evt: Extract<DiagnosticEventPayload, { type: "exec.process.completed" }>,
    metadata: DiagnosticEventMetadata,
  ) => {
    const attrs: Record<string, string | number> = {
      "aether.exec.target": evt.target,
      "aether.exec.mode": evt.mode,
      "aether.outcome": evt.outcome,
    };
    if (evt.failureKind) {
      attrs["aether.failureKind"] = evt.failureKind;
    }
    execProcessDurationHistogram.record(evt.durationMs, attrs);
    if (!tracesEnabled) {
      return;
    }

    const spanAttrs: Record<string, string | number | boolean> = {
      ...attrs,
      "aether.exec.command_length": evt.commandLength,
    };
    if (typeof evt.exitCode === "number") {
      spanAttrs["aether.exec.exit_code"] = evt.exitCode;
    }
    if (evt.exitSignal) {
      spanAttrs["aether.exec.exit_signal"] = normalizeDiagnosticValue(evt.exitSignal, "other");
    }
    if (evt.timedOut !== undefined) {
      spanAttrs["aether.exec.timed_out"] = evt.timedOut;
    }

    // Exec events carry the innermost ambient scope rather than a child context, so
    // the parent is looked up by the event's own span id first. For the aether
    // harness that scope is the harness run (no run scope is opened -
    // shouldEmitAgentRunDiagnostics is false there), so the parent is
    // aether.harness.run; other harnesses open a run scope and parent to aether.run.
    const span = spanWithDuration("aether.exec", spanAttrs, evt.durationMs, {
      parentContext: exportedInternalOrTrustedContext(evt, metadata),
      endTimeMs: evt.ts,
    });
    if (evt.outcome === "failed") {
      span.setStatus({
        code: SpanStatusCode.ERROR,
        ...(evt.failureKind ? { message: evt.failureKind } : {}),
      });
    }
    span.end(evt.ts);
  };

  const recordGcDuration = (
    evt: Extract<DiagnosticEventPayload, { type: "diagnostic.gc" }>,
    metadata: DiagnosticEventMetadata,
  ) => {
    if (!metadata.trusted && !isInternalDiagnosticEventMetadata(metadata)) {
      return;
    }
    gcDurationHistogram.record(evt.durationMs, undefined, ROOT_CONTEXT);
  };

  const recordGatewayEventLoopSample = (
    evt: Extract<DiagnosticEventPayload, { type: "gateway.event_loop.sample" }>,
    metadata: DiagnosticEventMetadata,
  ) => {
    if (!metadata.trusted && !isInternalDiagnosticEventMetadata(metadata)) {
      return;
    }
    // Process-wide windows must not inherit the reader's trace through an external SDK.
    gatewayEventLoopDelayMaxHistogram.record(evt.delayMaxMs, undefined, ROOT_CONTEXT);
    gatewayEventLoopObservedCounter.add(evt.intervalMs, undefined, ROOT_CONTEXT);
  };

  const recordHeartbeat = (
    evt: Extract<DiagnosticEventPayload, { type: "diagnostic.heartbeat" }>,
  ) => {
    queueDepthHistogram.record(evt.queued, { "aether.channel": "heartbeat" });
  };

  const recordLivenessWarning = (
    evt: Extract<DiagnosticEventPayload, { type: "diagnostic.liveness.warning" }>,
  ) => {
    const reason = evt.reasons.join(":");
    const attrs = {
      "aether.liveness.reason": normalizeDiagnosticValue(reason, "unknown"),
    };
    livenessWarningCounter.add(1, attrs);
    queueDepthHistogram.record(evt.queued, { "aether.channel": "liveness" });
    if (evt.eventLoopDelayP99Ms !== undefined) {
      livenessEventLoopDelayP99Histogram.record(evt.eventLoopDelayP99Ms, attrs);
    }
    if (evt.eventLoopDelayMaxMs !== undefined) {
      livenessEventLoopDelayMaxHistogram.record(evt.eventLoopDelayMaxMs, attrs);
    }
    if (evt.eventLoopUtilization !== undefined) {
      livenessEventLoopUtilizationHistogram.record(evt.eventLoopUtilization, attrs);
    }
    if (evt.cpuCoreRatio !== undefined) {
      livenessCpuCoreRatioHistogram.record(evt.cpuCoreRatio, attrs);
    }
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number> = {
      ...attrs,
      "aether.liveness.active": evt.active,
      "aether.liveness.waiting": evt.waiting,
      "aether.liveness.queued": evt.queued,
      "aether.liveness.interval_ms": evt.intervalMs,
      ...(evt.eventLoopDelayP99Ms !== undefined
        ? { "aether.liveness.event_loop_delay_p99_ms": evt.eventLoopDelayP99Ms }
        : {}),
      ...(evt.eventLoopDelayMaxMs !== undefined
        ? { "aether.liveness.event_loop_delay_max_ms": evt.eventLoopDelayMaxMs }
        : {}),
      ...(evt.eventLoopUtilization !== undefined
        ? { "aether.liveness.event_loop_utilization": evt.eventLoopUtilization }
        : {}),
      ...(evt.cpuUserMs !== undefined ? { "aether.liveness.cpu_user_ms": evt.cpuUserMs } : {}),
      ...(evt.cpuSystemMs !== undefined
        ? { "aether.liveness.cpu_system_ms": evt.cpuSystemMs }
        : {}),
      ...(evt.cpuTotalMs !== undefined ? { "aether.liveness.cpu_total_ms": evt.cpuTotalMs } : {}),
      ...(evt.cpuCoreRatio !== undefined
        ? { "aether.liveness.cpu_core_ratio": evt.cpuCoreRatio }
        : {}),
    };
    const span = spanWithDuration("aether.liveness.warning", spanAttrs, 0, {
      endTimeMs: evt.ts,
    });
    span.setStatus({
      code: SpanStatusCode.ERROR,
      message: reason,
    });
    span.end(evt.ts);
  };

  const recordDiagnosticPhaseCompleted = (
    evt: Extract<DiagnosticEventPayload, { type: "diagnostic.phase.completed" }>,
  ) => {
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number> = {
      "aether.phase": normalizeDiagnosticValue(evt.name, "unknown"),
      ...(evt.cpuUserMs !== undefined ? { "aether.phase.cpu_user_ms": evt.cpuUserMs } : {}),
      ...(evt.cpuSystemMs !== undefined ? { "aether.phase.cpu_system_ms": evt.cpuSystemMs } : {}),
      ...(evt.cpuTotalMs !== undefined ? { "aether.phase.cpu_total_ms": evt.cpuTotalMs } : {}),
      ...(evt.cpuCoreRatio !== undefined
        ? { "aether.phase.cpu_core_ratio": evt.cpuCoreRatio }
        : {}),
    };
    for (const [key, value] of Object.entries(evt.details ?? {})) {
      spanAttrs[`aether.phase.detail.${key}`] =
        typeof value === "boolean" ? String(value) : value;
    }
    const span = spanWithDuration("aether.diagnostic.phase", spanAttrs, evt.durationMs, {
      endTimeMs: evt.ts,
    });
    span.end(evt.ts);
  };

  const recordTelemetryExporter = (
    evt: TelemetryExporterDiagnosticEvent,
    metadata: DiagnosticEventMetadata,
  ) => {
    if (!metadata.trusted) {
      return;
    }
    telemetryExporterCounter.add(1, {
      "aether.exporter": normalizeDiagnosticValue(evt.exporter, "unknown"),
      "aether.signal": evt.signal,
      "aether.status": evt.status,
      ...(evt.reason ? { "aether.reason": evt.reason } : {}),
      ...(evt.errorCategory
        ? { "aether.errorCategory": normalizeDiagnosticValue(evt.errorCategory, "other") }
        : {}),
    });
  };

  return {
    recordGcDuration,
    recordGatewayEventLoopSample,
    recordSkillUsed,
    recordToolExecutionStarted,
    recordToolExecutionFinished,
    recordToolExecutionBlocked,
    recordPayloadLarge,
    recordExecProcessCompleted,
    recordHeartbeat,
    recordLivenessWarning,
    recordDiagnosticPhaseCompleted,
    recordTelemetryExporter,
  };
}
