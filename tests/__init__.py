"""
Test package for the payer policy crawler.

Present so the test modules form a package and can share helpers via
``from .conftest import make_row``. The suite is entirely offline - every HTTP
interaction is served by an ``httpx.MockTransport``.
"""
