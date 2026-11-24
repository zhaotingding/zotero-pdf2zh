"""Run one PDF job in its own process and report progress to the HTTP server."""

import multiprocessing
import threading
import traceback


def _entry(pipe, function, arguments):
    lock = threading.Lock()

    def emit(kind, data):
        with lock:
            pipe.send((kind, data))

    try:
        value = function(*arguments, progress=lambda data: emit("progress", data))
        emit("result", value)
    except Exception as exc:  # noqa: BLE001 -- propagate Python errors across the process boundary
        traceback.print_exc()
        emit("error", str(exc))
    finally:
        pipe.close()


def run_isolated(function, arguments, on_progress):
    # spawn 避免继承 HTTP 线程中不安全的 PDF 原生库状态。凭据只经本机进程管道传递。
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_entry, args=(writer, function, arguments))
    try:
        process.start()
    except BaseException:
        reader.close()
        raise
    finally:
        writer.close()
    result = error = None
    try:
        while True:
            try:
                kind, data = reader.recv()
            except EOFError:
                break
            if kind == "progress":
                on_progress(data)
            elif kind == "result":
                result = data
            elif kind == "error":
                error = data
    finally:
        reader.close()
        process.join()
        exitcode = process.exitcode
        process.close()
    if exitcode:
        raise RuntimeError(
            f"PDF 工作进程异常退出（0x{exitcode & 0xFFFFFFFF:08X}）。本次任务已停止，服务可继续接收任务。"
        )
    if error is not None:
        raise RuntimeError(error)
    if result is None:
        raise RuntimeError("PDF 工作进程没有返回结果。")
    return result
