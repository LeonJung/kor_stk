"""YAML-driven strategy configuration.

Operators describe a portfolio in a YAML file rather than wiring it in
Python — strategies and their parameters get edited without touching
code, making "swap a strategy in/out" the smallest possible change.

Schema::

    strategies:
      - class: ks_ws.strategies.program_flow.ProgramFlowStrategy
        params:
          confidence_cap_krw: 5_000_000_000
          exit_confidence: 0.7
      - class: my_module.MyCustomStrategy
        params: {window: 5}

    allocator:
      max_position_per_symbol: 100
      weights:
        program_flow: 1.0
        my_custom: 0.5

The loader uses importlib to resolve dotted class paths, so any strategy
class on the import path (project, plugin, user code) is referenceable.
``weights`` keys must match each strategy's ``name`` attribute.

Both top-level keys are optional — a config with only ``strategies``
returns a default Allocator; a config with only ``allocator`` returns
an empty strategy list.
"""

import importlib
from pathlib import Path
from typing import Any

import yaml

from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.base import Strategy


class ConfigError(Exception):
    """Malformed strategy config."""


def _resolve(class_path: str) -> type:
    if "." not in class_path:
        raise ConfigError(f"class path must include module: {class_path!r}")
    module_path, _, class_name = class_path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ConfigError(f"cannot import {module_path!r}: {e}") from e
    try:
        cls = getattr(module, class_name)
    except AttributeError as e:
        raise ConfigError(f"{module_path}.{class_name} not found") from e
    return cls


def build_strategy(spec: dict[str, Any]) -> Strategy:
    """Instantiate a single strategy from a {class, params} spec dict."""
    if "class" not in spec:
        raise ConfigError(f"strategy spec missing 'class': {spec}")
    cls = _resolve(spec["class"])
    if not issubclass(cls, Strategy):
        raise ConfigError(f"{spec['class']} is not a Strategy subclass")
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        raise ConfigError(f"strategy params must be a mapping, got {type(params).__name__}")
    try:
        return cls(**params)
    except TypeError as e:
        raise ConfigError(f"cannot instantiate {spec['class']}: {e}") from e


def build_allocator(spec: dict[str, Any]) -> Allocator:
    """Instantiate Allocator from a config dict (max_position_per_symbol +
    optional weights)."""
    max_pos = spec.get("max_position_per_symbol", 100)
    allocator = Allocator(max_position_per_symbol=int(max_pos))
    weights = spec.get("weights") or {}
    if not isinstance(weights, dict):
        raise ConfigError(f"allocator.weights must be a mapping, got {type(weights).__name__}")
    for name, weight in weights.items():
        allocator.set_weight(str(name), float(weight))
    return allocator


def load_portfolio(
    path: str | Path,
) -> tuple[list[Strategy], Allocator]:
    """Load (strategies, allocator) from a YAML config file."""
    text = Path(path).read_text(encoding="utf-8")
    return load_portfolio_from_str(text)


def load_portfolio_from_str(yaml_text: str) -> tuple[list[Strategy], Allocator]:
    """Same as load_portfolio but from a YAML string — useful for tests."""
    data = yaml.safe_load(yaml_text) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__}")

    strategies: list[Strategy] = []
    for spec in data.get("strategies") or []:
        if not isinstance(spec, dict):
            raise ConfigError(f"each strategy entry must be a mapping, got {spec!r}")
        strategies.append(build_strategy(spec))

    allocator = build_allocator(data.get("allocator") or {})
    return strategies, allocator
