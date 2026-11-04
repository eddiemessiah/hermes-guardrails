import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hg_core", ROOT / "core" / "__init__.py",
                                              submodule_search_locations=[str(ROOT / "core")])
mod = importlib.util.module_from_spec(spec)
sys.modules["hg_core"] = mod
spec.loader.exec_module(mod)
