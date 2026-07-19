"""The exception spine: every public dud failure descends from DudError.

Concrete errors keep their historical bases too (SessionLost is still
a RuntimeError, NotRepresentable still a ValueError), so existing
``except`` clauses keep working — DudError is an additional handle
(``except dud.DudError``), not a migration.
"""


class DudError(Exception):
    """Base for all public dud errors."""
