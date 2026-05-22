"""Process memory telemetry helper.

Extracted from ``sharepoint_ingest.ingestion_engine`` (formerly the
``_read_process_memory_mb`` method).  Kept in a separate module because
the Windows ctypes fallback makes it a dominant fraction of the engine
file without contributing to core orchestration logic.
"""

from __future__ import annotations

import os
from typing import Optional

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None  # type: ignore[assignment]


def read_process_memory_mb() -> Optional[float]:
    """Return the current process RSS memory usage in MiB, or ``None``.

    Tries ``psutil`` first (cross-platform), then falls back to the
    Windows ``Psapi.GetProcessMemoryInfo`` API via ``ctypes``.
    """
    if psutil is not None:
        try:
            rss_bytes = psutil.Process().memory_info().rss
            return round(float(rss_bytes) / (1024 * 1024), 2)
        except Exception:  # pragma: no cover
            pass

    # Windows fallback when psutil is unavailable at runtime
    if os.name != "nt":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):  # noqa: N801
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)

        kernel32 = ctypes.WinDLL("Kernel32.dll")
        psapi = ctypes.WinDLL("Psapi.dll")

        process_handle = kernel32.GetCurrentProcess()
        get_pmi = psapi.GetProcessMemoryInfo
        get_pmi.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        get_pmi.restype = wintypes.BOOL

        ok = get_pmi(process_handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return None

        return round(float(counters.WorkingSetSize) / (1024 * 1024), 2)
    except Exception:  # pragma: no cover
        return None
