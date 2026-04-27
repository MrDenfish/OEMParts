"""HTMX partials and JSON endpoints.

In Phase 1, HTMX partial responses are handled inline by each route module
(vehicles.py, searches.py, listings.py) by checking the HX-Request header.
This file is reserved for any shared API endpoints that don't fit a
specific page module.
"""
