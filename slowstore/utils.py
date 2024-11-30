import datetime
from functools import wraps
import slowstore

def json_default_serializer(o):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()

def ensure_loaded(func):
    @wraps(func)
    def wrapper(self: "slowstore.Store", *args, **kwargs):
        if not self.loaded:
            self.load()
        return func(self, *args, **kwargs)

    return wrapper
