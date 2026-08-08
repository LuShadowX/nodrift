"""nodrift — prove a refactor changed nothing.

Records the real inputs your test suite already produces, then replays them
against two versions of your code and compares everything observable: return
values, exceptions, and argument mutation.

No model reviews the code. The verdict comes from execution.
"""

__version__ = "0.1.3"

from .compare import compare
from .fingerprint import fingerprint
from .recorder import Recorder

__all__ = ["Recorder", "compare", "fingerprint", "__version__"]
