"""Schema definitions.

Importing this package triggers registration of every schema with the
schema_registry. Individual schema modules call register_schema() at module
load; this package's __init__ guarantees all modules are imported when a
caller does `from skills_kit_lib import schemas`.
"""

from . import portable  # noqa: F401
from . import skill_types  # noqa: F401
from . import claude_md  # noqa: F401
from . import standards  # noqa: F401
