import os
from datetime import timedelta


class Config:
    # No Render, vamos cadastrar essa chave nas variáveis de ambiente.
    SECRET_KEY = os.environ.get("SECRET_KEY")

    # Banco PostgreSQL do Render.
    database_url = os.environ.get("DATABASE_URL", "")

    if database_url.startswith("postgres://"):
        database_url = database_url.replace(
            "postgres://",
            "postgresql://",
            1,
        )

    # SQLite permite testar no computador antes de configurar o Render.
    SQLALCHEMY_DATABASE_URI = (
        database_url or "sqlite:///finance.db"
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Verifica a conexão antes de utilizar o banco.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    # Proteção dos cookies de acesso.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # No Render, usaremos APP_ENV=production para exigir HTTPS.
    SESSION_COOKIE_SECURE = (
        os.environ.get("APP_ENV") == "production"
    )

    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE

    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # Protege os formulários contra solicitações indevidas.
    WTF_CSRF_ENABLED = True

    # Limita o tamanho das requisições a 2 MB.
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024
