"""进程内轻量指标（Prometheus text 导出）。"""
from __future__ import annotations

import threading
from collections import defaultdict


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._labeled: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._labeled.clear()

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            if labels:
                key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
                self._labeled[key] += value
            else:
                self._counters[name] += value

    def get(self, name: str, **labels: str) -> float:
        with self._lock:
            if labels:
                key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
                return float(self._labeled.get(key, 0.0))
            return float(self._counters.get(name, 0.0))

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, value in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name} {value}")
            by_name: dict[str, list[tuple[tuple[tuple[str, str], ...], float]]] = defaultdict(list)
            for (name, label_items), value in self._labeled.items():
                by_name[name].append((label_items, value))
            for name, entries in sorted(by_name.items()):
                lines.append(f"# TYPE {name} counter")
                for label_items, value in sorted(entries):
                    label_str = ",".join(f'{k}="{v}"' for k, v in label_items)
                    lines.append(f"{name}{{{label_str}}} {value}")
        return "\n".join(lines) + ("\n" if lines else "")


_registry = MetricsRegistry()


def get_metrics() -> MetricsRegistry:
    return _registry


def inc_metric(name: str, value: float = 1.0, **labels: str) -> None:
    _registry.inc(name, value, **labels)
