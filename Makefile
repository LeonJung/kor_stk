.PHONY: test lint fmt fmt-check check

# ROS2 (sourced into the user's shell via PYTHONPATH) leaks its launch_testing
# and ament_* pytest plugins into our venv and they fail to import here. This
# disables entry-point plugin autoloading so we run with pytest builtins only.
export PYTEST_DISABLE_PLUGIN_AUTOLOAD = 1

test:
	uv run pytest

lint:
	uv run ruff check src/ tests/

fmt:
	uv run ruff format src/ tests/

fmt-check:
	uv run ruff format --check src/ tests/

check: lint fmt-check test
