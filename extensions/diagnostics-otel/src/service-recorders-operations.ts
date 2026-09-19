import { ROOT_CONTEXT, SpanStatusCode } from "@opentelemetry/api";
import {
  normalizeDiagnosticValue,
  normalizeDiagnosticLane,
} from "aether/plugin-sdk/diagnostic-runtime";
import { redactSensitiveText } from "../api.js";
import type {
  DiagnosticEventMetadata,
  DiagnosticEventPayload,
  DiagnosticEventPrivateData,
} from "../api.js";
import { normalizeOtelErrorMessage } from "./service-content-normalization.js";
import type { DiagnosticsRecorderRuntime } from "./service-recorder-runtime.js";
import type { SessionRecoveryDiagnosticEvent, TalkDiagnosticEvent } from "./service-types.js";

export function createOperationsRecorders(runtime: DiagnosticsRecorderRuntime) {
  const {
    durationHistogram,
    gatewayRpcRequestsCounter,
    gatewayRpcOutcomesCounter,
    gatewayRpcFirstResponseHistogram,
    gatewayRpcHandlerHistogram,
    gatewayRpcAdmissionHistogram,
    gatewayRpcQueueWaitHistogram,
    queueDepthHistogram,
    queueWaitHistogram,
    laneEnqueueCounter,
    laneDequeueCounter,
    sessionStateCounter,
    sessionTurnCreatedCounter,
    sessionStuckCounter,
    sessionStuckAgeHistogram,
    sessionRecoveryRequestedCounter,
    sessionRecoveryCompletedCounter,
    sessionRecoveryAgeHistogram,
    talkEventCounter,
    talkEventDurationHistogram,
    talkAudioBytesHistogram,
    runAttemptCounter,
    toolLoopCounter,
    memoryRssHistogram,
    memoryHeapUsedHistogram,
    memoryHeapTotalHistogram,
    memoryExternalHistogram,
    memoryArrayBuffersHistogram,
    memoryPressureCounter,
    asyncQueueDroppedCounter,
    tracer,
    activeTrustedSpans,
    spanWithDuration,
    trustedTraceContext,
    activeTrustedParentContext,
    internalOrTrustedExplicitParentContext,
    setSpanAttrs,
    completeTrackedLifecycleSpan,
    addRunAttrs,
    tracesEnabled,
  } = runtime;

  const recordGatewayRpc = (
    evt: Extract<DiagnosticEventPayload, { type: "gateway.rpc" }>,
    metadata: DiagnosticEventMetadata,
  ) => {
    if (!metadata.trusted) {
      return;
    }
    const attrs = { "aether.gateway.rpc.method": evt.method };
    if (evt.phase === "received") {
      gatewayRpcRequestsCounter.add(1, attrs);
      return;
    }
    const outcomeAttrs = {
      "aether.gateway.rpc.phase": evt.phase,
      "aether.gateway.rpc.outcome": evt.outcome,
    };
    gatewayRpcOutcomesCounter.add(1, outcomeAttrs);
    switch (evt.phase) {
      case "response":
        if (evt.outcome === "ok" || evt.outcome === "error") {
          gatewayRpcFirstResponseHistogram.record(evt.durationMs, attrs);
        }
        break;
      case "handler":
        gatewayRpcHandlerHistogram.record(evt.durationMs, attrs);
        gatewayRpcAdmissionHistogram.record(evt.admissionMs, attrs);
        break;
      case "dispatch":
        if (evt.queueWaitMs !== undefined) {
          gatewayRpcQueueWaitHistogram.record(evt.queueWaitMs, attrs);
        }
        break;
    }
    if (!tracesEnabled) {
      return;
    }
    // These completed observations do not own the handler or later response callbacks.
    // Preserve the explicit upstream parent; an absent parent must not borrow the export callback scope.
    const span = spanWithDuration(
      `aether.gateway.rpc.${evt.phase}`,
      {
        ...attrs,
        ...outcomeAttrs,
        ...(evt.phase === "handler"
          ? { "aether.gateway.rpc.admission_ms": evt.admissionMs }
          : {}),
        ...(evt.phase === "dispatch" ? { "aether.gateway.rpc.response": evt.response } : {}),
      },
      evt.durationMs,
      {
        endTimeMs: evt.ts,
        parentContext: internalOrTrustedExplicitParentContext(evt, metadata) ?? ROOT_CONTEXT,
      },
    );
    if (evt.outcome === "error" || evt.outcome === "threw") {
      span.setStatus({ code: SpanStatusCode.ERROR });
    }
    span.end(evt.ts);
  };

  const recordLaneEnqueue = (
    evt: Extract<DiagnosticEventPayload, { type: "queue.lane.enqueue" }>,
  ) => {
    const attrs = { "aether.lane": normalizeDiagnosticLane(evt.lane) };
    laneEnqueueCounter.add(1, attrs);
    queueDepthHistogram.record(evt.queueSize, attrs);
  };

  const recordLaneDequeue = (
    evt: Extract<DiagnosticEventPayload, { type: "queue.lane.dequeue" }>,
  ) => {
    const attrs = { "aether.lane": normalizeDiagnosticLane(evt.lane) };
    laneDequeueCounter.add(1, attrs);
    queueDepthHistogram.record(evt.queueSize, attrs);
    if (typeof evt.waitMs === "number") {
      queueWaitHistogram.record(evt.waitMs, attrs);
    }
  };

  const recordSessionState = (evt: Extract<DiagnosticEventPayload, { type: "session.state" }>) => {
    const attrs: Record<string, string> = { "aether.state": evt.state };
    if (evt.reason) {
      attrs["aether.reason"] = redactSensitiveText(evt.reason);
    }
    sessionStateCounter.add(1, attrs);
  };

  const recordSessionTurnCreated = (
    evt: Extract<DiagnosticEventPayload, { type: "session.turn.created" }>,
  ) => {
    sessionTurnCreatedCounter.add(1, {
      "aether.agent": normalizeDiagnosticValue(evt.agentId, "unknown"),
      "aether.channel": normalizeDiagnosticValue(evt.channel, "unknown"),
      "aether.trigger": evt.trigger,
    });
  };

  const recordSessionStuck = (evt: Extract<DiagnosticEventPayload, { type: "session.stuck" }>) => {
    const attrs: Record<string, string> = { "aether.state": evt.state };
    sessionStuckCounter.add(1, attrs);
    if (typeof evt.ageMs === "number") {
      sessionStuckAgeHistogram.record(evt.ageMs, attrs);
    }
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number> = { ...attrs };
    spanAttrs["aether.queueDepth"] = evt.queueDepth ?? 0;
    spanAttrs["aether.ageMs"] = evt.ageMs;
    const span = tracer.startSpan("aether.session.stuck", { attributes: spanAttrs });
    span.setStatus({ code: SpanStatusCode.ERROR, message: "session stuck" });
    span.end();
  };

  const sessionRecoveryAttrs = (evt: SessionRecoveryDiagnosticEvent) => {
    const attrs: Record<string, string> = { "aether.state": evt.state };
    if (evt.reason) {
      attrs["aether.reason"] = redactSensitiveText(evt.reason);
    }
    if (evt.activeWorkKind) {
      attrs["aether.active_work_kind"] = evt.activeWorkKind;
    }
    return attrs;
  };

  const recordSessionRecoveryRequested = (
    evt: Extract<DiagnosticEventPayload, { type: "session.recovery.requested" }>,
  ) => {
    const attrs = sessionRecoveryAttrs(evt);
    attrs["aether.action"] = evt.allowActiveAbort ? "abort" : "recover";
    sessionRecoveryRequestedCounter.add(1, attrs);
    sessionRecoveryAgeHistogram.record(evt.ageMs, attrs);
  };

  const recordSessionRecoveryCompleted = (
    evt: Extract<DiagnosticEventPayload, { type: "session.recovery.completed" }>,
  ) => {
    const attrs = sessionRecoveryAttrs(evt);
    attrs["aether.status"] = evt.status;
    attrs["aether.action"] = normalizeDiagnosticValue(evt.action, "unknown");
    if (evt.outcomeReason) {
      attrs["aether.reason"] = redactSensitiveText(evt.outcomeReason);
    }
    sessionRecoveryCompletedCounter.add(1, attrs);
    sessionRecoveryAgeHistogram.record(evt.ageMs, attrs);
  };

  const talkEventAttrs = (evt: TalkDiagnosticEvent): Record<string, string> => ({
    "aether.talk.brain": normalizeDiagnosticValue(evt.brain),
    "aether.talk.event_type": normalizeDiagnosticValue(evt.talkEventType),
    "aether.talk.mode": normalizeDiagnosticValue(evt.mode),
    "aether.talk.provider": normalizeDiagnosticValue(evt.provider),
    "aether.talk.transport": normalizeDiagnosticValue(evt.transport),
  });

  const recordTalkEvent = (evt: TalkDiagnosticEvent, metadata: DiagnosticEventMetadata) => {
    if (!metadata.trusted) {
      return;
    }
    const attrs = talkEventAttrs(evt);
    talkEventCounter.add(1, attrs);
    if (typeof evt.durationMs === "number") {
      talkEventDurationHistogram.record(evt.durationMs, attrs);
    }
    if (typeof evt.byteLength === "number") {
      talkAudioBytesHistogram.record(evt.byteLength, attrs);
    }
  };

  const recordRunAttempt = (evt: Extract<DiagnosticEventPayload, { type: "run.attempt" }>) => {
    runAttemptCounter.add(1, { "aether.attempt": evt.attempt });
  };

  const toolLoopAttrs = (
    evt: Extract<DiagnosticEventPayload, { type: "tool.loop" }>,
  ): Record<string, string | number> => ({
    "aether.toolName": normalizeDiagnosticValue(evt.toolName, "tool"),
    "aether.loop.level": evt.level,
    "aether.loop.action": evt.action,
    "aether.loop.detector": evt.detector,
    "aether.loop.count": evt.count,
    ...(evt.pairedToolName
      ? { "aether.loop.paired_tool": normalizeDiagnosticValue(evt.pairedToolName, "tool") }
      : {}),
  });

  const recordToolLoop = (evt: Extract<DiagnosticEventPayload, { type: "tool.loop" }>) => {
    const attrs = toolLoopAttrs(evt);
    toolLoopCounter.add(1, attrs);
    if (!tracesEnabled) {
      return;
    }
    const span = spanWithDuration("aether.tool.loop", attrs, 0, { endTimeMs: evt.ts });
    if (evt.level === "critical" || evt.action === "block") {
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: `${evt.detector}:${evt.action}`,
      });
    }
    span.end(evt.ts);
  };

  const recordMemoryUsageMetrics = (
    evt: Extract<
      DiagnosticEventPayload,
      { type: "diagnostic.memory.sample" | "diagnostic.memory.pressure" }
    >,
    attrs: Record<string, string> = {},
  ) => {
    memoryRssHistogram.record(evt.memory.rssBytes, attrs);
    memoryHeapUsedHistogram.record(evt.memory.heapUsedBytes, attrs);
    memoryHeapTotalHistogram.record(evt.memory.heapTotalBytes, attrs);
    memoryExternalHistogram.record(evt.memory.externalBytes, attrs);
    memoryArrayBuffersHistogram.record(evt.memory.arrayBuffersBytes, attrs);
  };

  const recordMemorySample = (
    evt: Extract<DiagnosticEventPayload, { type: "diagnostic.memory.sample" }>,
  ) => {
    recordMemoryUsageMetrics(evt);
  };

  const recordMemoryPressure = (
    evt: Extract<DiagnosticEventPayload, { type: "diagnostic.memory.pressure" }>,
  ) => {
    const attrs = {
      "aether.memory.level": evt.level,
      "aether.memory.reason": evt.reason,
    };
    memoryPressureCounter.add(1, attrs);
    recordMemoryUsageMetrics(evt, attrs);
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number | boolean> = {
      ...attrs,
      "aether.memory.rss_bytes": evt.memory.rssBytes,
      "aether.memory.heap_used_bytes": evt.memory.heapUsedBytes,
      "aether.memory.heap_total_bytes": evt.memory.heapTotalBytes,
      "aether.memory.external_bytes": evt.memory.externalBytes,
      "aether.memory.array_buffers_bytes": evt.memory.arrayBuffersBytes,
      ...(evt.thresholdBytes !== undefined
        ? { "aether.memory.threshold_bytes": evt.thresholdBytes }
        : {}),
      ...(evt.rssGrowthBytes !== undefined
        ? { "aether.memory.rss_growth_bytes": evt.rssGrowthBytes }
        : {}),
      ...(evt.windowMs !== undefined ? { "aether.memory.window_ms": evt.windowMs } : {}),
    };
    const span = spanWithDuration("aether.memory.pressure", spanAttrs, 0, {
      endTimeMs: evt.ts,
    });
    if (evt.level === "critical") {
      span.setStatus({
        code: SpanStatusCode.ERROR,
        message: evt.reason,
      });
    }
    span.end(evt.ts);
  };

  const recordAsyncQueueDropped = (
    evt: Extract<DiagnosticEventPayload, { type: "diagnostic.async_queue.dropped" }>,
  ) => {
    asyncQueueDroppedCounter.add(evt.droppedEvents, {
      "aether.diagnostic.async_queue.drop_class": "total",
    });
    if (evt.droppedTrustedEvents !== undefined) {
      asyncQueueDroppedCounter.add(evt.droppedTrustedEvents, {
        "aether.diagnostic.async_queue.drop_class": "trusted",
      });
    }
    if (evt.droppedUntrustedEvents !== undefined) {
      asyncQueueDroppedCounter.add(evt.droppedUntrustedEvents, {
        "aether.diagnostic.async_queue.drop_class": "untrusted",
      });
    }
    if (evt.droppedPriorityEvents !== undefined) {
      asyncQueueDroppedCounter.add(evt.droppedPriorityEvents, {
        "aether.diagnostic.async_queue.drop_class": "priority",
      });
    }
  };

  const recordRunCompleted = (
    evt: Extract<DiagnosticEventPayload, { type: "run.completed" }>,
    metadata: DiagnosticEventMetadata,
    privateData: DiagnosticEventPrivateData,
  ) => {
    const attrs: Record<string, string | number> = {
      "aether.outcome": evt.outcome,
      "aether.provider": evt.provider ?? "unknown",
      "aether.model": evt.model ?? "unknown",
    };
    if (evt.channel) {
      attrs["aether.channel"] = evt.channel;
    }
    if (evt.blockedBy) {
      attrs["aether.blocked_by"] = normalizeDiagnosticValue(evt.blockedBy, "unknown");
    }
    durationHistogram.record(evt.durationMs, attrs);
    if (!tracesEnabled) {
      return;
    }
    const spanAttrs: Record<string, string | number | boolean> = {
      "aether.outcome": evt.outcome,
    };
    addRunAttrs(spanAttrs, evt);
    if (evt.blockedBy) {
      spanAttrs["aether.blocked_by"] = normalizeDiagnosticValue(evt.blockedBy, "unknown");
    }
    if (evt.errorCategory) {
      spanAttrs["aether.errorCategory"] = normalizeDiagnosticValue(evt.errorCategory, "other");
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
      spanWithDuration("aether.run", spanAttrs, evt.durationMs, {
        parentContext: activeTrustedParentContext(evt, metadata),
        endTimeMs: evt.ts,
      });
    setSpanAttrs(span, spanAttrs);
    if (evt.outcome === "error") {
      const message =
        redactedError ?? (evt.errorCategory ? redactSensitiveText(evt.errorCategory) : undefined);
      span.setStatus({
        code: SpanStatusCode.ERROR,
        ...(message ? { message } : {}),
      });
    }
    if (trackedSpan && trustedTrace?.spanId) {
      completeTrackedLifecycleSpan(trustedTrace, trackedSpan, evt.ts);
      return;
    }
    span.end(evt.ts);
  };

  return {
    recordGatewayRpc,
    recordLaneEnqueue,
    recordLaneDequeue,
    recordSessionState,
    recordSessionTurnCreated,
    recordSessionStuck,
    recordSessionRecoveryRequested,
    recordSessionRecoveryCompleted,
    recordTalkEvent,
    recordRunAttempt,
    recordToolLoop,
    recordMemoryUsageMetrics,
    recordMemorySample,
    recordMemoryPressure,
    recordAsyncQueueDropped,
    recordRunCompleted,
  };
}
