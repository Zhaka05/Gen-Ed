from collections import namedtuple

from flask import Blueprint, current_app, redirect, render_template, request, send_file, url_for

from .db import get_db
from .auth import admin_required


bp = Blueprint('admin', __name__, url_prefix="/admin", template_folder='templates')


@bp.before_request
@admin_required
def before_request():
    """ Protect all of the admin endpoints. """
    pass


# A module-level list of registered admin pages.  Updated by register_admin_page()
_admin_pages = []


# Decorator function for registering routes as admin pages.
# Use:
#   @register_admin_page("Demo Links")
#   @[route stuff]
#   def handler():  [...]
def register_admin_page(display_name):
    def decorator(route_func):
        handler_name = f"admin.{route_func.__name__}"
        _admin_pages.append((handler_name, display_name))
        return route_func
    return decorator


def init_app(app):
    # inject admin pages into template contexts
    @app.context_processor
    def inject_admin_pages():
        return dict(admin_pages=_admin_pages)


def reload_consumers():
    db = get_db()
    consumer_rows = db.execute("SELECT * FROM consumers").fetchall()
    consumer_dict = {
        row['lti_consumer']: {"secret": row['lti_secret']} for row in consumer_rows
    }
    current_app.config['PYLTI_CONFIG']['consumers'] = consumer_dict


Filter = namedtuple('Filter', ('name', 'column', 'value'))


class Filters:
    def __init__(self):
        self._filters = []

    def __iter__(self):
        return self._filters.__iter__()

    def add_where(self, name, column, value):
        self._filters.append(Filter(name, column, value))

    def make_where(self):
        if not self._filters:
            return "", []
        else:
            return (
                "WHERE " + " AND ".join(f"{f.column}=?" for f in self._filters),
                [f.value for f in self._filters]
            )

    def dict_minus(self, item_name):
        return {name: value for name, column, value in self._filters if name != item_name}

    def dict(self):
        return {name: value for name, column, value in self._filters}


@bp.route("/")
def main():
    db = get_db()
    filters = Filters()

    consumers = db.execute("""
        SELECT
            consumers.*,
            COUNT(queries.id) AS num_queries,
            SUM(CASE WHEN queries.query_time > date('now', '-7 days') THEN 1 ELSE 0 END) AS num_recent_queries
        FROM consumers
        LEFT JOIN classes ON classes.lti_consumer=consumers.lti_consumer
        LEFT JOIN roles ON roles.class_id=classes.id
        LEFT JOIN queries ON queries.role_id=roles.id
        GROUP BY consumers.id
    """).fetchall()

    if 'consumer' in request.args:
        filters.add_where("consumer", "consumers.id", request.args['consumer'])

    where_clause, where_params = filters.make_where()
    classes = db.execute(f"""
        SELECT
            classes.*,
            COUNT(queries.id) AS num_queries,
            SUM(CASE WHEN queries.query_time > date('now', '-7 days') THEN 1 ELSE 0 END) AS num_recent_queries
        FROM classes
        LEFT JOIN consumers ON consumers.lti_consumer=classes.lti_consumer
        LEFT JOIN roles ON roles.class_id=classes.id
        LEFT JOIN queries ON queries.role_id=roles.id
        {where_clause}
        GROUP BY classes.id
    """, where_params).fetchall()

    if 'class' in request.args:
        filters.add_where("class", "roles.class_id", request.args['class'])

    where_clause, where_params = filters.make_where()
    users = db.execute(f"""
        SELECT
            users.*,
            COUNT(queries.id) AS num_queries,
            SUM(CASE WHEN queries.query_time > date('now', '-7 days') THEN 1 ELSE 0 END) AS num_recent_queries
        FROM users
        LEFT JOIN roles ON roles.user_id=users.id
        LEFT JOIN consumers ON consumers.lti_consumer=users.lti_consumer
        LEFT JOIN queries ON queries.user_id=users.id
        {where_clause}
        GROUP BY users.id
    """, where_params).fetchall()

    if 'user' in request.args:
        filters.add_where("user", "users.username", request.args['user'])

    where_clause, where_params = filters.make_where()
    roles = db.execute(f"SELECT roles.*, users.username, COUNT(queries.id) AS num_queries FROM roles LEFT JOIN users ON users.id=roles.user_id LEFT JOIN consumers ON users.lti_consumer=consumers.lti_consumer LEFT JOIN queries ON roles.id=queries.role_id {where_clause} GROUP BY roles.id", where_params).fetchall()

    if 'role' in request.args:
        filters.add_where("role", "roles.id", request.args['role'])

    where_clause, where_params = filters.make_where()
    queries_limit = 200
    queries = db.execute(f"SELECT queries.*, users.username FROM queries JOIN users ON queries.user_id=users.id LEFT JOIN consumers ON users.lti_consumer=consumers.lti_consumer LEFT JOIN roles ON queries.role_id=roles.id {where_clause} ORDER BY query_time DESC LIMIT ?", where_params + [queries_limit]).fetchall()

    return render_template("admin.html", consumers=consumers, classes=classes, users=users, roles=roles, queries=queries, filters=filters)


@register_admin_page("Download DB")
@bp.route("/get_db")
def get_db_file():
    return send_file(current_app.config['DATABASE'], as_attachment=True)


@bp.route("/consumer/new")
def consumer_new():
    return render_template("consumer_form.html")


@bp.route("/consumer/<int:id>")
def consumer_form(id=None):
    db = get_db()
    consumer_row = db.execute("SELECT * FROM consumers WHERE id=?", [id]).fetchone()
    return render_template("consumer_form.html", consumer=consumer_row)


@bp.route("/consumer/update", methods=['POST'])
def consumer_update():
    db = get_db()

    consumer_id = request.form.get("consumer_id", None)

    if consumer_id is None:
        # Adding a new consumer
        cur = db.execute("INSERT INTO consumers (lti_consumer, lti_secret, openai_key) VALUES (?, ?, ?)",
                         [request.form['lti_consumer'], request.form['lti_secret'], request.form['openai_key']])
        consumer_id = cur.lastrowid
        db.commit()

    elif 'clear_lti_secret' in request.form:
        db.execute("UPDATE consumers SET lti_secret='' WHERE id=?", [consumer_id])
        db.commit()

    elif 'clear_openai_key' in request.form:
        db.execute("UPDATE consumers SET openai_key='' WHERE id=?", [consumer_id])
        db.commit()

    else:
        # Updating
        if request.form.get('lti_secret', ''):
            db.execute("UPDATE consumers SET lti_secret=? WHERE id=?", [request.form['lti_secret'], consumer_id])
            db.commit()
        if request.form.get('openai_key', ''):
            db.execute("UPDATE consumers SET openai_key=? WHERE id=?", [request.form['openai_key'], consumer_id])
            db.commit()

    # anything might have changed: reload all consumers
    reload_consumers()

    return redirect(url_for(".consumer_form", consumer_id=consumer_id))
