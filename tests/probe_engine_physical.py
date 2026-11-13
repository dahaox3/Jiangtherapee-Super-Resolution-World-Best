"""Run only via the isolated original-interpreter engine probe."""
from engine_patch.physical import gpu_selftest

report["physical_gpu"] = gpu_selftest()
