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
    session,
    url_for,
)
from flask_login import (
    current_user,
    login_required,
    logout_user,
)
from sqlalchemy import func, select

from forms import ActionForm
from models import User, Workspace, db


TIMEZONE = ZoneInfo("America/Sao_Paulo")


# --------------------------------------------------
# MODELOS
# --------------------------------------------------

class AccessBlock(db.Model):
    """Bloqueio manual aplicado pelo administrador."""

    __tablename__ = "access_blocks"

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        primary_key=True,
    )


class Subscription(db.Model):
    """Período de acesso liberado manualmente pelo dono."""

    __tablename__ = "subscriptions"

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        primary_key=True,
    )

    # Primeiro dia em que o acesso deixa de valer.
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


def private_json(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"

    return response


def admin_snapshot():
    """Consulta apenas dados cadastrais e de acesso."""

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

    subscriptions = {
        item.user_id: item
        for item in db.session.scalars(
            select(Subscription)
        ).all()
    }

    workspace_counts = dict(
        db.session.execute(
            select(
                Workspace.user_id,
                func.count(Workspace.id),
            ).group_by(Workspace.user_id)
        ).all()
    )

    active_ids = set()
    access_statuses = {}
    current_date = today_br()
    configured_owner = owner_email()

    for user in users:
        subscription = subscriptions.get(user.id)

        if configured_owner and user.email == configured_owner:
            status = "admin"

        elif user.id in blocked_ids:
            status = "blocked"

        elif subscription is None:
            status = "pending"

        elif subscription.expires_on <= current_date:
            status = "expired"

        else:
            status = "active"
            active_ids.add(user.id)

        access_statuses[user.id] = status

    return {
        "users": users,
        "blocked_ids": blocked_ids,
        "subscriptions": subscriptions,
        "workspace_counts": workspace_counts,
        "active_ids": active_ids,
        "access_statuses": access_statuses,
        "today": current_date,
    }


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

        subscription = details["subscription"]

        if subscription is not None:
            context["subscription_expires_on"] = (
                subscription.expires_on
            )

        return context

    @app.before_request
    def enforce_access():
        if not current_user.is_authenticated:
            return None

        if is_platform_admin():
            return None

        endpoint = request.endpoint

        if endpoint is None:
            return None

        # Rotas necessárias enquanto o cliente aguarda.
        allowed_endpoints = {
            "plan",
            "plan_status",
            "plan_login",
            "logout",
            "static",
            "health",
        }

        if endpoint in allowed_endpoints:
            return None

        # Essas rotas têm sua própria verificação de dono.
        if endpoint in {
            "admin_panel",
            "admin_status",
            "admin_set_access",
        }:
            return None

        details = access_details(current_user.id)

        if details["active"]:
            return None

        if request.path.startswith("/api/"):
            return private_json({
                "error": "plan_required",
                "access_status": details["status"],
                "message": (
                    "Aguarde a confirmação do responsável "
                    "para acessar o sistema."
                ),
                "plan_url": url_for("plan"),
            }, 403)

        # Nenhuma alteração financeira é executada.
        return redirect(url_for("plan"), code=303)

    # --------------------------------------------------
    # CONSULTA AUTOMÁTICA DA TELA DO CLIENTE
    # --------------------------------------------------

    @app.get("/api/plano/status")
    def plan_status():
        if not current_user.is_authenticated:
            return private_json({
                "error": "authentication_required",
                "login_url": url_for("login"),
            }, 401)

        if is_platform_admin():
            return private_json({
                "status": "admin",
                "access_granted": True,
                "requires_payment": False,
                "dashboard_url": url_for("dashboard"),
            })

        details = access_details(current_user.id)
        subscription = details["subscription"]

        messages = {
            "pending": (
                "Aguardando confirmação do pagamento "
                "pelo responsável."
            ),
            "active": (
                "Pagamento confirmado! Sua conta está "
                "liberada. Faça login para começar."
            ),
            "expired": (
                "Seu plano venceu. Fale com o responsável "
                "para combinar a renovação."
            ),
            "blocked": (
                "Seu acesso está bloqueado. "
                "Fale com o responsável."
            ),
        }

        return private_json({
            "status": details["status"],
            "access_granted": details["active"],
            "requires_payment": True,
            "message": messages[details["status"]],
            "expires_on": (
                subscription.expires_on.isoformat()
                if subscription
                else None
            ),
            "login_action_url": url_for("plan_login"),
        })

    # --------------------------------------------------
    # IR PARA O LOGIN APÓS A CONFIRMAÇÃO
    # --------------------------------------------------

    @app.post("/plano/entrar")
    @login_required
    def plan_login():
        form = ActionForm()

        if not form.validate_on_submit():
            abort(400)

        if is_platform_admin():
            return redirect(url_for("dashboard"))

        details = access_details(current_user.id)

        if not details["active"]:
            flash(
                "Seu acesso ainda não está liberado.",
                "info",
            )

            return redirect(url_for("plan"))

        # O cliente já estava identificado para acompanhar
        # a confirmação. Encerra essa sessão para que ele
        # faça o login solicitado.
        logout_user()
        session.clear()

        flash(
            "Pagamento confirmado! "
            "Entre com seu e-mail e senha.",
            "success",
        )

        return redirect(url_for("login"))

    # --------------------------------------------------
    # PAINEL DO ADMINISTRADOR
    # --------------------------------------------------

    @app.get("/administracao")
    @admin_required
    def admin_panel():
        snapshot = admin_snapshot()

        return render_template(
            "admin.html",
            title="Administração",
            **snapshot,
        )

    # --------------------------------------------------
    # CONSULTA AUTOMÁTICA DOS CADASTROS
    # --------------------------------------------------

    @app.get("/api/administracao/cadastros")
    def admin_status():
        if not current_user.is_authenticated:
            return private_json({
                "error": "authentication_required",
                "login_url": url_for("login"),
            }, 401)

        if not is_platform_admin():
            return private_json({
                "error": "forbidden",
            }, 403)

        snapshot = admin_snapshot()

        counts = {
            "clients": 0,
            "active": 0,
            "pending": 0,
            "expired": 0,
            "blocked": 0,
        }

        users_payload = []

        for user in snapshot["users"]:
            status = snapshot["access_statuses"][user.id]
            subscription = snapshot["subscriptions"].get(user.id)

            if status != "admin":
                counts["clients"] += 1
                counts[status] += 1

            # A assinatura registra a liberação manual.
            # Não existe consulta ao banco ou ao WhatsApp.
            if status == "admin":
                payment_status = "exempt"

            elif status == "active":
                payment_status = "confirmed"

            elif status == "expired":
                payment_status = "renewal_pending"

            elif status == "blocked":
                payment_status = "blocked"

            else:
                payment_status = "awaiting_confirmation"

            users_payload.append({
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "created_at": user.created_at.isoformat(),
                "workspace_count": (
                    snapshot["workspace_counts"].get(user.id, 0)
                ),
                "status": status,
                "payment_status": payment_status,
                "expires_on": (
                    subscription.expires_on.isoformat()
                    if subscription
                    else None
                ),
            })

        return private_json({
            "users": users_payload,
            "counts": counts,
            "checked_at": datetime.now(TIMEZONE).isoformat(),
        })

    # --------------------------------------------------
    # CONFIRMAÇÃO MANUAL E BLOQUEIO
    # --------------------------------------------------

    @app.post("/administracao/usuarios/<int:user_id>/acesso")
    @admin_required
    def admin_set_access(user_id):
        form = ActionForm()

        if not form.validate_on_submit():
            abort(400)

        action = request.form.get("action")

        if action not in {
            "confirm_payment",
            "confirm_renewal",
            "activate",
            "renew",
            "block",
            "unblock",
        }:
            abort(400)

        # Mantém compatibilidade com o painel anterior.
        action = {
            "activate": "confirm_payment",
            "renew": "confirm_renewal",
        }.get(action, action)

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

        # O desbloqueio não confirma um novo pagamento.
        # Se não houver plano vigente, o cliente continua
        # aguardando confirmação ou renovação.
        if action == "unblock":
            if block is not None:
                db.session.delete(block)

            db.session.commit()

            flash(
                "Bloqueio removido. "
                "O acesso depende de uma assinatura vigente.",
                "success",
            )

            return redirect(url_for("admin_panel"))

        current_date = today_br()

        if action == "confirm_renewal":
            # A próxima tela enviará o vencimento que
            # o administrador estava vendo.
            # Isso evita renovar duas vezes pelo mesmo envio.
            expected_expiration = request.form.get(
                "expected_expires_on"
            )

            actual_expiration = (
                subscription.expires_on.isoformat()
                if subscription
                else ""
            )

            if expected_expiration is None:
                db.session.rollback()

                flash(
                    "Atualize o painel de administração "
                    "antes de confirmar uma renovação.",
                    "error",
                )

                return redirect(url_for("admin_panel"))

            if expected_expiration != actual_expiration:
                db.session.rollback()

                flash(
                    "A assinatura já foi alterada. "
                    "Confira o vencimento antes de renovar novamente.",
                    "info",
                )

                return redirect(url_for("admin_panel"))

            base_date = current_date

            if (
                subscription is not None
                and subscription.expires_on > current_date
            ):
                base_date = subscription.expires_on

            expiration = add_one_month(base_date)

        else:
            # Repetir a confirmação inicial de um plano
            # vigente não acrescenta outro mês.
            if (
                subscription is not None
                and subscription.expires_on > current_date
            ):
                expiration = subscription.expires_on

            else:
                expiration = add_one_month(current_date)

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
                "Renovação confirmada."
                if action == "confirm_renewal"
                else "Pagamento confirmado e acesso liberado."
            )
            + " Próximo vencimento: "
            + expiration.strftime("%d/%m/%Y")
            + ".",
            "success",
        )

        return redirect(url_for("admin_panel"))
