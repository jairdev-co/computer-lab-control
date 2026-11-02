# Import libraries
from functools import wraps
from flask import redirect, session


# Redirect to login when there is no active session
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect("/login?expirada=1")
        return f(*args, **kwargs)

    return decorated_function
