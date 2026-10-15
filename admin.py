import os

from functools import wraps

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    current_user,
    login_required,
    logout_user,
)
from sqlalchemy import select

from forms import ActionForm
from models import User, Workspace, db


class AccessBlock(db.Model):
    """Bloqueios de acesso aplicados pelo dono do sistema."""

    __tablename__ = "access_blocks"

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        primary_key=True,
    )


def is_platform_admin():
    owner_email = os.environ.get(
        "ADMIN_EMAIL", ""
    ).strip().lower()

    return bool(
        owner_email
        and current_user.is_authenticated
        and current_user.email == owner_email
    )


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_platform_admin():
            abort(403)

        return view(*args, **kwargs)

    return wrapped


def init_admin(app):
    @app.context_processor
    def admin_context():
        return {
            "is_platform_admin": is_platform_admin(),
        }

    @app.before_request
    def enforce_access():
        if not current_user.is_authenticated:
            return None

        if is_platform_admin():
            return None

        blocked = db.session.get(
            AccessBlock,
            current_user.id,
        )

        if blocked is not None:
            logout_user()

            # Evita redirecionamento repetido após o logout.
            from flask import session
            session.clear()

            flash(
                "Seu acesso está bloqueado. "
                "Entre em contato com a KBSistemas.",
                "error",
            )

            return redirect(url_for("login"))

        return None

    @app.get("/administracao")
    @admin_required
    def admin_panel():
        users = db.session.scalars(
            select(User).order_by(
                User.created_at.desc(),
                User.id.desc(),
            )
        ).all()

        blocked_ids = set(
            db.session.scalars(
                select(AccessBlock.user_id)
            ).all()
        )

        workspace_counts = {}

        for user_id in db.session.scalars(
            select(Workspace.user_id)
        ).all():
            workspace_counts[user_id] = (
                workspace_counts.get(user_id, 0) + 1
            )

        return render_template(
            "admin.html",
            title="Administração",
            users=users,
            blocked_ids=blocked_ids,
            workspace_counts=workspace_counts,
        )

    @app.post("/administracao/usuarios/<int:user_id>/acesso")
    @admin_required
    def admin_set_access(user_id):
        form = ActionForm()

        if not form.validate_on_submit():
            abort(400)

        user = db.session.get(User, user_id)

        if user is None:
            abort(404)

        owner_email = os.environ.get(
            "ADMIN_EMAIL", ""
        ).strip().lower()

        if user.email == owner_email:
            abort(
                400,
                description="A conta do dono não pode ser bloqueada.",
            )

        action = request.form.get("action")

        if action not in ("block", "unblock"):
            abort(400)

        block = db.session.get(AccessBlock, user.id)

        if action == "block":
            if block is None:
                db.session.add(
                    AccessBlock(user_id=user.id)
                )

        elif block is not None:
            db.session.delete(block)

        db.session.commit()

        flash(
            "Acesso bloqueado."
            if action == "block"
            else "Acesso liberado.",
            "success",
        )

        return redirect(url_for("admin_panel"))
