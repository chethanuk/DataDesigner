from data_designer.integrations.opentelemetry import OpenTelemetryRuntime


def misuse(rt: OpenTelemetryRuntime) -> None:
    assert rt._dataset_records is not None
    rt._dataset_records.record(1)  # Counter has add(), not record()
