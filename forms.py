from datetime import date
from decimal import Decimal

from flask_wtf import FlaskForm
from wtforms import (
    DateField,
    DecimalField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    EqualTo,
    InputRequired,
    Length,
    NumberRange,
    Optional,
    ValidationError,
)


CATEGORIES = [
    ("Alimentação", "Alimentação"),
    ("Moradia", "Moradia"),
    ("Transporte", "Transporte"),
    ("Saúde", "Saúde"),
    ("Educação", "Educação"),
    ("Lazer", "Lazer"),
    ("Compras", "Compras"),
    ("Assinaturas", "Assinaturas"),
    ("Impostos", "Impostos"),
    ("Salário", "Salário"),
    ("Vendas", "Vendas"),
    ("Serviços", "Serviços"),
    ("Fornecedores", "Fornecedores"),
    ("Funcionários", "Funcionários"),
    ("Outros", "Outros"),
]


def clean_text(value):
    return value.strip() if isinstance(value, str) else value


def clean_email(value):
    return value.strip().lower() if isinstance(value, str) else value


def validate_email_format(form, field):
    value = field.data or ""

    if (
        value.count("@") != 1
        or any(character.isspace() for character in value)
    ):
        raise ValidationError("Digite um e-mail válido.")

    local, domain = value.split("@")

    if (
        not local
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
    ):
        raise ValidationError("Digite um e-mail válido.")


def validate_cents(form, field):
    value = field.data

    if value is None:
        return

    if not value.is_finite():
        raise ValidationError("Digite um valor válido.")

    if value != value.quantize(Decimal("0.01")):
        raise ValidationError("Use no máximo duas casas decimais.")


def money_field(label, minimum="0.01", default=None):
    return DecimalField(
        label,
        places=None,
        default=default,
        validators=[
            InputRequired(message="Informe o valor."),
            NumberRange(
                min=Decimal(minimum),
                max=Decimal("999999999.99"),
                message="Informe um valor dentro do limite permitido.",
            ),
            validate_cents,
        ],
        render_kw={
            "step": "0.01",
            "min": minimum,
            "max": "999999999.99",
        },
    )


class RegisterForm(FlaskForm):
    name = StringField(
        "Seu nome",
        filters=[clean_text],
        validators=[
            DataRequired(message="Informe seu nome."),
            Length(min=2, max=100),
        ],
    )

    email = StringField(
        "E-mail",
        filters=[clean_email],
        validators=[
            DataRequired(message="Informe seu e-mail."),
            Length(max=254),
            validate_email_format,
        ],
    )

    password = PasswordField(
        "Senha",
        validators=[
            DataRequired(message="Crie uma senha."),
            Length(
                min=10,
                max=128,
                message="Use uma senha entre 10 e 128 caracteres.",
            ),
        ],
    )

    confirm_password = PasswordField(
        "Confirmar senha",
        validators=[
            DataRequired(message="Confirme sua senha."),
            EqualTo(
                "password",
                message="As senhas não são iguais.",
            ),
        ],
    )

    submit = SubmitField("Criar minha conta")


class LoginForm(FlaskForm):
    email = StringField(
        "E-mail",
        filters=[clean_email],
        validators=[
            DataRequired(message="Informe seu e-mail."),
            Length(max=254),
            validate_email_format,
        ],
    )

    password = PasswordField(
        "Senha",
        validators=[
            DataRequired(message="Informe sua senha."),
            Length(max=128),
        ],
    )

    submit = SubmitField("Entrar")


class TransactionForm(FlaskForm):
    description = StringField(
        "Descrição",
        filters=[clean_text],
        validators=[
            DataRequired(message="Informe a descrição."),
            Length(max=200),
        ],
    )

    kind = SelectField(
        "Tipo",
        choices=[
            ("expense", "Despesa"),
            ("income", "Receita"),
        ],
        validators=[DataRequired()],
    )

    # As opções serão carregadas com as contas
    # do espaço financeiro que o usuário está acessando.
    account_id = SelectField(
        "Conta",
        coerce=int,
        validators=[InputRequired(message="Selecione uma conta.")],
    )

    category = SelectField(
        "Categoria",
        choices=CATEGORIES,
        validators=[DataRequired()],
    )

    amount = money_field("Valor (R$)")

    due_date = DateField(
        "Vencimento",
        default=date.today,
        validators=[
            DataRequired(message="Informe uma data válida."),
        ],
    )

    status = SelectField(
        "Situação",
        choices=[
            ("pending", "Pendente"),
            ("paid", "Realizado"),
        ],
        validators=[DataRequired()],
    )

    paid_date = DateField(
        "Data do pagamento ou recebimento",
        validators=[Optional()],
    )

    notes = TextAreaField(
        "Observações",
        filters=[clean_text],
        validators=[
            Optional(),
            Length(max=2000),
        ],
    )

    submit = SubmitField("Salvar lançamento")

    def validate_paid_date(self, field):
        if self.status.data == "paid" and not field.data:
            raise ValidationError(
                "Informe quando o pagamento ou recebimento aconteceu."
            )

        if field.data and field.data > date.today():
            raise ValidationError(
                "A data de realização não pode estar no futuro."
            )

        if self.status.data == "pending" and field.data:
            raise ValidationError(
                "Deixe a data de realização vazia para contas pendentes."
            )


class AccountForm(FlaskForm):
    name = StringField(
        "Nome da conta",
        filters=[clean_text],
        validators=[
            DataRequired(message="Informe o nome da conta."),
            Length(max=100),
        ],
    )

    opening_balance = money_field(
        "Saldo inicial (R$)",
        minimum="-999999999.99",
        default=Decimal("0.00"),
    )

    submit = SubmitField("Salvar conta")


class BudgetForm(FlaskForm):
    category = SelectField(
        "Categoria",
        choices=CATEGORIES,
        validators=[DataRequired()],
    )

    limit = money_field("Limite mensal (R$)")

    submit = SubmitField("Salvar orçamento")


class GoalForm(FlaskForm):
    name = StringField(
        "Nome da meta",
        filters=[clean_text],
        validators=[
            DataRequired(message="Informe o nome da meta."),
            Length(max=120),
        ],
    )

    target = money_field("Valor desejado (R$)")

    saved = money_field(
        "Valor já reservado (R$)",
        minimum="0",
        default=Decimal("0.00"),
    )

    deadline = DateField(
        "Prazo da meta",
        validators=[Optional()],
    )

    submit = SubmitField("Salvar meta")


class ActionForm(FlaskForm):
    """Proteção para ações como excluir e sair da conta."""

    submit = SubmitField("Confirmar")
