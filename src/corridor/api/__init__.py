"""The HTTP API: app factory, routers, dependencies and error rendering.

A composition root. Handlers here own the database transaction for a request and pass the
session down to the modules that do the work.
"""
