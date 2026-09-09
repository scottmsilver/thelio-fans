"""Synthetic load for the stress test: every CPU core busy, and a GPU compute kernel run
through the OpenCL library that ships with the NVIDIA driver, so nothing new is installed."""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import threading
import time
from typing import Any, Protocol

from fanwatch.text import sanitize


class Load(Protocol):
    """Something that heats a component until told to stop."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def failure(self) -> str | None: ...


def _spin(stop: Any) -> None:
    a = 1.0
    while not stop.is_set():
        for _ in range(100_000):
            a = a * 1.0000001 + 0.1
        if a > 1e300:
            a = 1.0


class CpuLoad:
    """One busy process per worker, checking a shared stop flag every 100k iterations."""

    def __init__(self, workers: int | None = None) -> None:
        self.workers = workers or os.cpu_count() or 1
        self._stop = multiprocessing.Event()
        self._procs: list[multiprocessing.Process] = []

    def start(self) -> None:
        self.stop()
        self._stop.clear()
        try:
            for _ in range(self.workers):
                p = multiprocessing.Process(target=_spin, args=(self._stop,), daemon=True)
                p.start()
                self._procs.append(p)  # only processes that actually started are tracked
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        self._stop.set()
        for p in self._procs:
            p.join(timeout=5)
            if p.is_alive():
                p.kill()
                p.join(timeout=5)
        self._procs = []

    def failure(self) -> str | None:
        """A worker that died on its own, or None."""
        dead = [p for p in self._procs if not p.is_alive()]
        return f"{len(dead)} CPU load worker(s) exited early" if dead else None


CL_DEVICE_TYPE_GPU = 1 << 2
CL_DEVICE_NAME = 0x102B
CL_MEM_READ_WRITE = 1
KERNEL = b"""
__kernel void burn(__global float* out, const int iters) {
    const int i = get_global_id(0);
    float a = (float)i * 1e-6f, b = 1.0001f, c = 0.9999f;
    for (int k = 0; k < iters; k++) {
        a = fma(a, b, c);
        b = fma(b, c, a);
        c = fma(c, a, b);
    }
    out[i] = a + b + c;
}
"""
WORK_ITEMS = 1 << 20
CALIBRATE_ITERATIONS = 200  # a first, short launch, timed to size the real ones
TARGET_LAUNCH_S = 0.05  # keep each kernel short so the desktop and a stop stay responsive
MIN_ITERATIONS, MAX_ITERATIONS = 100, 100_000

_P = ctypes.c_void_p
_PP = ctypes.POINTER(ctypes.c_void_p)
_U = ctypes.c_uint
_UP = ctypes.POINTER(ctypes.c_uint)
_I = ctypes.c_int
_IP = ctypes.POINTER(ctypes.c_int)
_SZ = ctypes.c_size_t
_SZP = ctypes.POINTER(ctypes.c_size_t)
_U64 = ctypes.c_ulonglong
# Every function this module calls, with its full 64-bit-safe prototype.
PROTOTYPES: dict[str, tuple[Any, list[Any]]] = {
    "clGetPlatformIDs": (_I, [_U, _PP, _UP]),
    "clGetDeviceIDs": (_I, [_P, _U64, _U, _PP, _UP]),
    "clGetDeviceInfo": (_I, [_P, _U, _SZ, ctypes.c_void_p, _SZP]),
    "clCreateContext": (_P, [_P, _U, _PP, _P, _P, _IP]),
    "clCreateCommandQueue": (_P, [_P, _P, _U64, _IP]),
    "clCreateProgramWithSource": (_P, [_P, _U, ctypes.POINTER(ctypes.c_char_p), _SZP, _IP]),
    "clBuildProgram": (_I, [_P, _U, _PP, ctypes.c_char_p, _P, _P]),
    "clCreateKernel": (_P, [_P, ctypes.c_char_p, _IP]),
    "clCreateBuffer": (_P, [_P, _U64, _SZ, _P, _IP]),
    "clSetKernelArg": (_I, [_P, _U, _SZ, ctypes.c_void_p]),
    "clEnqueueNDRangeKernel": (_I, [_P, _P, _U, _SZP, _SZP, _SZP, _U, _P, _P]),
    "clFinish": (_I, [_P]),
    "clReleaseMemObject": (_I, [_P]),
    "clReleaseKernel": (_I, [_P]),
    "clReleaseProgram": (_I, [_P]),
    "clReleaseCommandQueue": (_I, [_P]),
    "clReleaseContext": (_I, [_P]),
}


class OpenClError(RuntimeError):
    """OpenCL is missing, has no GPU, or refused a call."""


class OpenClLoad:
    """Repeatedly runs a fused-multiply-add kernel on the first OpenCL GPU from a thread.
    Foreign calls release the GIL, so the sampler in the main thread keeps running."""

    def __init__(self, lib: Any | None = None) -> None:
        self.cl = lib if lib is not None else _open_library()
        self.device_name = "GPU"
        self.iterations = CALIBRATE_ITERATIONS
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: str | None = None
        self.context: int | None = None
        self.queue: int | None = None
        self.program: int | None = None
        self.kernel: int | None = None
        self.buffer: int | None = None
        try:
            self._setup()
        except BaseException:
            self.release()
            raise

    def _check(self, err: int, what: str) -> None:
        if err != 0:
            raise OpenClError(f"{what} failed with OpenCL error {err}")

    def _created(self, handle: int | None, what: str, err: ctypes.c_int) -> int:
        self._check(err.value, what)
        if not handle:
            raise OpenClError(f"{what} returned a null handle")
        return handle

    def _setup(self) -> None:
        cl = self.cl
        platform, count = ctypes.c_void_p(), ctypes.c_uint()
        self._check(cl.clGetPlatformIDs(1, ctypes.byref(platform), ctypes.byref(count)), "platform")
        if count.value == 0 or not platform.value:
            raise OpenClError("no OpenCL platform")
        device = ctypes.c_void_p()
        self._check(
            cl.clGetDeviceIDs(platform, CL_DEVICE_TYPE_GPU, 1, ctypes.byref(device), None),
            "GPU device",
        )
        if not device.value:
            raise OpenClError("no OpenCL GPU")
        name = ctypes.create_string_buffer(256)
        if cl.clGetDeviceInfo(device, CL_DEVICE_NAME, 256, name, None) == 0:
            self.device_name = sanitize(name.value.decode("utf-8", "replace"))
        err = ctypes.c_int()
        self.context = self._created(
            cl.clCreateContext(None, 1, ctypes.byref(device), None, None, ctypes.byref(err)),
            "context",
            err,
        )
        self.queue = self._created(
            cl.clCreateCommandQueue(self.context, device, 0, ctypes.byref(err)), "queue", err
        )
        source = ctypes.c_char_p(KERNEL)
        self.program = self._created(
            cl.clCreateProgramWithSource(
                self.context, 1, ctypes.byref(source), None, ctypes.byref(err)
            ),
            "program",
            err,
        )
        self._check(
            cl.clBuildProgram(self.program, 1, ctypes.byref(device), b"", None, None), "build"
        )
        self.kernel = self._created(
            cl.clCreateKernel(self.program, b"burn", ctypes.byref(err)), "kernel", err
        )
        self.buffer = self._created(
            cl.clCreateBuffer(
                self.context, CL_MEM_READ_WRITE, WORK_ITEMS * 4, None, ctypes.byref(err)
            ),
            "buffer",
            err,
        )
        buf = ctypes.c_void_p(self.buffer)
        self._check(
            cl.clSetKernelArg(self.kernel, 0, ctypes.sizeof(buf), ctypes.byref(buf)), "arg 0"
        )
        self._global = (ctypes.c_size_t * 1)(WORK_ITEMS)
        self._set_iterations(CALIBRATE_ITERATIONS)
        # Fail here, in the caller's thread, rather than silently in the loop; and size the
        # real launches from a short timed one so each stays around TARGET_LAUNCH_S.
        started = time.monotonic()
        self.launch()
        took = max(1e-4, time.monotonic() - started)
        scaled = int(CALIBRATE_ITERATIONS * TARGET_LAUNCH_S / took)
        self._set_iterations(max(MIN_ITERATIONS, min(MAX_ITERATIONS, scaled)))

    def _set_iterations(self, iterations: int) -> None:
        self.iterations = iterations
        arg = ctypes.c_int(iterations)
        self._check(self.cl.clSetKernelArg(self.kernel, 1, 4, ctypes.byref(arg)), "arg 1")

    def launch(self) -> None:
        """One kernel run, waited for."""
        cl = self.cl
        self._check(
            cl.clEnqueueNDRangeKernel(
                self.queue, self.kernel, 1, None, self._global, None, 0, None, None
            ),
            "launch",
        )
        self._check(cl.clFinish(self.queue), "finish")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.launch()
            except OpenClError as exc:
                self._error = str(exc)
                return

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise OpenClError("GPU load is still running from a previous phase")
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            if not self._thread.is_alive():
                self._thread = None

    def failure(self) -> str | None:
        """Why the GPU loop stopped on its own, or None while it runs."""
        return self._error

    def release(self) -> None:
        """Free every OpenCL object; safe to call twice or after a partial setup."""
        self.stop()
        cl = self.cl
        for attr, fn in (
            ("buffer", "clReleaseMemObject"),
            ("kernel", "clReleaseKernel"),
            ("program", "clReleaseProgram"),
            ("queue", "clReleaseCommandQueue"),
            ("context", "clReleaseContext"),
        ):
            handle = getattr(self, attr)
            if handle:
                getattr(cl, fn)(handle)
                setattr(self, attr, None)


def _open_library() -> Any:
    for name in ("libOpenCL.so.1", "libOpenCL.so"):
        try:
            cl = ctypes.CDLL(name)
        except OSError:
            continue
        for fn, (restype, argtypes) in PROTOTYPES.items():
            f = getattr(cl, fn)
            f.restype, f.argtypes = restype, argtypes
        return cl
    raise OpenClError("libOpenCL.so not found; is the NVIDIA driver installed?")


def gpu_load() -> tuple[OpenClLoad | None, str]:
    """The GPU load and a description, or None and the reason it is unavailable. The
    first OpenCL GPU is assumed to be the GPU that NVML reports as device 0."""
    try:
        load = OpenClLoad()
    except OpenClError as exc:
        return None, str(exc)
    return load, f"OpenCL on {load.device_name}, {load.iterations} iterations per launch"
