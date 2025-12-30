from http.client import responses

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
from .db import get_db
from sqlite3 import Row
from .auth import get_auth
from .llm import with_llm, LLM
import asyncio
bp = Blueprint('report', __name__, url_prefix="/report", template_folder="templates")

@bp.route("/", methods=['GET'])
def main():
    return render_template("report.html")

@bp.route("/", methods=['POST'])
@with_llm(spend_token=True)
def post_report(llm: LLM) -> str:
    db = get_db()
    rows = db.execute("""
    SELECT code, error, issue FROM code_queries
    """)
    # work with this rows
    prompt = """Please summarize student queries\n"""
    for i, row in enumerate(rows):
        prompt += f"Student {i+1}\n"
        prompt += f"code: {row['code']}\n"
        prompt += f"code: {row['error']}\n"
        prompt += f"code: {row['issue']}\n"
        prompt += "\n"

    # api request
    # response_main, response_txt = asyncio.run(
    #     llm.get_completion(prompt=prompt)
    # )
    print(prompt)
    # display response
    return render_template("report.html", main=response_main, response=response_txt)

async def get_response(llm: LLM, prompt):
    ...
# @bp.route("/add", methods=["POST"])
# def add_query() -> str:
#     db = get_db()
#     auth = get_auth()
#     role_id = auth.cur_class.role_id if auth.cur_class else None
#
#     code = request.form["code"]
#     error = request.form["error"]
#     issue = request.form["issue"]
#
#     cur = db.execute(
#         "INSERT INTO code_queries (code, error, issue, user_id, role_id) VALUES (?, ?, ?, ?, ?)",
#         [code, error, issue, auth.user_id, role_id]
#     )
#     db.commit()
#     return redirect(url_for("report.main"))
#
# @bp.route("/add", methods=["GET"])
# def show_add_query():
#     return render_template("add_query.html")

