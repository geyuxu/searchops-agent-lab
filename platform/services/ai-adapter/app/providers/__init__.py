"""Concrete providers loaded through the ``AI_PROVIDER=module.path:ClassName`` extension point.

``app.provider`` owns the abstract boundary (``Provider``) and the built-in ``MockProvider``.
Anything shipped here is an *optional* implementation that is never imported unless
``AI_PROVIDER`` names it, so adding a module to this package cannot slow down or break the
default deployment.

Rules for every provider in this package (enforced by ``app.provider.load_provider``, which
``app.main`` calls at import time):

* the constructor must take no arguments and must not perform I/O — a constructor that raises
  keeps the container from starting, and ``docker-compose`` ``depends_on: service_healthy``
  then wedges the whole stack;
* the class must implement ``app.provider.Provider`` (``load_provider`` rejects anything else);
* only the ``app`` package is copied into the image, so submodules here need no Dockerfile
  change and are addressed from the container as ``app.providers.<module>:<Class>``.
"""
