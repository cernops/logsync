"""Pytest isolation for tests that change the process identity."""

import inspect
import multiprocessing
import signal
import traceback

from collections.abc import Callable, Mapping
from multiprocessing.connection import Connection
from typing import NamedTuple, Protocol, cast

_FORK_CONTEXT = multiprocessing.get_context("fork")


class _PyFuncItem(Protocol):
    @property
    def obj(self) -> Callable[..., object]: ...

    @property
    def funcargs(self) -> Mapping[str, object]: ...


class _ChildResult(NamedTuple):
    succeeded: bool
    traceback: str


def _run_test(test_function: Callable[..., object], test_arguments: dict[str, object], result_writer: Connection) -> None:
    """Run one test and return a formatted exception without relying on the child's identity."""
    try:
        _ = test_function(**test_arguments)
    except BaseException:
        result = _ChildResult(False, traceback.format_exc())
    else:
        result = _ChildResult(True, "")

    try:
        result_writer.send(result)
    finally:
        result_writer.close()


def pytest_pyfunc_call(pyfuncitem: _PyFuncItem) -> bool:
    """Run every Python test below this conftest in an isolated forked process."""
    test_function = pyfuncitem.obj
    argument_names = inspect.signature(test_function).parameters
    test_arguments = {name: pyfuncitem.funcargs[name] for name in argument_names}
    result_reader, result_writer = _FORK_CONTEXT.Pipe(duplex=False)
    process = _FORK_CONTEXT.Process(target=_run_test, args=(test_function, test_arguments, result_writer))
    process.start()
    result_writer.close()

    try:
        try:
            child_result = cast(_ChildResult, result_reader.recv())
        except EOFError:
            child_result = None
    finally:
        result_reader.close()
        process.join()
        if process.is_alive():
            process.terminate()
            process.join()

    exitcode = process.exitcode
    process.close()

    if exitcode is None:
        raise AssertionError("isolated test process did not report an exit status")
    elif exitcode < 0:
        try:
            signal_name = signal.Signals(-exitcode).name
        except ValueError:
            signal_name = f"signal {-exitcode}"
        raise AssertionError(f"isolated test process terminated by {signal_name}")
    elif exitcode != 0:
        raise AssertionError(f"isolated test process exited with status {exitcode}")
    elif child_result is None:
        raise AssertionError("isolated test process did not report a result")
    elif not child_result.succeeded:
        raise AssertionError(child_result.traceback)

    return True
