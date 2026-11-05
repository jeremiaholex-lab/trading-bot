"""Validation package.

Houses the owner's personal validation parameters (the board's "conditions to
keep going" from the founder review) and, later, the prospective paper-forward
comparison runbook that consumes them.

The founder board voted FUND IF on the trading bot, but only once these personal
inputs are defined and a prospective paper-forward comparison against a named
baseline is run with realistic execution friction. This package is the single
source of truth for those inputs.
"""
