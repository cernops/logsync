"""Prometheus text export for the in-memory metrics registry."""

import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping

from .errors import format_error
from .metrics import MetricSample, metrics
from .signals import critical_section


log = logging.getLogger(__name__)


def prometheus_text(samples: Iterable[MetricSample]) -> str:
    """Render samples in the Prometheus text exposition format."""
    groups: dict[str, tuple[str, str, list[str]]] = {}
    for sample in samples:
        group = groups.setdefault(sample.name, (sample.help, sample.metric_type, []))
        group[2].append(f"{sample.name}{_labels(sample.labels)} {_value(sample.value)}")

    lines: list[str] = []
    for name, (help_text, metric_type, rendered_samples) in groups.items():
        lines.extend(
            (
                f"# HELP {name} {help_text}",
                f"# TYPE {name} {metric_type}",
                *rendered_samples,
            )
        )
    return "\n".join(lines) + ("\n" if lines else "")


class MetricsFilePublisher:
    """Publish snapshots atomically, no more often than a minimum interval."""

    def __init__(
        self,
        path: str,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path: str = path
        self._min_interval: float = min_interval
        self._clock: Callable[[], float] = clock
        self._last_publish: float | None = None
        # Clear the output file immediately to confirm access and erase old metrics
        _atomic_write(self._path, "")

    def publish_if_due(self) -> bool:
        """Publish the current snapshot when the minimum interval has elapsed."""
        now = self._clock()
        if (
            self._last_publish is not None
            and now - self._last_publish < self._min_interval
        ):
            return False
        _atomic_write(self._path, prometheus_text(metrics.samples()))
        self._last_publish = now
        return True


def _atomic_write(path: str, content: str) -> None:
    with critical_section(True):
        fd, temporary = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.",
            dir=os.path.dirname(path) or ".",
        )
        primary_error: BaseException | None = None
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                fd = -1
                _ = stream.write(content)
            os.replace(temporary, path)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError as exc:
                    if primary_error is None:
                        raise OSError(f"failed to close temporary metrics file {temporary}: {exc}") from exc
                    log.error(f"failed to close temporary metrics file {temporary}: {format_error(exc)}")
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if primary_error is None:
                    raise OSError(f"failed to remove temporary metrics file {temporary}: {exc}") from exc
                log.error(f"failed to remove temporary metrics file {temporary}: {format_error(exc)}")


def _labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    values = ",".join(
        f'{key}="{_escape_label(value)}"' for key, value in labels.items()
    )
    return f"{{{values}}}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _value(value: int | float) -> str:
    if isinstance(value, float):
        return format(value, ".12g")
    return str(value)
