"""Sphinx configuration for the action0-client documentation."""

import sys
from pathlib import Path

# make the package importable even without an installed wheel
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from action0.client import __version__  # noqa: E402

project = "action0-client"
author = "Simon Lachinger"
project_copyright = "2026, Simon Lachinger"
version = __version__
release = __version__

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.doctest",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_autodoc_typehints",
    "sphinx_copybutton",
]

# link references like :py:class:`typing.Protocol` to the Python docs and
# references to the action0.url / action0.req classes to their docs; the
# twisted inventory resolves Deferred and friends
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "action0-url": ("https://laughinjar.github.io/action0-url/", None),
    "action0-req": ("https://laughinjar.github.io/action0-req/", None),
    "twisted": ("https://docs.twisted.org/en/stable/api/", None),
}

# order the API reference like the source files and merge the rich __init__
# docstrings into their class documentation
autodoc_member_order = "bysource"
autoclass_content = "both"

# sphinx-autodoc-typehints: document every parameter type and default value
always_document_param_types = True
typehints_defaults = "comma"

myst_enable_extensions = ["colon_fence"]

html_theme = "furo"
html_title = f"action0-client {__version__}"
