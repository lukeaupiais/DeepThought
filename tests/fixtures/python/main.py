from models import Widget
from lib.helpers import helper
from other.helpers import helper as alternate


def annotated(value: Widget) -> Widget:
    return value


def constructed() -> Widget:
    return Widget()


def run() -> int:
    return helper(3) + alternate(3)


def unresolved():
    return missing_function()
