# SPDX-FileCopyrightText: 2023 Mark Liffiton <liffiton@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-only

import platform
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from sqlite3 import Row
from tempfile import NamedTemporaryFile
from typing import Any, ParamSpec, TypeVar
from urllib.parse import urlencode

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask.app import Flask
from werkzeug.wrappers.response import Response

from .auth import admin_required
from .csv import csv_response
from .db import backup_db, get_db
from .openai import get_models

bp = Blueprint('admin', __name__, url_prefix="/admin", template_folder='templates')


@bp.before_request
@admin_required
def before_request() -> None:
    """ Apply decorator to protect all admin blueprint endpoints. """


@dataclass(frozen=True)
class DBDownloadStatus:
    """Status of database download encryption."""
    encrypted: bool
    reason: str | None = None  # reason provided if not encrypted

@bp.context_processor
def inject_db_download_status() -> dict[str, DBDownloadStatus]:
    if platform.system() == "Windows":
        status = DBDownloadStatus(False, "Encryption unavailable on Windows servers.")
    elif not current_app.config.get('AGE_PUBLIC_KEY'):
        status = DBDownloadStatus(False, "No encryption key configured, AGE_PUBLIC_KEY not set.")
    else:
        status = DBDownloadStatus(True)
    return {'db_download_status': status}


@dataclass(frozen=True)
class AdminLink:
    """Represents a link in the admin interface.
    Attributes:
        endpoint: The Flask endpoint name
        display: The text to show in the navigation UI
    """
    endpoint: str
    display: str

# For decorator type hints
P = ParamSpec('P')
R = TypeVar('R')

@dataclass
class AdminLinks:
    """Container for registering admin navigation links."""
    regular: list[AdminLink] = field(default_factory=list)
    right: list[AdminLink] = field(default_factory=list)

    def register(self, display_name: str, right: bool = False) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Decorator to register an admin page link.
        Args:
            display_name: Text to show in the admin interface navigation
            right: If True, display this link on the right side of the nav bar
        """
        def decorator(route_func: Callable[P, R]) -> Callable[P, R]:
            handler_name = f"admin.{route_func.__name__}"
            link = AdminLink(handler_name, display_name)
            if right:
                self.right.append(link)
            else:
                self.regular.append(link)
            return route_func
        return decorator

    def get_template_context(self) -> dict[str, list[AdminLink]]:
        return {
            'admin_links': self.regular,
            'admin_links_right': self.right,
        }

# Module-level instance
_admin_links = AdminLinks()
register_admin_link = _admin_links.register  # name for the decorator to be imported/used in other modules


def init_app(app: Flask) -> None:
    @app.context_processor
    def inject_admin_links() -> dict[str, list[AdminLink]]:
        return _admin_links.get_template_context()


@dataclass(frozen=True)
class ChartData:
    labels: list[str | int | float]
    series: dict[str, list[int | float]]
    colors: list[str]


# A module-level list of registered charts for the main admin page.  Updated by register_admin_chart()
_admin_chart_generators: list[Callable[[str, list[str]], list[ChartData]]] = []


def register_admin_chart(generator_func: Callable[[str, list[str]], list[ChartData]]) -> None:
    _admin_chart_generators.append(generator_func)


def reload_consumers() -> None:
    db = get_db()
    consumer_rows = db.execute("SELECT * FROM consumers").fetchall()
    consumer_dict = {
        row['lti_consumer']: {"secret": row['lti_secret']} for row in consumer_rows
    }
    current_app.config['PYLTI_CONFIG']['consumers'] = consumer_dict


@dataclass(frozen=True)
class FilterSpec:
    name: str
    column: str
    display: str

@dataclass(frozen=True)
class Filter:
    spec: FilterSpec
    value: str
    display_value: str


class Filters:
    def __init__(self) -> None:
        self._filters: list[Filter] = []

    def __iter__(self) -> Iterator[Filter]:
        return self._filters.__iter__()

    def add(self, spec: FilterSpec, value: str, display_value: str) -> None:
        self._filters.append(Filter(spec, value, display_value))

    def make_where(self, selected: list[str]) -> tuple[str, list[Any]]:
        filters = [f for f in self._filters if f.spec.name in selected]
        if not filters:
            return "1", []
        else:
            return (
                " AND ".join(f"{f.spec.column}=?" for f in filters),
                [f.value for f in filters]
            )

    def filter_string(self) -> str:
        filter_dict = {f.spec.name: f.value for f in self._filters}
        return f"?{urlencode(filter_dict)}"

    def filter_string_without(self, exclude_name: str) -> str:
        filter_dict = {f.spec.name: f.value for f in self._filters if f.spec.name != exclude_name}
        return f"?{urlencode(filter_dict)}"

    def template_string(self, selected_name: str) -> str:
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


def get_queries_filtered(where_clause: str, where_params: list[str], queries_limit: int | None = None) -> list[Row]:
    db = get_db()
    sql = f"""
        SELECT
            queries.*,
            users.id AS user_id,
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
        WHERE {where_clause}
        ORDER BY queries.id DESC
    """
    if queries_limit is not None:
        sql += f"LIMIT {int(queries_limit)}"
    queries = db.execute(sql, [*where_params]).fetchall()
    return queries


@bp.route("/csv/queries/")
def get_queries_csv() -> str | Response:
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
            filters.add(spec, value, "dummy value")  # display value not used in CSV export

    # queries, filtered by consumer, class, user, and role
    where_clause, where_params = filters.make_where(['consumer', 'class', 'user', 'role'])
    queries = get_queries_filtered(where_clause, where_params)

    return csv_response('admin_export', 'queries', queries)


@bp.route("/")
def main() -> str:
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
            models.shortname AS model,
            COUNT(queries.id) AS num_queries,
            COUNT(DISTINCT classes.id) AS num_classes,
            COUNT(DISTINCT roles.id) AS num_users,
            SUM(CASE WHEN queries.query_time > date('now', '-7 days') THEN 1 ELSE 0 END) AS num_recent_queries
        FROM consumers
        LEFT JOIN models ON models.id=consumers.model_id
        LEFT JOIN classes_lti ON classes_lti.lti_consumer_id=consumers.id
        LEFT JOIN classes ON classes.id=classes_lti.class_id
        LEFT JOIN roles ON roles.class_id=classes.id
        LEFT JOIN queries ON queries.role_id=roles.id
        GROUP BY consumers.id
        ORDER BY num_recent_queries DESC, consumers.id DESC
    """).fetchall()

    # classes, filtered by consumer
    where_clause, where_params = filters.make_where(['consumer'])
    classes = db.execute(f"""
        SELECT
            classes.id,
            classes.name,
            COALESCE(consumers.lti_consumer, class_owner.display_name) AS owner,
            models.shortname AS model,
            COUNT(DISTINCT roles.id) AS num_users,
            COUNT(queries.id) AS num_queries,
            SUM(CASE WHEN queries.query_time > date('now', '-7 days') THEN 1 ELSE 0 END) AS num_recent_queries
        FROM classes
        LEFT JOIN classes_user ON classes.id=classes_user.class_id
        LEFT JOIN users AS class_owner ON classes_user.creator_user_id=class_owner.id
        LEFT JOIN models ON models.id=classes_user.model_id
        LEFT JOIN classes_lti ON classes.id=classes_lti.class_id
        LEFT JOIN consumers ON consumers.id=classes_lti.lti_consumer_id
        LEFT JOIN roles ON roles.class_id=classes.id
        LEFT JOIN queries ON queries.role_id=roles.id
        WHERE {where_clause}
        GROUP BY classes.id
        ORDER BY num_recent_queries DESC, classes.id DESC
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
        WHERE {where_clause}
        GROUP BY users.id
        ORDER BY num_recent_queries DESC, users.id DESC
    """, where_params).fetchall()

    # roles, filtered by consumer, class, and user
    where_clause, where_params = filters.make_where(['consumer', 'class', 'user'])
    roles = db.execute(f"""
        SELECT
            roles.*,
            users.display_name,
            users.email,
            users.auth_name,
            classes.name AS class_name,
            COALESCE(consumers.lti_consumer, class_owner.display_name) AS class_owner,
            auth_providers.name AS auth_provider
        FROM roles
        LEFT JOIN users ON users.id=roles.user_id
        LEFT JOIN auth_providers ON users.auth_provider=auth_providers.id
        LEFT JOIN classes ON roles.class_id=classes.id
        LEFT JOIN classes_lti ON classes.id=classes_lti.class_id
        LEFT JOIN classes_user ON classes.id=classes_user.class_id
        LEFT JOIN users AS class_owner ON classes_user.creator_user_id=class_owner.id
        LEFT JOIN consumers ON consumers.id=classes_lti.lti_consumer_id
        WHERE {where_clause}
        ORDER BY roles.id DESC
    """, where_params).fetchall()

    # queries, filtered by consumer, class, user, and role
    where_clause, where_params = filters.make_where(['consumer', 'class', 'user', 'role'])
    queries = get_queries_filtered(where_clause, where_params, queries_limit=200)

    charts = []
    for generate_chart in _admin_chart_generators:
        charts.extend(generate_chart(where_clause, where_params))

    return render_template("admin.html", charts=charts, consumers=consumers, classes=classes, users=users, roles=roles, queries=queries, filters=filters)


@register_admin_link("Download DB", right=True)
@bp.route("/get_db")
def get_db_file() -> Response:
    db_name = current_app.config['DATABASE_NAME']
    db_basename = Path(db_name).stem
    dl_name = f"{db_basename}_{date.today().strftime('%Y%m%d')}.db"
    if current_app.config.get('AGE_PUBLIC_KEY'):
        dl_name += '.age'

    if platform.system() == "Windows":
        # Slightly unsafe way to do it, because the file may be written while
        # send_file is sending it.  Temp file issues make it hard to do
        # otherwise on Windows, though, and no one should run a production
        # server for this on Windows, anyway.
        if current_app.config.get('AGE_PUBLIC_KEY'):
            current_app.logger.warning("Database download on Windows does not support encryption")
        return send_file(current_app.config['DATABASE'],
                         mimetype='application/vnd.sqlite3',
                         as_attachment=True, download_name=dl_name)
    else:
        db_backup_file = NamedTemporaryFile()
        backup_db(Path(db_backup_file.name))
        return send_file(db_backup_file,
                         mimetype='application/vnd.sqlite3',
                         as_attachment=True, download_name=dl_name)


@bp.route("/consumer/new")
def consumer_new() -> str:
    return render_template("consumer_form.html", models=get_models())


@bp.route("/consumer/delete/<int:consumer_id>", methods=['POST'])
def consumer_delete(consumer_id: int) -> Response:
    db = get_db()

    # Check for dependencies
    classes_count = db.execute("SELECT COUNT(*) FROM classes_lti WHERE lti_consumer_id=?", [consumer_id]).fetchone()[0]

    if classes_count > 0:
        flash("Cannot delete consumer: there are related classes.", "warning")
        return redirect(url_for(".consumer_form", consumer_id=consumer_id))

    # No dependencies, proceed with deletion

    # Fetch the consumer's name
    consumer_name_row = db.execute("SELECT lti_consumer FROM consumers WHERE id=?", [consumer_id]).fetchone()
    if not consumer_name_row:
        flash("Invalid id.", "danger")
        return redirect(url_for(".consumer_form", consumer_id=consumer_id))

    consumer_name = consumer_name_row['lti_consumer']

    # Delete the row
    db.execute("DELETE FROM consumers WHERE id=?", [consumer_id])
    db.commit()
    reload_consumers()

    flash(f"Consumer '{consumer_name}' deleted.")

    return redirect(url_for(".main"))


@bp.route("/consumer/<int:consumer_id>")
def consumer_form(consumer_id: int | None = None) -> str:
    db = get_db()
    consumer_row = db.execute("SELECT * FROM consumers WHERE id=?", [consumer_id]).fetchone()
    return render_template("consumer_form.html", consumer=consumer_row, models=get_models())


@bp.route("/consumer/update", methods=['POST'])
def consumer_update() -> Response:
    db = get_db()

    consumer_id = request.form.get("consumer_id", type=int)

    if consumer_id is None:
        # Adding a new consumer
        cur = db.execute("INSERT INTO consumers (lti_consumer, lti_secret, openai_key, model_id) VALUES (?, ?, ?, ?)",
                         [request.form['lti_consumer'], request.form['lti_secret'], request.form['openai_key'], request.form['model_id']])
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

    return redirect(url_for(".consumer_form", consumer_id=consumer_id))
