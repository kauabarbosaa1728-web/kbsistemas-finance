import calendar
import os

from datetime import date, datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func, select

from forms import ActionForm
from models import User, Workspace, db


TIMEZONE = ZoneInfo("America/Sao_Paulo")


# --------------------------------------------------
# MODELOS DE CONTROLE DE ACESSO
# --------------------------------------------------

class AccessBlock(db.Model):
    """Bloqueios manuais aplicados pelo administrador."""

    __tablename__ = "access_blocks"

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        primary_key=True,
    )


class Subscription(db.Model):
    """Liberação manual do plano de um cliente."""

    __tablename__ = "subscriptions"

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        primary_key=True,
    )

    # Primeiro dia em que o acesso deixa de valer.
    # Exemplo: expires_on=15/11 permite uso até 14/11.
    expires_on = db.Column(
        db.Date,
        nullable=False,
    )

    granted_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(TIMEZONE),
        onupdate=lambda: datetime.now(TIMEZONE),
    )


# --------------------------------------------------
# FUNÇÕES AUXILIARES
# --------------------------------------------------

def today_br():
    return datetime.now(TIMEZONE).date()


def owner_email():
    return os.environ.get(
        "ADMIN_EMAIL", ""
    ).strip().lower()


def is_platform_admin():
    configured_email = owner_email()

    return bool(
        configured_email
        and current_user.is_authenticated
        and current_user.email == configured_email
    )


def add_one_month(value):
    month_index = value.month
    year = value.year + month_index // 12
    month = month_index % 12 + 1

    last_day = calendar.monthrange(year, month)[1]

    return date(
        year,
        month,
        min(value.day, last_day),
    )


def access_details(user_id):
    block = db.session.get(AccessBlock, user_id)
    subscription = db.session.get(Subscription, user_id)

    if block is not None:
        status = "blocked"

    elif subscription is None:
        status = "pending"

    elif subscription.expires_on <= today_br():
        status = "expired"

    else:
        status = "active"

    return {
        "status": status,
        "active": status == "active",
        "subscription": subscription,
    }


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_platform_admin():
            abort(403)

        return view(*args, **kwargs)

    return wrapped


# --------------------------------------------------
# INTEGRAÇÃO COM O APLICATIVO
# --------------------------------------------------

def init_admin(app):
    @app.context_processor
    def admin_context():
        admin = is_platform_admin()

        context = {
            "is_platform_admin": admin,
            "has_active_plan": False,
            "access_status": "anonymous",
            "subscription_expires_on": None,
        }

        if not current_user.is_authenticated:
            return context

        if admin:
            context["has_active_plan"] = True
            context["access_status"] = "admin"

            return context

        details = access_details(current_user.id)

        context["has_active_plan"] = details["active"]
        context["access_status"] = details["status"]

        if details["subscription"] is not None:
            context["subscription_expires_on"] = (
                details["subscription"].expires_on
            )

        return context

    @app.before_request
    def enforce_access():
        # O login_required das rotas protege quem não entrou.
        if not current_user.is_authenticated:
            return None

        # O dono não precisa contratar o plano.
        if is_platform_admin():
            return None

        endpoint = request.endpoint

        # Preserva as respostas normais para URLs inexistentes.
        if endpoint is None:
            return None

        # Cliente sem plano pode consultar a oferta e sair.
        allowed_endpoints = {
            "plan",
            "logout",
            "static",
            "health",
        }

        if endpoint in allowed_endpoints:
            return None

        # As próprias rotas administrativas negam acesso
        # a qualquer pessoa que não seja o dono.
        if endpoint in {
            "admin_panel",
            "admin_set_access",
        }:
            return None

        details = access_details(current_user.id)

        if details["active"]:
            return None

        # Não permite consultar dados financeiros nem
        # cadastrar, editar ou excluir sem plano ativo.
        if request.path.startswith("/api/"):
            response = jsonify({
                "error": "plan_required",
                "access_status": details["status"],
                "message": (
                    "Seu acesso precisa ser liberado "
                    "pelo responsável."
                ),
                "plan_url": url_for("plan"),
            })

            response.status_code = 403
            response.headers["Cache-Control"] = "no-store"

            return response

        # 303 transforma uma tentativa de POST em GET
        # para a página do plano, sem executar a alteração.
        return redirect(url_for("plan"), code=303)

    # --------------------------------------------------
    # PAINEL DO ADMINISTRADOR
    # --------------------------------------------------

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

        subscription_items = db.session.scalars(
            select(Subscription)
        ).all()

        subscriptions = {
            item.user_id: item
            for item in subscription_items
        }

        active_ids = {
            item.user_id
            for item in subscription_items
            if (
                item.expires_on > today_br()
                and item.user_id not in blocked_ids
            )
        }

        workspace_counts = dict(
            db.session.execute(
                select(
                    Workspace.user_id,
                    func.count(Workspace.id),
                ).group_by(Workspace.user_id)
            ).all()
        )

        access_statuses = {}

        for user in users:
            if user.email == owner_email():
                access_statuses[user.id] = "admin"

            elif user.id in blocked_ids:
                access_statuses[user.id] = "blocked"

            elif user.id not in subscriptions:
                access_statuses[user.id] = "pending"

            elif user.id in active_ids:
                access_statuses[user.id] = "active"

            else:
                access_statuses[user.id] = "expired"

        return render_template(
            "admin.html",
            title="Administração",
            users=users,
            blocked_ids=blocked_ids,
            workspace_counts=workspace_counts,
            subscriptions=subscriptions,
            active_ids=active_ids,
            access_statuses=access_statuses,
            today=today_br(),
        )

    # --------------------------------------------------
    # LIBERAÇÃO, RENOVAÇÃO E BLOQUEIO
    # --------------------------------------------------

    @app.post("/administracao/usuarios/<int:user_id>/acesso")
    @admin_required
    def admin_set_access(user_id):
        form = ActionForm()

        if not form.validate_on_submit():
            abort(400)

        action = request.form.get("action")

        if action not in {
            "activate",
            "renew",
            "block",
            "unblock",
        }:
            abort(400)

        # Serializa alterações simultâneas sobre o mesmo
        # cliente no PostgreSQL.
        user = db.session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
        )

        if user is None:
            abort(404)

        if user.email == owner_email():
            abort(
                400,
                description=(
                    "A conta do dono é isenta "
                    "e não pode ser alterada por esta ação."
                ),
            )

        block = db.session.get(AccessBlock, user.id)
        subscription = db.session.get(
            Subscription,
            user.id,
        )

        if action == "block":
            if block is None:
                db.session.add(
                    AccessBlock(user_id=user.id)
                )

            db.session.commit()

            flash(
                "Acesso bloqueado. "
                "Os dados do cliente foram preservados.",
                "success",
            )

            return redirect(url_for("admin_panel"))

        # "unblock" mantém compatibilidade com o botão
        # Liberar acesso da versão anterior do painel.
        if action == "renew":
            base_date = today_br()

            if (
                subscription is not None
                and subscription.expires_on > base_date
            ):
                base_date = subscription.expires_on

            expiration = add_one_month(base_date)

        else:
            # Liberar não reduz uma validade já existente
            # nem acrescenta meses por cliques repetidos.
            if (
                subscription is not None
                and subscription.expires_on > today_br()
            ):
                expiration = subscription.expires_on
            else:
                expiration = add_one_month(today_br())

        if subscription is None:
            subscription = Subscription(
                user_id=user.id,
                expires_on=expiration,
                granted_by=current_user.id,
            )

            db.session.add(subscription)

        else:
            subscription.expires_on = expiration
            subscription.granted_by = current_user.id
            subscription.updated_at = datetime.now(TIMEZONE)

        if block is not None:
            db.session.delete(block)

        db.session.commit()

        flash(
            (
                "Plano renovado."
                if action == "renew"
                else "Acesso liberado."
            )
            + " Próximo vencimento: "
            + expiration.strftime("%d/%m/%Y")
            + ".",
            "success",
        )

        return redirect(url_for("admin_panel"))
