"""Single source of truth for the SDK version.

Read by ``pyproject.toml`` (hatch dynamic version), exported from
``khai.__init__``, and stamped into the ``User-Agent`` header by the transport
so the backend can see which SDK version is talking to it.
"""

__version__ = "0.1.0"
