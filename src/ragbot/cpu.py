"""Keep Windows from throttling the model work (Phase I2).

Windows 11 power throttling (EcoQoS) runs processes it deems background — a service, or a console
started without a visible window, which is how the app, the worker and the scripts run — on the
efficiency cores at low clocks. Measured 2026-09-29 on the pilot box: reranking 8-10 candidates took
14-16 s per question throttled and 1.7-1.8 s after this opt-out (docs/tuning_log.md). The opt-out is per
process and changes no system setting. No-op on other systems and when Windows refuses.
"""
from __future__ import annotations

import os
from functools import lru_cache


@lru_cache(maxsize=1)
def disable_power_throttling() -> bool:
    """Opt this process out of execution-speed throttling. Returns whether Windows accepted it."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _State(ctypes.Structure):          # PROCESS_POWER_THROTTLING_STATE
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetProcessInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.SetProcessInformation.restype = wintypes.BOOL
        state = _State(1, 0x1, 0x0)             # version 1; control EXECUTION_SPEED; state 0 = not throttled
        return bool(k32.SetProcessInformation(k32.GetCurrentProcess(), 4,   # ProcessPowerThrottling
                                              ctypes.byref(state), ctypes.sizeof(state)))
    except (OSError, AttributeError):
        return False
