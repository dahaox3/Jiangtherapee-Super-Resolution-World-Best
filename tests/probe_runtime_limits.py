"""Read-only capability/asset diagnosis in the isolated bundled interpreter."""
import os
import sys
import json

report["environment"] = {key: os.environ.get(key) for key in
                         ("JSR_WGPU_ADAPTER", "JSR_WGPU_BACKEND", "WGPU_BACKEND_TYPE")}
report["wgpu_modules_loaded_before_probe"] = sorted(name for name in sys.modules if name.startswith("wgpu"))
from v15_2.runtime import Runtime
from v15_2 import pipeline, tuning, memory

if os.environ.get("JSR_PROBE_LATE_BACKEND"):
    os.environ["JSR_WGPU_BACKEND"] = os.environ["JSR_PROBE_LATE_BACKEND"]
    os.environ["WGPU_BACKEND_TYPE"] = os.environ["JSR_PROBE_LATE_BACKEND"]
    report["late_backend"] = os.environ["JSR_PROBE_LATE_BACKEND"]

rt = Runtime(budget_bytes=1000000000)
try:
    report["capabilities"] = rt.capabilities
    report["device_limits"] = dict(rt.device.limits)
    report["memory_snapshot"] = memory.memory_snapshot(rt.capabilities.get("adapter", {}), 1000000000)
    report["roots"] = {"pipeline": str(pipeline.ROOT), "tuning": str(tuning.ROOT)}
    cache = memory.cache_directory() / (tuning.cache_key(rt.capabilities) + ".json")
    report["tuning_cache"] = {"path": str(cache), "exists": cache.is_file()}
    if cache.is_file():
        report["tuning_cache"]["data"] = json.loads(cache.read_text(encoding="utf8"))
    graphs = []
    for size in (96, 128, 192, 256):
        path = tuning.ROOT / ("wgpu/assets_v15_2/backend_%d.json" % size)
        row = {"grid": size, "path": str(path), "exists": path.is_file()}
        if path.is_file():
            row["max_graph_tensor"] = tuning.max_graph_tensor_bytes(path)
            row["within_storage_limit"] = row["max_graph_tensor"][0] <= rt.capabilities["max_storage_buffer_binding_size"]
        graphs.append(row)
    report["graphs"] = graphs
finally:
    rt.close()
