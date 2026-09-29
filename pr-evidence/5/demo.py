import typing

import data_designer.config as dd
from data_designer.config.run_config import RunConfig

print(f"data_designer.config from: {dd.__file__.split('/packages/')[0].rsplit('/', 1)[-1]}")
rc = RunConfig(disable_early_shutdown=True, shutdown_error_rate=0.2)
print("RunConfig(disable_early_shutdown=True, shutdown_error_rate=0.2)")
print(f"  .shutdown_error_rate           = {rc.shutdown_error_rate}")
print(f"  .effective_shutdown_error_rate = {getattr(rc, 'effective_shutdown_error_rate', '<no such attribute>')}")
print(f"  model_dump()['shutdown_error_rate'] = {rc.model_dump()['shutdown_error_rate']}")
try:
    ret = typing.get_type_hints(dd.ThrottleConfig.to_request_admission_tuning)["return"]
    print(f"get_type_hints(ThrottleConfig.to_request_admission_tuning)['return'] = {ret.__name__}")
except Exception as e:
    print(f"get_type_hints(ThrottleConfig.to_request_admission_tuning) -> {type(e).__name__}: {e}")
