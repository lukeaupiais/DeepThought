"""Django's own column names for model fields.

Reads [[field class, name, db_column or null], ...] as JSON on stdin and
writes {"<request as JSON>": column} on stdout. Each field is Django's own
class (django.db.models.<class>, else django.db.models.Field for a field
class Django does not define), named with ``set_attributes_from_name`` and
asked ``get_attname_column()``, as Django does when a model class is built.
Only Django is imported; nothing of the analyzed project is.
"""

import json
import sys

from django.conf import settings

settings.configure()

from django.db import models  # noqa: E402

found = {}
for request in json.load(sys.stdin):
    kind, name, db_column = request
    field_class = getattr(models, kind, None)
    if not (isinstance(field_class, type) and issubclass(field_class, models.Field)):
        field_class = models.Field
    options = {} if db_column is None else {"db_column": db_column}
    try:
        if issubclass(field_class, models.ForeignObject):
            if field_class is models.ForeignObject:
                continue
            field = field_class("self", on_delete=models.CASCADE, **options)
        else:
            field = field_class(**options)
        field.set_attributes_from_name(name)
        found[json.dumps(request, separators=(",", ":"))] = field.get_attname_column()[
            1
        ]
    except Exception:  # a field class needing more arguments: left unnamed
        continue
json.dump(found, sys.stdout)
