import calendar
import csv
import io
import os

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import click

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from config import Config
from forms import (
    AccountForm,
    ActionForm,
    BudgetForm,
    GoalForm,
    LoginForm,
    RegisterForm,
    TransactionForm,
)
from models import (
    Account,
    Budget,
    Goal,
    Transaction,
    User,
    Workspace,
    db,
)


app = Flask(__name__)
app.config.from_object(Config)

if not app.config.get("SECRET_KEY"):
    raise RuntimeError(
        "Configure a variável de ambiente SECRET_KEY."
    )

db.init_app(app)

csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Entre para acessar seu sistema."
login_manager.login_message_category = "info"

TIMEZONE = ZoneInfo("America/Sao_Paulo")


# --------------------------------------------------
# FUNÇÕES AUXILIARES
# --------------------------------------------------

def today_br():
    return datetime.now(TIMEZONE).date()


def to_cents(value):
    return int(Decimal(value) * 100)


@app.template_filter("brl")
def format_brl(cents):
    value = Decimal(cents or 0) / Decimal("100")
    formatted = f"{value:,.2f}"

    formatted = (
        formatted.replace(",", "_")
        .replace(".", ",")
        .replace("_", ".")
    )

    return f"R$ {formatted}"


@app.template_filter("date_br")
def format_date(value):
    return value.strftime("%d/%m/%Y") if value else "—"


def selected_month():
    value = request.args.get("month")

    if not value:
        return today_br().replace(day=1)

    try:
        parsed = datetime.strptime(value, "%Y-%m").date()

        if parsed.strftime("%Y-%m") != value:
            raise ValueError

        if parsed.year < 1900 or parsed.year > 9998:
            raise ValueError

        return parsed.replace(day=1)

    except ValueError:
        abort(400, description="Mês inválido.")


def month_bounds(month):
    last_day = calendar.monthrange(
        month.year,
        month.month,
    )[1]

    return month, month.replace(day=last_day)


def shift_month(month, offset):
    total = month.year * 12 + month.month - 1 + offset
    year, month_index = divmod(total, 12)

    return date(year, month_index + 1, 1)


def owned_workspace():
    workspace_id = session.get("workspace_id")
    workspace = None

    if isinstance(workspace_id, int):
        workspace = db.session.scalar(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.user_id == current_user.id,
            )
        )

    if workspace is None:
        workspace = db.session.scalar(
            select(Workspace)
            .where(Workspace.user_id == current_user.id)
            .order_by(Workspace.id)
        )

    if workspace is None:
        abort(403, description="Espaço financeiro indisponível.")

    session["workspace_id"] = workspace.id

    return workspace


def owned_record(model, record_id, workspace):
    record = db.session.scalar(
        select(model).where(
            model.id == record_id,
            model.workspace_id == workspace.id,
        )
    )

    if record is None:
        abort(404)

    return record


def bump_revision(workspace):
    db.session.execute(
        update(Workspace)
        .where(Workspace.id == workspace.id)
        .values(revision=Workspace.revision + 1)
    )


def commit_change(workspace):
    bump_revision(workspace)
    db.session.commit()


def active_accounts(workspace):
    return db.session.scalars(
        select(Account)
        .where(
            Account.workspace_id == workspace.id,
            Account.is_active.is_(True),
        )
        .order_by(Account.name)
    ).all()


def account_choices(workspace, current_account_id=None):
    accounts = db.session.scalars(
        select(Account)
        .where(Account.workspace_id == workspace.id)
        .order_by(Account.name)
    ).all()

    return [
        (
            account.id,
            account.name
            + ("" if account.is_active else " (arquivada)"),
        )
        for account in accounts
        if account.is_active or account.id == current_account_id
    ]


def transaction_total(
    workspace,
    kind,
    start=None,
    end=None,
    status="paid",
    category=None,
):
    statement = select(
        func.coalesce(func.sum(Transaction.amount_cents), 0)
    ).where(
        Transaction.workspace_id == workspace.id,
        Transaction.kind == kind,
        Transaction.status == status,
    )

    date_column = (
        Transaction.paid_date
        if status == "paid"
        else Transaction.due_date
    )

    if start is not None:
        statement = statement.where(date_column >= start)

    if end is not None:
        statement = statement.where(date_column <= end)

    if category is not None:
        statement = statement.where(
            Transaction.category == category
        )

    return int(db.session.scalar(statement) or 0)


def serialize_transaction(item):
    return {
        "id": item.id,
        "description": item.description,
        "kind": item.kind,
        "category": item.category,
        "amount_cents": item.amount_cents,
        "account_id": item.account_id,
        "due_date": item.due_date.isoformat(),
        "paid_date": (
            item.paid_date.isoformat()
            if item.paid_date
            else None
        ),
        "status": item.status,
        "notes": item.notes or "",
    }


def dashboard_data(workspace, month):
    start, end = month_bounds(month)

    income = transaction_total(
        workspace, "income", start, end
    )

    expense = transaction_total(
        workspace, "expense", start, end
    )

    opening_balance = int(
        db.session.scalar(
            select(
                func.coalesce(
                    func.sum(Account.opening_balance_cents),
                    0,
                )
            ).where(Account.workspace_id == workspace.id)
        )
        or 0
    )

    balance = (
        opening_balance
        + transaction_total(workspace, "income", end=end)
        - transaction_total(workspace, "expense", end=end)
    )

    pending_income = transaction_total(
        workspace,
        "income",
        start,
        end,
        status="pending",
    )

    pending_expense = transaction_total(
        workspace,
        "expense",
        start,
        end,
        status="pending",
    )

    overdue_expense = int(
        db.session.scalar(
            select(
                func.coalesce(
                    func.sum(Transaction.amount_cents),
                    0,
                )
            ).where(
                Transaction.workspace_id == workspace.id,
                Transaction.kind == "expense",
                Transaction.status == "pending",
                Transaction.due_date < today_br(),
            )
        )
        or 0
    )

    chart = []

    for offset in range(-5, 1):
        chart_month = shift_month(month, offset)
        chart_start, chart_end = month_bounds(chart_month)

        chart.append({
            "month": chart_month.strftime("%Y-%m"),
            "income_cents": transaction_total(
                workspace,
                "income",
                chart_start,
                chart_end,
            ),
            "expense_cents": transaction_total(
                workspace,
                "expense",
                chart_start,
                chart_end,
            ),
        })

    goals = db.session.scalars(
        select(Goal)
        .where(Goal.workspace_id == workspace.id)
        .order_by(Goal.id.desc())
    ).all()

    budgets = db.session.scalars(
        select(Budget)
        .where(
            Budget.workspace_id == workspace.id,
            Budget.month == month,
        )
        .order_by(Budget.category)
    ).all()

    recent = db.session.scalars(
        select(Transaction)
        .where(Transaction.workspace_id == workspace.id)
        .order_by(
            Transaction.updated_at.desc(),
            Transaction.id.desc(),
        )
        .limit(8)
    ).all()

    return {
        "workspace_id": workspace.id,
        "revision": workspace.revision,
        "month": month.strftime("%Y-%m"),
        "balance_cents": balance,
        "income_cents": income,
        "expense_cents": expense,
        "result_cents": income - expense,
        "pending_income_cents": pending_income,
        "pending_expense_cents": pending_expense,
        "overdue_expense_cents": overdue_expense,
        "savings_rate": (
            round((income - expense) / income * 100, 1)
            if income
            else None
        ),
        "chart": chart,
        "recent": [
            serialize_transaction(item)
            for item in recent
        ],
        "goals": [
            {
                "id": goal.id,
                "name": goal.name,
                "target_cents": goal.target_cents,
                "saved_cents": goal.saved_cents,
                "deadline": (
                    goal.deadline.isoformat()
                    if goal.deadline
                    else None
                ),
            }
            for goal in goals
        ],
        "budgets": [
            {
                "id": budget.id,
                "category": budget.category,
                "limit_cents": budget.limit_cents,
                "spent_cents": transaction_total(
                    workspace,
                    "expense",
                    start,
                    end,
                    category=budget.category,
                ),
            }
            for budget in budgets
        ],
    }


def render_form(title, form, back_endpoint):
    return render_template(
        "form.html",
        title=title,
        form=form,
        back_url=url_for(back_endpoint),
    )


# --------------------------------------------------
# LOGIN E CONTEXTO DAS TELAS
# --------------------------------------------------

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


@app.context_processor
def template_context():
    context = {
        "action_form": ActionForm(),
        "current_year": today_br().year,
    }

    if current_user.is_authenticated:
        context["workspaces"] = db.session.scalars(
            select(Workspace)
            .where(Workspace.user_id == current_user.id)
            .order_by(Workspace.id)
        ).all()

        context["workspace"] = owned_workspace()

    return context


@app.after_request
def response_headers(response):
    if current_user.is_authenticated:
        response.headers["Cache-Control"] = "no-store"

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = (
        "strict-origin-when-cross-origin"
    )

    return response


@app.errorhandler(CSRFError)
def csrf_error(error):
    if request.path.startswith("/api/"):
        return jsonify({
            "error": "Sessão expirada. Atualize a página."
        }), 400

    return (
        "Sessão do formulário expirada. "
        "Volte, atualize a página e tente novamente.",
        400,
    )


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


@app.route("/cadastro", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    form = RegisterForm()

    if form.validate_on_submit():
        user = User(
            name=form.name.data,
            email=form.email.data,
        )

        user.set_password(form.password.data)

        try:
            db.session.add(user)
            db.session.flush()

            personal = Workspace(
                user_id=user.id,
                name="Minhas finanças",
                kind="personal",
            )

            business = Workspace(
                user_id=user.id,
                name="Minha empresa",
                kind="business",
            )

            db.session.add_all([personal, business])
            db.session.flush()

            db.session.add_all([
                Account(
                    workspace_id=personal.id,
                    name="Conta principal",
                ),
                Account(
                    workspace_id=business.id,
                    name="Caixa da empresa",
                ),
            ])

            db.session.commit()

        except IntegrityError:
            db.session.rollback()

            form.email.errors.append(
                "Não foi possível cadastrar esse e-mail. "
                "Se já possui conta, tente entrar."
            )

        else:
            session.clear()
            login_user(user)
            session.permanent = True
            session["workspace_id"] = personal.id

            return redirect(url_for("dashboard"))

    return render_template(
        "register.html",
        form=form,
        title="Criar conta",
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    form = LoginForm()

    if form.validate_on_submit():
        user = db.session.scalar(
            select(User).where(
                User.email == form.email.data
            )
        )

        if user and user.check_password(form.password.data):
            session.clear()
            login_user(user)
            session.permanent = True

            return redirect(url_for("dashboard"))

        flash("E-mail ou senha inválidos.", "error")

    return render_template(
        "login.html",
        form=form,
        title="Entrar",
    )


@app.post("/sair")
@login_required
def logout():
    form = ActionForm()

    if not form.validate_on_submit():
        abort(400)

    logout_user()
    session.clear()

    return redirect(url_for("login"))


@app.post("/espaco/<int:workspace_id>")
@login_required
def switch_workspace(workspace_id):
    form = ActionForm()

    if not form.validate_on_submit():
        abort(400)

    workspace = db.session.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.user_id == current_user.id,
        )
    )

    if workspace is None:
        abort(404)

    session["workspace_id"] = workspace.id

    return redirect(url_for("dashboard"))


# --------------------------------------------------
# PAINEL E ATUALIZAÇÃO DOS DADOS
# --------------------------------------------------

@app.get("/dashboard")
@login_required
def dashboard():
    workspace = owned_workspace()
    month = selected_month()

    return render_template(
        "dashboard.html",
        title="Visão geral",
        month=month.strftime("%Y-%m"),
        summary=dashboard_data(workspace, month),
    )


@app.get("/api/dashboard")
@login_required
def dashboard_api():
    workspace = owned_workspace()

    return jsonify(
        dashboard_data(workspace, selected_month())
    )


# --------------------------------------------------
# LANÇAMENTOS
# --------------------------------------------------

@app.get("/lancamentos")
@login_required
def transactions():
    workspace = owned_workspace()
    month = selected_month()
    start, end = month_bounds(month)

    query = request.args.get("q", "").strip()[:200]
    kind = request.args.get("kind", "")
    status = request.args.get("status", "")

    statement = select(Transaction).where(
        Transaction.workspace_id == workspace.id,
        Transaction.due_date >= start,
        Transaction.due_date <= end,
    )

    if query:
        statement = statement.where(
            Transaction.description.ilike(
                f"%{query}%",
                escape="\\",
            )
        )

    if kind in ("income", "expense"):
        statement = statement.where(
            Transaction.kind == kind
        )

    if status in ("pending", "paid"):
        statement = statement.where(
            Transaction.status == status
        )

    statement = statement.order_by(
        Transaction.due_date.desc(),
        Transaction.id.desc(),
    )

    page_number = request.args.get("page", 1, type=int)

    pagination = db.paginate(
        statement,
        page=max(1, page_number or 1),
        per_page=30,
        error_out=False,
    )

    accounts = db.session.scalars(
        select(Account).where(
            Account.workspace_id == workspace.id
        )
    ).all()

    return render_template(
        "transactions.html",
        title="Lançamentos",
        pagination=pagination,
        items=pagination.items,
        accounts={account.id: account for account in accounts},
        month=month.strftime("%Y-%m"),
        q=query,
        kind=kind,
        status=status,
    )


@app.route(
    "/lancamentos/novo",
    methods=["GET", "POST"],
)
@app.route(
    "/lancamentos/<int:transaction_id>/editar",
    methods=["GET", "POST"],
)
@login_required
def transaction_form(transaction_id=None):
    workspace = owned_workspace()
    item = None

    if transaction_id is not None:
        item = owned_record(
            Transaction,
            transaction_id,
            workspace,
        )

    form = TransactionForm()

    form.account_id.choices = account_choices(
        workspace,
        item.account_id if item else None,
    )

    if request.method == "GET":
        if item:
            form.description.data = item.description
            form.kind.data = item.kind
            form.account_id.data = item.account_id
            form.category.data = item.category
            form.amount.data = (
                Decimal(item.amount_cents) / 100
            )
            form.due_date.data = item.due_date
            form.status.data = item.status
            form.paid_date.data = item.paid_date
            form.notes.data = item.notes

        else:
            form.due_date.data = today_br()

            if form.account_id.choices:
                form.account_id.data = (
                    form.account_id.choices[0][0]
                )

    if form.validate_on_submit():
        account = owned_record(
            Account,
            form.account_id.data,
            workspace,
        )

        if not account.is_active:
            if item is None or account.id != item.account_id:
                abort(400, description="Conta arquivada.")

        if item is None:
            item = Transaction(workspace_id=workspace.id)
            db.session.add(item)

        item.description = form.description.data
        item.kind = form.kind.data
        item.account_id = account.id
        item.category = form.category.data
        item.amount_cents = to_cents(form.amount.data)
        item.due_date = form.due_date.data
        item.status = form.status.data
        item.paid_date = form.paid_date.data
        item.notes = form.notes.data

        commit_change(workspace)

        flash("Lançamento salvo.", "success")

        return redirect(url_for(
            "transactions",
            month=item.due_date.strftime("%Y-%m"),
        ))

    return render_form(
        "Editar lançamento" if item else "Novo lançamento",
        form,
        "transactions",
    )


@app.post("/lancamentos/<int:transaction_id>/excluir")
@login_required
def delete_transaction(transaction_id):
    workspace = owned_workspace()
    form = ActionForm()

    if not form.validate_on_submit():
        abort(400)

    item = owned_record(
        Transaction,
        transaction_id,
        workspace,
    )

    month = item.due_date.strftime("%Y-%m")

    db.session.delete(item)
    commit_change(workspace)

    flash("Lançamento excluído.", "success")

    return redirect(url_for("transactions", month=month))


# --------------------------------------------------
# CONTAS
# --------------------------------------------------

@app.get("/contas")
@login_required
def accounts():
    workspace = owned_workspace()

    items = db.session.scalars(
        select(Account)
        .where(Account.workspace_id == workspace.id)
        .order_by(Account.name)
    ).all()

    return render_template(
        "accounts.html",
        title="Minhas contas",
        items=items,
    )


@app.route("/contas/nova", methods=["GET", "POST"])
@app.route(
    "/contas/<int:account_id>/editar",
    methods=["GET", "POST"],
)
@login_required
def account_form(account_id=None):
    workspace = owned_workspace()
    item = None

    if account_id is not None:
        item = owned_record(Account, account_id, workspace)

    form = AccountForm()

    if request.method == "GET" and item:
        form.name.data = item.name
        form.opening_balance.data = (
            Decimal(item.opening_balance_cents) / 100
        )

    if form.validate_on_submit():
        if item is None:
            item = Account(workspace_id=workspace.id)
            db.session.add(item)

        item.name = form.name.data
        item.opening_balance_cents = to_cents(
            form.opening_balance.data
        )

        try:
            commit_change(workspace)

        except IntegrityError:
            db.session.rollback()

            form.name.errors.append(
                "Já existe uma conta com esse nome."
            )

        else:
            flash("Conta salva.", "success")
            return redirect(url_for("accounts"))

    return render_form(
        "Editar conta" if item else "Nova conta",
        form,
        "accounts",
    )


@app.post("/contas/<int:account_id>/alternar")
@login_required
def toggle_account(account_id):
    workspace = owned_workspace()
    form = ActionForm()

    if not form.validate_on_submit():
        abort(400)

    account = owned_record(Account, account_id, workspace)
    account.is_active = not account.is_active

    commit_change(workspace)

    flash("Situação da conta atualizada.", "success")

    return redirect(url_for("accounts"))


# --------------------------------------------------
# ORÇAMENTO
# --------------------------------------------------

@app.route("/planejamento", methods=["GET", "POST"])
@login_required
def budgets():
    workspace = owned_workspace()
    month = selected_month()
    form = BudgetForm()

    if form.validate_on_submit():
        budget = db.session.scalar(
            select(Budget).where(
                Budget.workspace_id == workspace.id,
                Budget.month == month,
                Budget.category == form.category.data,
            )
        )

        if budget is None:
            budget = Budget(
                workspace_id=workspace.id,
                month=month,
                category=form.category.data,
            )
            db.session.add(budget)

        budget.limit_cents = to_cents(form.limit.data)

        try:
            commit_change(workspace)

        except IntegrityError:
            db.session.rollback()

            flash(
                "O orçamento foi alterado em outra sessão. "
                "Tente salvar novamente.",
                "error",
            )

        else:
            flash("Orçamento salvo.", "success")

        return redirect(url_for(
            "budgets",
            month=month.strftime("%Y-%m"),
        ))

    summary = dashboard_data(workspace, month)

    return render_template(
        "budgets.html",
        title="Planejamento mensal",
        form=form,
        items=summary["budgets"],
        month=month.strftime("%Y-%m"),
    )


@app.post("/planejamento/<int:budget_id>/excluir")
@login_required
def delete_budget(budget_id):
    workspace = owned_workspace()
    form = ActionForm()

    if not form.validate_on_submit():
        abort(400)

    budget = owned_record(Budget, budget_id, workspace)
    month = budget.month.strftime("%Y-%m")

    db.session.delete(budget)
    commit_change(workspace)

    flash("Limite removido.", "success")

    return redirect(url_for("budgets", month=month))


# --------------------------------------------------
# METAS
# --------------------------------------------------

@app.get("/metas")
@login_required
def goals():
    workspace = owned_workspace()

    items = db.session.scalars(
        select(Goal)
        .where(Goal.workspace_id == workspace.id)
        .order_by(Goal.id.desc())
    ).all()

    return render_template(
        "goals.html",
        title="Minhas metas",
        items=items,
    )


@app.route("/metas/nova", methods=["GET", "POST"])
@app.route(
    "/metas/<int:goal_id>/editar",
    methods=["GET", "POST"],
)
@login_required
def goal_form(goal_id=None):
    workspace = owned_workspace()
    item = None

    if goal_id is not None:
        item = owned_record(Goal, goal_id, workspace)

    form = GoalForm()

    if request.method == "GET" and item:
        form.name.data = item.name
        form.target.data = Decimal(item.target_cents) / 100
        form.saved.data = Decimal(item.saved_cents) / 100
        form.deadline.data = item.deadline

    if form.validate_on_submit():
        if item is None:
            item = Goal(workspace_id=workspace.id)
            db.session.add(item)

        item.name = form.name.data
        item.target_cents = to_cents(form.target.data)
        item.saved_cents = to_cents(form.saved.data)
        item.deadline = form.deadline.data

        commit_change(workspace)

        flash("Meta salva.", "success")

        return redirect(url_for("goals"))

    return render_form(
        "Editar meta" if item else "Nova meta",
        form,
        "goals",
    )


@app.post("/metas/<int:goal_id>/excluir")
@login_required
def delete_goal(goal_id):
    workspace = owned_workspace()
    form = ActionForm()

    if not form.validate_on_submit():
        abort(400)

    item = owned_record(Goal, goal_id, workspace)

    db.session.delete(item)
    commit_change(workspace)

    flash("Meta excluída.", "success")

    return redirect(url_for("goals"))


# --------------------------------------------------
# RELATÓRIOS E EXPORTAÇÃO
# --------------------------------------------------

@app.get("/relatorios")
@login_required
def reports():
    workspace = owned_workspace()
    month = selected_month()

    return render_template(
        "reports.html",
        title="Relatórios",
        month=month.strftime("%Y-%m"),
        summary=dashboard_data(workspace, month),
    )


def csv_safe(value):
    text = str(value)

    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text

    return text


@app.get("/relatorios/exportar")
@login_required
def export_csv():
    workspace = owned_workspace()
    month = selected_month()
    start, end = month_bounds(month)

    items = db.session.scalars(
        select(Transaction)
        .where(
            Transaction.workspace_id == workspace.id,
            Transaction.due_date >= start,
            Transaction.due_date <= end,
        )
        .order_by(Transaction.due_date, Transaction.id)
    ).all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    writer.writerow([
        "Descrição",
        "Tipo",
        "Categoria",
        "Valor (R$)",
        "Vencimento",
        "Realização",
        "Situação",
    ])

    for item in items:
        amount = (
            f"{Decimal(item.amount_cents) / 100:.2f}"
            .replace(".", ",")
        )

        writer.writerow([
            csv_safe(item.description),
            "Receita" if item.kind == "income" else "Despesa",
            csv_safe(item.category),
            amount,
            format_date(item.due_date),
            format_date(item.paid_date),
            "Realizado" if item.status == "paid" else "Pendente",
        ])

    response = make_response(
        ("\ufeff" + output.getvalue()).encode("utf-8")
    )

    response.headers["Content-Type"] = (
        "text/csv; charset=utf-8"
    )

    response.headers["Content-Disposition"] = (
        'attachment; filename="finance-'
        f'{month:%Y-%m}.csv"'
    )

    return response


# --------------------------------------------------
# SAÚDE DO SERVIÇO E INICIALIZAÇÃO
# --------------------------------------------------

@app.get("/health")
def health():
    try:
        db.session.execute(select(1))
    except Exception:
        db.session.rollback()
        app.logger.exception("Falha na conexão com o banco.")

        return jsonify({"status": "unavailable"}), 503

    return jsonify({"status": "ok"})


@app.cli.command("init-db")
def init_db():
    """Cria as tabelas que ainda não existem."""
    db.create_all()
    click.echo("Banco de dados inicializado.")


# --------------------------------------------------
# ADMINISTRAÇÃO DA PLATAFORMA
# --------------------------------------------------

# Carrega o modelo de bloqueios e registra as rotas
# administrativas antes de iniciar o aplicativo.
from admin import init_admin

init_admin(app)


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
