"""
OpenTelemetry provider wiring (OTLP/gRPC export to the A365 observability
endpoint).

``configure_otel_providers()`` must be called as the very first thing in
``main.py`` — before importing the agent or server — so the global providers
are installed before any instrumented library loads. The OTLP endpoint is taken
from ``OTEL_EXPORTER_OTLP_ENDPOINT`` and is never hard-coded. When no endpoint
is configured (local dev), providers are still installed so manual spans work,
but no exporter is attached.
"""

from __future__ import annotations

import logging

from . import config

logger = logging.getLogger(__name__)

_configured = False


def _build_resource():
    from opentelemetry.sdk.resources import Resource

    attrs = {
        "service.name": config.SERVICE_NAME,
        "service.namespace": config.SERVICE_NAMESPACE,
    }
    if config.AGENT_ID:
        attrs["agent.id"] = config.AGENT_ID
    if config.AGENT_MANAGER_USER_ID:
        attrs["manager.id"] = config.AGENT_MANAGER_USER_ID
    return Resource.create(attrs)


def _instrument_libraries() -> None:
    """Best-effort auto-instrumentation; missing instrumentation libs are ignored."""
    instrumentors = [
        ("opentelemetry.instrumentation.aiohttp_server", "AioHttpServerInstrumentor"),
        ("opentelemetry.instrumentation.aiohttp_client", "AioHttpClientInstrumentor"),
        ("opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
        ("opentelemetry.instrumentation.logging", "LoggingInstrumentor"),
    ]
    for module_name, cls_name in instrumentors:
        try:
            module = __import__(module_name, fromlist=[cls_name])
            getattr(module, cls_name)().instrument()
        except Exception as exc:  # pragma: no cover - optional deps
            logger.debug("Skipping instrumentation %s: %s", cls_name, exc)


def configure_otel_providers(service_name: str | None = None) -> None:
    """Install global tracer/meter/logger providers and (optionally) OTLP exporters."""
    global _configured
    if _configured:
        return
    if service_name:
        config.SERVICE_NAME = service_name

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    resource = _build_resource()
    tracer_provider = TracerProvider(resource=resource)

    endpoint = config.OTEL_EXPORTER_OTLP_ENDPOINT
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            tracer_provider.add_span_processor(
                SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )

            from opentelemetry import metrics
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

            metrics.set_meter_provider(
                MeterProvider(
                    resource=resource,
                    metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))],
                )
            )

            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

            logger_provider = LoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint))
            )
        except Exception as exc:  # pragma: no cover - optional deps
            logger.warning("OTLP exporter setup failed (%s); spans stay in-process.", exc)
    else:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set; OTLP export disabled.")

    trace.set_tracer_provider(tracer_provider)
    _instrument_libraries()
    _configured = True
    logger.info("OTEL providers configured for service '%s'.", config.SERVICE_NAME)
