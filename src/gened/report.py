# SPDX-FileCopyrightText: 2025 Mark Liffiton <liffiton@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-only

"""
LLM-powered query/chat summarization/clustering/reporting for instructors

"""


from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from jinja2 import Environment

from .db import get_db
from .llm import with_llm, LLM
from .auth import ClassData, instructor_required, get_auth_class

bp = Blueprint('report', __name__, url_prefix="/report", template_folder="templates")

@bp.before_request
@instructor_required
def before_request() -> None:
    """ Apply decorator to protect all instructor blueprint endpoints. """


jinja_env = Environment(
    trim_blocks=True,
    lstrip_blocks=True,
)

template = jinja_env.from_string("""


=== STUDENT QUERIES ===
{% for query in queries %}
    Query {{ loop.index }}
    <code>
    {{ query["code"] }}
    </code>
    <error>
    {{ query["error"] }}
    </error>
    <issue>
    {{ query["issue"] }}
    </issue>
{% endfor %}

""")

@bp.route("/", methods=['GET'])
def main():
    return render_template("report.html")

@bp.route("/", methods=['POST'])
@with_llm(spend_token=True)
def post_report(llm: LLM) -> str:

    db = get_db()
    auth = get_auth_class()
    class_id = auth.class_id

    rows = db.execute("""
    SELECT code, error, issue, role_id FROM code_queries WHERE role_id = ?
    """, [class_id])

    # api request
    response_main, response_txt = asyncio.run(
        llm.get_completion(messages=[{"role": "system", "content": template.render(queries=rows)}])
    )
    # display response
    return render_template("report.html", main=response_main, response=response_txt)

