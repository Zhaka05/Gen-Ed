from collections import namedtuple
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for

from .db import get_db, backup_db
from .auth import admin_required
from .openai import get_models


bp = Blueprint('admin', __name__, url_prefix="/admin", template_folder='templates')


@bp.before_request
@admin_required
def before_request():
    """ Apply decorator to protect all admin blueprint endpoints. """
    pass


# A module-level list of registered admin pages.  Updated by register_admin_link()
_admin_links = []
_admin_links_right = []


# Decorator function for registering routes as admin pages.
# Use:
#   @register_admin_link("Demo Links")
#   @[route stuff]
#   def handler():  [...]
def register_admin_link(display_name, right=False):
    def decorator(route_func):
        handler_name = f"admin.{route_func.__name__}"
        if right:
            _admin_links_right.append((handler_name, display_name))
        else:
            _admin_links.append((handler_name, display_name))
        return route_func
    return decorator


def init_app(app):
    # inject admin pages into template contexts
    @app.context_processor
    def inject_admin_links():
        return dict(admin_links=_admin_links, admin_links_right=_admin_links_right)


def reload_consumers():
    db = get_db()
    consumer_rows = db.execute("SELECT * FROM consumers").fetchall()
    consumer_dict = {
        row['lti_consumer']: {"secret": row['lti_secret']} for row in consumer_rows
    }
    current_app.config['PYLTI_CONFIG']['consumers'] = consumer_dict


FilterSpec = namedtuple('FilterSpec', ('name', 'column', 'display'))
Filter = namedtuple('Filter', ('spec', 'value', 'display_value'))


class Filters:
    def __init__(self):
        self._filters = []

    def __iter__(self):
        return self._filters.__iter__()

    def add(self, spec, value, display_value):
        self._filters.append(Filter(spec, value, display_value))

    def make_where(self, selected):
        filters = [f for f in self._filters if f.spec.name in selected]
        if not filters:
            return "", []
        else:
            return (
                "WHERE " + " AND ".join(f"{f.spec.column}=?" for f in filters),
                [f.value for f in filters]
            )

    def filter_string(self):
        filter_dict = {spec.name: value for (spec, value, _) in self._filters}
        return f"?{urlencode(filter_dict)}"

    def filter_string_without(self, exclude_name):
        filter_dict = {spec.name: value for (spec, value, _) in self._filters if spec.name != exclude_name}
        return f"?{urlencode(filter_dict)}"

    def template_string(self, selected_name):
        '''
        Return a string that will be used to create a link URL for each row in
        a table.  This string is passed to a Javascript function as
        `{{template_string}}`, to be used with string interpolation in
        Javascript.  Therefore, it should contain "${{value}}" as a placeholder
        for the value -- it is rendered by Python's f-string interpolation as
        "${value}" in the page source, suitable for Javascript string
        interpolation.
        '''
        return self.filter_string_without(selected_name) + f"&{selected_name}=${{value}}"


@bp.route("/")
def main():
    db = get_db()
    filters = Filters()

    specs = [
        FilterSpec('consumer', 'consumers.id', 'consumers.lti_consumer'),
        FilterSpec('class', 'classes.id', 'classes.name'),
        FilterSpec('user', 'users.id', 'users.display_name'),
        FilterSpec('role', 'roles.id', 'printf("%s (%s:%s)", users.display_name, role_class.name, roles.role)'),
    ]
    for spec in specs:
        if spec.name in request.args:
            value = request.args[spec.name]
            # bit of a hack to have a single SQL query cover all different filters...
            display_row = db.execute(f"""
                SELECT {spec.display}
                FROM consumers, classes, users
                LEFT JOIN roles ON roles.user_id=users.id
                LEFT JOIN classes AS role_class ON role_class.id=roles.class_id
                WHERE {spec.column}=?
                LIMIT 1
            """, [value]).fetchone()
            display_value = display_row[0]
            filters.add(spec, value, display_value)

    # all consumers
    consumers = db.execute("""
        SELECT
            consumers.*,
            COUNT(queries.id) AS num_queries,
            COUNT(DISTINCT classes.id) AS num_classes,
            COUNT(DISTINCT roles.id) AS num_users,
            SUM(CASE WHEN queries.query_time > date('now', '-7 days') THEN 1 ELSE 0 END) AS num_recent_queries
        FROM consumers
        LEFT JOIN classes_lti ON classes_lti.lti_consumer_id=consumers.id
        LEFT JOIN classes ON classes.id=classes_lti.class_id
        LEFT JOIN roles ON roles.class_id=classes.id
        LEFT JOIN queries ON queries.role_id=roles.id
        GROUP BY consumers.id
        ORDER BY consumers.id DESC
    """).fetchall()

    # classes, filtered by consumer
    where_clause, where_params = filters.make_where(['consumer'])
    classes = db.execute(f"""
        SELECT
            classes.id,
            classes.name,
            COALESCE(consumers.lti_consumer, class_owner.display_name) AS owner,
            COUNT(DISTINCT roles.id) AS num_users,
            COUNT(queries.id) AS num_queries,
            SUM(CASE WHEN queries.query_time > date('now', '-7 days') THEN 1 ELSE 0 END) AS num_recent_queries
        FROM classes
        LEFT JOIN classes_user ON classes.id=classes_user.class_id
        LEFT JOIN users AS class_owner ON classes_user.creator_user_id=class_owner.id
        LEFT JOIN classes_lti ON classes.id=classes_lti.class_id
        LEFT JOIN consumers ON consumers.id=classes_lti.lti_consumer_id
        LEFT JOIN roles ON roles.class_id=classes.id
        LEFT JOIN queries ON queries.role_id=roles.id
        {where_clause}
        GROUP BY classes.id
        ORDER BY classes.id DESC
    """, where_params).fetchall()

    # users, filtered by consumer and class
    where_clause, where_params = filters.make_where(['consumer', 'class'])
    users = db.execute(f"""
        SELECT
            users.id,
            users.display_name,
            users.email,
            users.auth_name,
            auth_providers.name AS auth_provider,
            users.query_tokens,
            COUNT(queries.id) AS num_queries,
            SUM(CASE WHEN queries.query_time > date('now', '-7 days') THEN 1 ELSE 0 END) AS num_recent_queries
        FROM users
        LEFT JOIN auth_providers ON users.auth_provider=auth_providers.id
        LEFT JOIN roles ON roles.user_id=users.id
        LEFT JOIN classes ON roles.class_id=classes.id
        LEFT JOIN classes_lti ON classes.id=classes_lti.class_id
        LEFT JOIN consumers ON consumers.id=classes_lti.lti_consumer_id
        LEFT JOIN queries ON queries.user_id=users.id
        {where_clause}
        GROUP BY users.id
        ORDER BY users.id DESC
    """, where_params).fetchall()

    # roles, filtered by consumer, class, and user
    where_clause, where_params = filters.make_where(['consumer', 'class', 'user'])
    roles = db.execute(f"""
        SELECT
            roles.*,
            users.id,
            users.display_name,
            users.email,
            users.auth_name,
            auth_providers.name AS auth_provider,
            COUNT(queries.id) AS num_queries
        FROM roles
        LEFT JOIN users ON users.id=roles.user_id
        LEFT JOIN auth_providers ON users.auth_provider=auth_providers.id
        LEFT JOIN classes ON roles.class_id=classes.id
        LEFT JOIN classes_lti ON classes.id=classes_lti.class_id
        LEFT JOIN consumers ON consumers.id=classes_lti.lti_consumer_id
        LEFT JOIN queries ON roles.id=queries.role_id
        {where_clause}
        GROUP BY roles.id
        ORDER BY roles.id DESC
    """, where_params).fetchall()

    # queries, filtered by consumer, class, user, and role
    where_clause, where_params = filters.make_where(['consumer', 'class', 'user', 'role'])
    queries_limit = 200
    queries = db.execute(f"""
        SELECT
            queries.*,
            users.id,
            users.display_name,
            users.email,
            users.auth_name,
            auth_providers.name AS auth_provider
        FROM queries
        JOIN users ON queries.user_id=users.id
        LEFT JOIN auth_providers ON users.auth_provider=auth_providers.id
        LEFT JOIN roles ON queries.role_id=roles.id
        LEFT JOIN classes ON roles.class_id=classes.id
        LEFT JOIN classes_lti ON classes.id=classes_lti.class_id
        LEFT JOIN consumers ON consumers.id=classes_lti.lti_consumer_id
        {where_clause}
        ORDER BY query_time DESC LIMIT ?
    """, where_params + [queries_limit]).fetchall()

    return render_template("admin.html", consumers=consumers, classes=classes, users=users, roles=roles, queries=queries, filters=filters)


@register_admin_link("Download DB", right=True)
@bp.route("/get_db")
def get_db_file():
    db_backup_file = NamedTemporaryFile()
    backup_db(db_backup_file.name)
    db_name = current_app.config['DATABASE_NAME']
    db_basename = Path(db_name).stem
    dl_name = f"{db_basename}_{date.today().strftime('%Y%m%d')}.db"
    return send_file(db_backup_file, mimetype='application/vnd.sqlite3', as_attachment=True, download_name=dl_name)


@bp.route("/consumer/new")
def consumer_new():
    return render_template("consumer_form.html", models=get_models())


@bp.route("/consumer/<int:id>")
def consumer_form(id=None):
    db = get_db()
    consumer_row = db.execute("SELECT * FROM consumers WHERE id=?", [id]).fetchone()
    return render_template("consumer_form.html", consumer=consumer_row, models=get_models())


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
        flash(f"Consumer {request.form['lti_consumer']} created.")

    elif 'clear_lti_secret' in request.form:
        db.execute("UPDATE consumers SET lti_secret='' WHERE id=?", [consumer_id])
        db.commit()
        flash("Consumer secret cleared.")

    elif 'clear_openai_key' in request.form:
        db.execute("UPDATE consumers SET openai_key='' WHERE id=?", [consumer_id])
        db.commit()
        flash("Consumer API key cleared.")

    else:
        # Updating
        if request.form.get('lti_secret', ''):
            db.execute("UPDATE consumers SET lti_secret=? WHERE id=?", [request.form['lti_secret'], consumer_id])
        if request.form.get('openai_key', ''):
            db.execute("UPDATE consumers SET openai_key=? WHERE id=?", [request.form['openai_key'], consumer_id])
        if request.form.get('model_id', ''):
            db.execute("UPDATE consumers SET model_id=? WHERE id=?", [request.form['model_id'], consumer_id])
        db.commit()
        flash("Consumer updated.")

    # anything might have changed: reload all consumers
    reload_consumers()

    return redirect(url_for(".consumer_form", id=consumer_id))
