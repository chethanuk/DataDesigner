from data_designer.engine.observability import RequestAdmissionEventSink
from data_designer.integrations.opentelemetry import OpenTelemetryRuntime

rt = OpenTelemetryRuntime()
try:
    print("isinstance(runtime, RequestAdmissionEventSink):", isinstance(rt, RequestAdmissionEventSink))
except TypeError as e:
    print("isinstance(runtime, RequestAdmissionEventSink): TypeError:", e)
try:
    from data_designer.engine.observability import FilteringRequestAdmissionEventSink, FilteringSchedulerAdmissionEventSink
    print("runtime is FilteringSchedulerAdmissionEventSink:", isinstance(rt, FilteringSchedulerAdmissionEventSink))
    print("runtime is FilteringRequestAdmissionEventSink:", isinstance(rt, FilteringRequestAdmissionEventSink))
except ImportError as e:
    print("ImportError:", e)
