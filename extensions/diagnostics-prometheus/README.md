# @aether/diagnostics-prometheus

Official Prometheus diagnostics exporter for Aether.

This plugin exposes Aether Gateway runtime metrics in Prometheus text format for Prometheus, Grafana, VictoriaMetrics, and compatible scrapers.

## Install

```bash
aether plugins install @aether/diagnostics-prometheus
```

Restart the Gateway after installing or updating the plugin.

## Configure

Enable the plugin and set the scrape endpoint options in `plugins.entries.diagnostics-prometheus.config`.

The full config surface, metric names, and scrape examples live in the docs:

- https://docs.aether.ai/gateway/prometheus

## Package

- Plugin id: `diagnostics-prometheus`
- Package: `@aether/diagnostics-prometheus`
- Minimum Aether host: `2026.4.25`
