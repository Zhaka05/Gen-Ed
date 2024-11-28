# SPDX-FileCopyrightText: 2023 Mark Liffiton <liffiton@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-only

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from sqlite3 import Row
from typing import Literal, ParamSpec, TypeVar

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash
from werkzeug.wrappers.response import Response

from .db import get_db
from .redir import safe_redirect_next

# Constants
AUTH_SESSION_KEY = "__gened_auth"

ProviderType = Literal['local', 'lti', 'demo', 'google', 'github', 'microsoft']
RoleType = Literal['instructor', 'student']

@dataclass(frozen=True)
class ClassData:
    class_id: int
    class_name: str
    role: RoleType

@dataclass(frozen=True)
class AuthData:
    user_id: int | None = None
    is_admin: bool = False
    is_tester: bool = False
    auth_provider: ProviderType | None = None
    display_name: str | None = None
    class_id: int | None = None    # current class ID
    class_name: str | None = None  # current class name
    role_id: int | None = None     # current role
    role: RoleType | None = None   # current role name (e.g., 'instructor')
    class_experiments: list[str] = field(default_factory=list)  # any experiments the current class is registered in
    other_classes: list[ClassData] = field(default_factory=list)  # for storing active classes that are not the user's current class


def _invalidate_g_auth() -> None:
    """ Ensure no auth data is cached in the g object.
        Use after modifying auth data stored in the session,
        so g.auth will be regenerated on next access in get_auth().
    """
    g.pop('auth', None)


def set_session_auth_user(user_id: int) -> None:
    """ Set the current session's user (on login, after authentication).
        Clears all other auth data in the session.
    """
    auth = {
        'user_id': user_id,
    }
    session[AUTH_SESSION_KEY] = auth
    _invalidate_g_auth()


def set_session_auth_class(class_id: int | None) -> None:
    """ Set the current session's active class (on login or class switch).
        Adds to any existing auth data in the session.
    """
    sess_auth = session.get(AUTH_SESSION_KEY, {})
    assert 'user_id' in sess_auth
    assert sess_auth['user_id'] is not None  # must be logged in already for this function to be valid
    sess_auth['class_id'] = class_id
    session[AUTH_SESSION_KEY] = sess_auth
    _invalidate_g_auth()


def _get_auth_from_session() -> AuthData:
    """ Populate auth data for the current session based on its current
        user_id and class_id (if any).
    """
    # Get the session auth dict, or an empty dict if it's not there, to find
    # current user_id (if any).
    sess_auth = session.get(AUTH_SESSION_KEY, {})
    user_id = sess_auth.get('user_id', None)
    class_id = sess_auth.get('class_id', None)

    if not user_id:
        # No logged in user; return the default/empty auth data
        return AuthData()

    db = get_db()

    # Get user's data
    user_row = db.execute("""
        SELECT
            users.display_name,
            users.is_admin,
            users.is_tester,
            auth_providers.name AS auth_provider
        FROM users
        LEFT JOIN auth_providers ON auth_providers.id=users.auth_provider
        WHERE users.id=?
    """, [user_id]).fetchone()

    if not user_row:
        # Fall through if user_id is not in database (deleted from DB?)
        return AuthData()

    # Collect auth data values
    display_name=user_row['display_name']
    is_admin=user_row['is_admin']
    is_tester=user_row['is_tester']
    auth_provider=user_row['auth_provider']

    # Check the database for any active roles (may be changed by another user)
    # and populate class/role information.
    # Uses WHERE active=1 to only allow active roles.
    role_rows = db.execute("""
        SELECT
            roles.id AS role_id,
            roles.class_id,
            roles.role,
            classes.name,
            classes.enabled
        FROM roles
        JOIN classes ON classes.id=roles.class_id
        WHERE roles.user_id=? AND roles.active=1
        ORDER BY roles.id DESC
    """, [user_id]).fetchall()

    role_id = None
    role = None
    class_experiments = []
    other_classes = []
    class_name = None

    for row in role_rows:
        if row['class_id'] == class_id:
            # capture class/role info
            role_id = row['role_id']
            role = row['role']
            # check for any registered experiments in the current class
            experiment_class_rows = db.execute("SELECT experiments.name FROM experiments JOIN experiment_class ON experiment_class.experiment_id=experiments.id WHERE experiment_class.class_id=?", [class_id]).fetchall()
            class_experiments = [row['name'] for row in experiment_class_rows]
        elif row['enabled']:
            # store a list of any other classes that are enabled (for navbar switching UI)
            class_data = ClassData(
                class_id=row['class_id'],
                class_name=row['name'],
                role=row['role']
            )
            other_classes.append(class_data)

    if not role_id and not is_admin:
        # ensure we don't keep a class_id in auth if it's not a valid/active one
        class_id = None

    if class_id is not None:
        # get the class name (after all the above has shaken out)
        class_row = db.execute("SELECT name FROM classes WHERE id=?", [class_id]).fetchone()
        class_name = class_row['name']
        # admin gets instructor role in all classes automatically
        if is_admin:
            role = 'instructor'

    # return an AuthData with all collected values
    return AuthData(
        user_id=user_id,
        is_admin=is_admin,
        is_tester=is_tester,
        auth_provider=auth_provider,
        display_name=display_name,
        class_id=class_id,
        class_name=class_name,
        role_id=role_id,
        role=role,
        class_experiments=class_experiments,
        other_classes=other_classes
    )


def get_auth() -> AuthData:
    if 'auth' not in g:
        g.auth = _get_auth_from_session()

    return g.auth  # type: ignore[no-any-return]


def get_last_class(user_id: int) -> int | None:
    """ Find and return the last class (as a class ID) for the given user,
        as long as the user still has an active role in that class.

        Returns the class_id or None if nothing is found / matches.
    """
    db = get_db()

    class_row = db.execute("""
        SELECT users.last_class_id AS class_id
        FROM users
        JOIN roles ON roles.user_id=users.id
        WHERE users.id=?
          AND roles.class_id=users.last_class_id
          AND roles.active=1
    """, [user_id]).fetchone()

    if not class_row:
        return None

    class_id = class_row['class_id']
    assert isinstance(class_id, int)
    return class_id


def ext_login_update_or_create(provider_name: str, user_normed: dict[str, str | None], query_tokens: int=0) -> Row:
    """
    For an external authentication login:
      1. Create an account for the user if they do not already have an account (entry in users)
      2. Update the account with user info provided if one does already exist
      3. Get and return the account info for that user

    Parameters
    ----------
    provider_name : str
      Name of the external auth provider: in set {lti, google, github, microsoft}
    user_normed : dict
      User information.
      Must contain non-null 'ext_id' key; must contain keys 'email', 'full_name', and 'auth_name', and at least one should be non-null.
    query_tokens : int (default 0)
      Number of query tokens to assign to the user *if* creating an account for them (on first login).

    Returns
    -------
    SQLite row object containing the 'users' table row for the now-logged-in user.
    """
    db = get_db()

    provider_row = db.execute("SELECT id FROM auth_providers WHERE name=?", [provider_name]).fetchone()
    provider_id = provider_row['id']

    auth_row = db.execute("SELECT * FROM auth_external WHERE auth_provider=? AND ext_id=?", [provider_id, user_normed['ext_id']]).fetchone()

    if auth_row:
        user_id = auth_row['user_id']
        # Update w/ latest user info (name, email, etc. could conceivably change)
        cur = db.execute(
            "UPDATE users SET full_name=?, email=?, auth_name=? WHERE id=?",
            [user_normed['full_name'], user_normed['email'], user_normed['auth_name'], user_id]
        )
        db.commit()

    else:
        # Create a new user account.
        cur = db.execute(
            "INSERT INTO users (auth_provider, full_name, email, auth_name, query_tokens) VALUES (?, ?, ?, ?, ?)",
            [provider_id, user_normed['full_name'], user_normed['email'], user_normed['auth_name'], query_tokens]
        )
        user_id = cur.lastrowid
        db.execute("INSERT INTO auth_external(user_id, auth_provider, ext_id) VALUES (?, ?, ?)", [user_id, provider_id, user_normed['ext_id']])
        db.commit()

        current_app.logger.info(f"New acct: '{user_normed['full_name']}' {user_normed['email']}({provider_name})")

    # get all values in newly updated/inserted row
    user_row = db.execute("SELECT * FROM users WHERE id=?", [user_id]).fetchone()
    assert isinstance(user_row, Row)
    return user_row


bp = Blueprint('auth', __name__, url_prefix="/auth", template_folder='templates')


@bp.route("/login", methods=['GET', 'POST'])
def login() -> str | Response:
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        auth_row = db.execute("SELECT * FROM auth_local JOIN users ON auth_local.user_id=users.id WHERE username=?", [username]).fetchone()

        if not auth_row or not check_password_hash(auth_row['password'], password):
            flash("Invalid username or password.", "warning")
        else:
            # Success!
            last_class_id = get_last_class(auth_row['id'])
            set_session_auth_user(auth_row['id'])
            set_session_auth_class(last_class_id)
            return safe_redirect_next(default_endpoint="helper.help_form")

    # we either have a GET request or we fell through the POST login attempt with a failure
    next_url = request.args.get('next', '')
    return render_template("login.html", next_url=next_url)


@bp.route("/logout", methods=['POST'])
def logout() -> Response:
    session.clear()  # clear the entire session to be safest here.
    flash("You have been logged out.")
    return redirect(url_for(".login"))


# For decorator type hints
P = ParamSpec('P')
R = TypeVar('R')


def login_required(f: Callable[P, R]) -> Callable[P, Response | R]:
    '''Redirect to login on this route if user is not logged in.'''
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> Response | R:
        auth = get_auth()
        if not auth.user_id:
            flash("Login required.", "warning")
            return redirect(url_for('auth.login', next=request.full_path))
        return f(*args, **kwargs)
    return decorated_function


def instructor_required(f: Callable[P, R]) -> Callable[P, Response | R]:
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> Response | R:
        auth = get_auth()
        if auth.role != "instructor":
            flash("Instructor login required.", "warning")
            return redirect(url_for('auth.login', next=request.full_path))
        return f(*args, **kwargs)
    return decorated_function


def class_enabled_required(f: Callable[P, R]) -> Callable[P, str | R]:
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> str | R:
        auth = get_auth()
        class_id = auth.class_id

        if class_id is None:
            # No active class, no problem
            return f(*args, **kwargs)

        # Otherwise, there's an active class, so we require it to be enabled.
        db = get_db()
        class_row = db.execute("SELECT * FROM classes WHERE id=?", [class_id]).fetchone()
        if not class_row['enabled']:
            flash("The current class is archived or disabled.  New requests cannot be made.", "warning")
            return render_template("error.html")

        return f(*args, **kwargs)

    return decorated_function


def admin_required(f: Callable[P, R]) -> Callable[P, Response | R]:
    '''Redirect to login on this route if user is not an admin.'''
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> Response | R:
        auth = get_auth()
        if not auth.is_admin:
            flash("Login required.", "warning")
            return redirect(url_for('auth.login', next=request.full_path))
        return f(*args, **kwargs)
    return decorated_function


def tester_required(f: Callable[P, R]) -> Callable[P, Response | R]:
    '''Return a 404 on this route (hide it, basically) if user is not a tester.'''
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> Response | R:
        auth = get_auth()
        if not auth.is_tester:
            return abort(404)
        return f(*args, **kwargs)
    return decorated_function
