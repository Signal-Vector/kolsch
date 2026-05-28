"""Authorization provider errors — spec §4.9.

Authorization denial itself is NOT an error (it is a structured
`AuthorizationResult`). Only configuration problems in the auth
provider produce errors — both Fatal, both raised at initialization.
"""

from errors.base import PlatformError
from errors.level import Level


class AuthzMalformedRule(PlatformError):
    """A rule entry is malformed (missing fields, wrong shape)."""

    code = "authz.malformed_rule"
    level = Level.FATAL

    def __init__(
        self,
        rule_index: int,
        error: str,
        *,
        where: str = "authz.rule_based.load",
    ) -> None:
        super().__init__(
            message=f"Malformed authorization rule at index {rule_index}: {error}",
            where=where,
            detail={"rule_index": rule_index, "error": error},
        )


class AuthzRegexInvalid(PlatformError):
    """A regex in `who` or `what` failed to compile (TC-AUZ-05)."""

    code = "authz.regex_invalid"
    level = Level.FATAL

    def __init__(
        self,
        rule_index: int,
        field: str,
        regex: str,
        error: str,
        *,
        where: str = "authz.rule_based.compile",
    ) -> None:
        super().__init__(
            message=(
                f"Invalid regex in rule {rule_index} field {field!r}: "
                f"{regex!r} ({error})"
            ),
            where=where,
            detail={
                "rule_index": rule_index,
                "field": field,
                "regex": regex,
                "error": error,
            },
        )
