
import os
import sqlite3
import time
import uuid
import random

from flask import (
    Flask, render_template, request, redirect,
    url_for, send_from_directory, abort, g, send_file
)

import qrcode
from io import BytesIO
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "uploads")
app.config["DATABASE"] = os.path.join(BASE_DIR, "instance", "sharebox.db")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload

# Ensure folders exist
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)


# -----------------------------
# Database helpers
# -----------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            filename TEXT,
            original_name TEXT,
            content TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );
        """
    )
    db.commit()


@app.before_request
def before_request():
    init_db()
    cleanup_expired()


def cleanup_expired():
    """Delete expired items and their files."""
    now = int(time.time())
    db = get_db()
    cur = db.execute(
        "SELECT id, kind, filename FROM items WHERE expires_at IS NOT NULL AND expires_at < ?",
        (now,),
    )
    rows = cur.fetchall()
    for row in rows:
        if row["kind"] == "file" and row["filename"]:
            file_path = os.path.join(app.config["UPLOAD_FOLDER"], row["filename"])
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
    db.execute("DELETE FROM items WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
    db.commit()


# -----------------------------
# Utility functions
# -----------------------------
#def generate_id():
    #return uuid.uuid4().hex[:4]
def generate_id():
    return str(random.randint(1000, 9999))


EXPIRY_OPTIONS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "1d": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "never": None,
}


def get_expiry_timestamp(option_key):
    now = int(time.time())
    seconds = EXPIRY_OPTIONS.get(option_key)
    if seconds is None:
        return None
    return now + seconds


@app.template_filter("datetimeformat")
def datetimeformat(value):
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


# -----------------------------
# Routes
# -----------------------------
@app.route("/home")
def index():
    return render_template("index.html")


@app.route("/", methods=["GET", "POST"])
def share():
    if request.method == "POST":
        text_content = (request.form.get("text") or "").strip()
        expiry_option = request.form.get("expiry", "1d")
        expires_at = get_expiry_timestamp(expiry_option)

        file = request.files.get("file")
        db = get_db()
        item_id = generate_id()
        now = int(time.time())

        if file and file.filename:
            stored_name = f"{item_id}_{file.filename}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], stored_name))

            db.execute(
                "INSERT INTO items (id, kind, filename, original_name, content, created_at, expires_at) "
                "VALUES (?, 'file', ?, ?, NULL, ?, ?)",
                (item_id, stored_name, file.filename, now, expires_at),
            )
            db.commit()
            return redirect(url_for("view_item", item_id=item_id))

        if text_content:
            db.execute(
                "INSERT INTO items (id, kind, filename, original_name, content, created_at, expires_at) "
                "VALUES (?, 'text', NULL, NULL, ?, ?, ?)",
                (item_id, text_content, now, expires_at),
            )
            db.commit()
            return redirect(url_for("view_item", item_id=item_id))

        return render_template("share.html", error="Please enter text or choose a file.")

    return render_template("share.html")


@app.route("/<item_id>")
def view_item(item_id):
    db = get_db()
    cur = db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    item = cur.fetchone()
    if item is None:
        abort(404)

    # Check expiry at view-time too
    if item["expires_at"] is not None and item["expires_at"] < int(time.time()):
        if item["kind"] == "file" and item["filename"]:
            file_path = os.path.join(app.config["UPLOAD_FOLDER"], item["filename"])
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
        db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        db.commit()
        abort(404)

    share_url = url_for("view_item", item_id=item_id, _external=True)
    return render_template("view_item.html", item=item, share_url=share_url)


@app.route("/download/<item_id>")
def download_file(item_id):
    db = get_db()
    cur = db.execute("SELECT * FROM items WHERE id = ? AND kind = 'file'", (item_id,))
    item = cur.fetchone()
    if item is None:
        abort(404)

    if item["expires_at"] is not None and item["expires_at"] < int(time.time()):
        abort(404)

    return send_from_directory(
        app.config["UPLOAD_FOLDER"],
        item["filename"],
        as_attachment=True,
        download_name=item["original_name"],
    )



@app.route("/qrcode/<item_id>")
def qrcode_image(item_id):
    db = get_db()
    cur = db.execute("SELECT id FROM items WHERE id = ?", (item_id,))
    item = cur.fetchone()
    if item is None:
        abort(404)

    share_url = url_for("view_item", item_id=item_id, _external=True)
    img = qrcode.make(share_url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

@app.route("/about")
def about():
    return render_template("about.html")
# Receiver input page
@app.route("/receiver")
def receiver_home():
    code = request.args.get("code")
    if code:
        return redirect(url_for("receiver_item", item_id=code))
    return render_template("receiver_home.html")


# Fetch the shared item
@app.route("/receiver/<item_id>")
def receiver_item(item_id):
    db = get_db()
    cur = db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
    item = cur.fetchone()

    if item is None:
        abort(404)

    if item["expires_at"] and item["expires_at"] < int(time.time()):
        abort(404)

    return render_template("receiver.html", item=item)





@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404





if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
