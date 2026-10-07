def isdeleted(obj):
    return bool(getattr(obj, "_deleted", False))
