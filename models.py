from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, UniqueConstraint
from werkzeug.security import (
    check_password_hash,
    generate_password_hash,
)


db = SQLAlchemy()


def utc_now():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100), nullable=False)

    email = db.Column(
        db.String(254),
        unique=True,
        nullable=False,
        index=True,
    )

    password_hash = db.Column(
        db.String(255),
        nullable=False,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(
            self.password_hash,
            password,
        )


class Workspace(db.Model):
    """Espaço financeiro pessoal ou empresarial de um usuário."""

    __tablename__ = "workspaces"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    name = db.Column(db.String(100), nullable=False)

    kind = db.Column(db.String(20), nullable=False)

    # Aumentará a cada alteração para detectar atualizações
    # em outra aba, celular ou computador.
    revision = db.Column(
        db.Integer,
        default=0,
        nullable=False,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('personal', 'business')",
            name="workspace_kind_valid",
        ),
    )


class Account(db.Model):
    """Conta bancária, carteira ou caixa da empresa."""

    __tablename__ = "accounts"

    id = db.Column(db.Integer, primary_key=True)

    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id"),
        nullable=False,
        index=True,
    )

    name = db.Column(db.String(100), nullable=False)

    # Dinheiro será armazenado em centavos.
    # Exemplo: R$ 150,90 = 15090.
    opening_balance_cents = db.Column(
        db.BigInteger,
        default=0,
        nullable=False,
    )

    is_active = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "name",
            name="account_name_per_workspace",
        ),
        UniqueConstraint(
            "id",
            "workspace_id",
            name="account_id_workspace_unique",
        ),
    )


class Transaction(db.Model):
    """Receita ou despesa, realizada ou pendente."""

    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)

    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id"),
        nullable=False,
        index=True,
    )

    account_id = db.Column(
        db.Integer,
        nullable=False,
    )

    description = db.Column(
        db.String(200),
        nullable=False,
    )

    kind = db.Column(db.String(20), nullable=False)

    category = db.Column(
        db.String(80),
        nullable=False,
    )

    amount_cents = db.Column(
        db.BigInteger,
        nullable=False,
    )

    # Data prevista para receber ou pagar.
    due_date = db.Column(
        db.Date,
        nullable=False,
        index=True,
    )

    # Preenchida apenas quando o lançamento for realizado.
    paid_date = db.Column(
        db.Date,
        nullable=True,
        index=True,
    )

    status = db.Column(
        db.String(20),
        default="pending",
        nullable=False,
    )

    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    __table_args__ = (
        # Impede vincular um lançamento a uma conta
        # pertencente a outro espaço financeiro.
        db.ForeignKeyConstraint(
            ["account_id", "workspace_id"],
            ["accounts.id", "accounts.workspace_id"],
            name="transaction_account_workspace_fk",
        ),
        CheckConstraint(
            "amount_cents > 0",
            name="transaction_amount_positive",
        ),
        CheckConstraint(
            "kind IN ('income', 'expense')",
            name="transaction_kind_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'paid')",
            name="transaction_status_valid",
        ),
        CheckConstraint(
            "(status = 'paid' AND paid_date IS NOT NULL) "
            "OR (status = 'pending' AND paid_date IS NULL)",
            name="transaction_payment_date_valid",
        ),
        db.Index(
            "ix_transactions_workspace_due",
            "workspace_id",
            "due_date",
        ),
    )


class Budget(db.Model):
    """Limite de despesas por categoria e mês."""

    __tablename__ = "budgets"

    id = db.Column(db.Integer, primary_key=True)

    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id"),
        nullable=False,
        index=True,
    )

    # Sempre será o primeiro dia do mês.
    month = db.Column(db.Date, nullable=False)

    category = db.Column(
        db.String(80),
        nullable=False,
    )

    limit_cents = db.Column(
        db.BigInteger,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "month",
            "category",
            name="budget_category_per_month",
        ),
        CheckConstraint(
            "limit_cents > 0",
            name="budget_limit_positive",
        ),
    )


class Goal(db.Model):
    """Meta financeira com acompanhamento de reserva."""

    __tablename__ = "goals"

    id = db.Column(db.Integer, primary_key=True)

    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id"),
        nullable=False,
        index=True,
    )

    name = db.Column(db.String(120), nullable=False)

    target_cents = db.Column(
        db.BigInteger,
        nullable=False,
    )

    saved_cents = db.Column(
        db.BigInteger,
        default=0,
        nullable=False,
    )

    deadline = db.Column(db.Date, nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "target_cents > 0",
            name="goal_target_positive",
        ),
        CheckConstraint(
            "saved_cents >= 0",
            name="goal_saved_nonnegative",
        ),
    )
