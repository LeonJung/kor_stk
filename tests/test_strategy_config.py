import pytest

from ks_ws.strategies.allocator import Allocator
from ks_ws.strategies.config import (
    ConfigError,
    build_allocator,
    build_strategy,
    load_portfolio_from_str,
)
from ks_ws.strategies.program_flow import ProgramFlowStrategy


def test_build_strategy_resolves_class_and_params():
    spec = {
        "class": "ks_ws.strategies.program_flow.ProgramFlowStrategy",
        "params": {"confidence_cap_krw": 1_000_000_000, "exit_confidence": 0.5},
    }
    s = build_strategy(spec)
    assert isinstance(s, ProgramFlowStrategy)
    assert s.confidence_cap_krw == 1_000_000_000
    assert s.exit_confidence == 0.5


def test_build_strategy_no_params_uses_defaults():
    spec = {"class": "ks_ws.strategies.program_flow.ProgramFlowStrategy"}
    s = build_strategy(spec)
    assert isinstance(s, ProgramFlowStrategy)


def test_missing_class_raises():
    with pytest.raises(ConfigError, match="missing 'class'"):
        build_strategy({"params": {}})


def test_unimportable_module_raises():
    with pytest.raises(ConfigError, match="cannot import"):
        build_strategy({"class": "no_such_module.SomeClass"})


def test_class_not_in_module_raises():
    with pytest.raises(ConfigError, match="not found"):
        build_strategy({"class": "ks_ws.strategies.program_flow.NoSuchClass"})


def test_class_not_a_strategy_raises():
    with pytest.raises(ConfigError, match="not a Strategy subclass"):
        build_strategy({"class": "ks_ws.strategies.allocator.Allocator"})


def test_invalid_params_type_raises():
    with pytest.raises(ConfigError, match="must be a mapping"):
        build_strategy(
            {
                "class": "ks_ws.strategies.program_flow.ProgramFlowStrategy",
                "params": [1, 2, 3],
            }
        )


def test_constructor_failure_wrapped():
    with pytest.raises(ConfigError, match="cannot instantiate"):
        build_strategy(
            {
                "class": "ks_ws.strategies.program_flow.ProgramFlowStrategy",
                "params": {"unknown_kwarg": 1},
            }
        )


def test_build_allocator_with_weights():
    spec = {
        "max_position_per_symbol": 50,
        "weights": {"alpha": 1.5, "beta": 0.5},
    }
    a = build_allocator(spec)
    assert isinstance(a, Allocator)
    assert a.max_position_per_symbol == 50
    assert a.weight_for("alpha") == 1.5
    assert a.weight_for("beta") == 0.5


def test_build_allocator_default_weights():
    a = build_allocator({})
    assert a.max_position_per_symbol == 100
    assert a.weight_for("anything") == 1.0


def test_build_allocator_invalid_weights_raises():
    with pytest.raises(ConfigError, match="weights"):
        build_allocator({"weights": "not-a-mapping"})


def test_load_portfolio_from_string_full():
    yaml_text = """
strategies:
  - class: ks_ws.strategies.program_flow.ProgramFlowStrategy
    params:
      confidence_cap_krw: 2000000000
      exit_confidence: 0.6

allocator:
  max_position_per_symbol: 30
  weights:
    program_flow: 0.8
"""
    strategies, allocator = load_portfolio_from_str(yaml_text)
    assert len(strategies) == 1
    assert isinstance(strategies[0], ProgramFlowStrategy)
    assert strategies[0].confidence_cap_krw == 2_000_000_000
    assert allocator.max_position_per_symbol == 30
    assert allocator.weight_for("program_flow") == 0.8


def test_load_portfolio_empty_returns_defaults():
    strategies, allocator = load_portfolio_from_str("")
    assert strategies == []
    assert allocator.max_position_per_symbol == 100


def test_load_portfolio_strategies_only():
    yaml_text = """
strategies:
  - class: ks_ws.strategies.program_flow.ProgramFlowStrategy
"""
    strategies, allocator = load_portfolio_from_str(yaml_text)
    assert len(strategies) == 1
    assert allocator.max_position_per_symbol == 100  # default


def test_load_portfolio_allocator_only():
    yaml_text = """
allocator:
  max_position_per_symbol: 7
"""
    strategies, allocator = load_portfolio_from_str(yaml_text)
    assert strategies == []
    assert allocator.max_position_per_symbol == 7


def test_load_portfolio_root_must_be_mapping():
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_portfolio_from_str("- not a mapping")


def test_load_portfolio_strategy_entry_must_be_mapping():
    yaml_text = """
strategies:
  - just_a_string
"""
    with pytest.raises(ConfigError):
        load_portfolio_from_str(yaml_text)


def test_load_portfolio_from_file(tmp_path):
    from ks_ws.strategies.config import load_portfolio

    config_path = tmp_path / "portfolio.yaml"
    config_path.write_text(
        """
strategies:
  - class: ks_ws.strategies.program_flow.ProgramFlowStrategy
allocator:
  max_position_per_symbol: 10
""",
        encoding="utf-8",
    )
    strategies, allocator = load_portfolio(config_path)
    assert len(strategies) == 1
    assert allocator.max_position_per_symbol == 10
