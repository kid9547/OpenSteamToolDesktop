"""
AsyncWorker — 异步任务工作线程

将耗时操作（文件扫描、Steam API 调用）放到后台线程，
通过信号通知 UI 主线程结果，避免界面卡顿。
"""
from __future__ import annotations

import inspect

from PyQt6.QtCore import QThread, pyqtSignal


class AsyncWorker(QThread):
    """异步任务工作线程

    用法：
        worker = AsyncWorker(some_slow_function, arg1, arg2)
        worker.finished_with_result.connect(on_success)
        worker.finished_with_error.connect(on_error)
        worker.start()

    进度上报：若 ``func`` 的签名里声明了 ``progress_cb`` 关键字参数，
    工作线程会自动注入 ``progress_cb=self.progress.emit``，函数在执行
    过程中调用 ``progress_cb(text)`` 即可把进度文本发回主线程::

        def slow_job(progress_cb=None):
            progress_cb and progress_cb("步骤 1/3 …")

    ``progress`` 信号从工作线程发出，Qt 的队列连接保证槽函数在主线程执行。
    """

    finished_with_result = pyqtSignal(object)
    finished_with_error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self._func = func
        self._args = args
        self._kwargs = kwargs
        self._is_cancelled = False

    def run(self):
        """在后台线程中执行"""
        try:
            if self._is_cancelled:
                return
            kwargs = dict(self._kwargs)
            if "progress_cb" not in kwargs:
                try:
                    accepts_cb = "progress_cb" in inspect.signature(self._func).parameters
                except (TypeError, ValueError):  # C 扩展 / 内置函数可能无签名
                    accepts_cb = False
                if accepts_cb:
                    kwargs["progress_cb"] = self.progress.emit
            result = self._func(*self._args, **kwargs)
            if not self._is_cancelled:
                self.finished_with_result.emit(result)
        except Exception as e:
            if not self._is_cancelled:
                self.finished_with_error.emit(str(e))

    def cancel(self):
        """请求取消（设置标记，函数内部可检测 self._is_cancelled）"""
        self._is_cancelled = True

    def __del__(self):
        # 兜底：仅设置取消标志，不调用 wait()（wait 在 __del__ 中可能死锁）
        # 正常清理应由调用方负责：cancel() + wait() + deleteLater()
        try:
            self._is_cancelled = True
        except Exception:
            pass
