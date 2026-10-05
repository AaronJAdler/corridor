"""Identifiers.

Every primary key is a UUIDv7 generated in the application: it sorts by creation time, so
B-tree inserts stay local, and it does not reveal a count the way a serial id does.
"""

import uuid


def new_id() -> uuid.UUID:
    return uuid.uuid7()
