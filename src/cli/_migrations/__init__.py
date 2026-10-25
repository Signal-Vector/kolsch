"""Per-artifact migrators run at `kolsch setup` re-run time.

The package is the home for in-place schema migrations. Each module
exports a pure function that takes the pre-fix shape and returns the
post-fix shape plus any Recoverable events the caller should log.
The runner stays in `cli._setup` so the migration package never
touches stdio.
"""

from cli._migrations.authz_rules import (
    DeprecatedRegexRule,
    MigrationResult,
    migrate_authz_rules,
)
from cli._migrations.default_rules import (
    DefaultRulesPlan,
    apply_default_rule_additions,
    plan_default_rule_additions,
)

__all__ = [
    "DefaultRulesPlan",
    "DeprecatedRegexRule",
    "MigrationResult",
    "apply_default_rule_additions",
    "migrate_authz_rules",
    "plan_default_rule_additions",
]
