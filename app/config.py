from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything from .env. Values that admins change at runtime live in the `settings` table instead."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    secret_key: str
    fernet_key: str = ""
    base_url: str = "http://localhost:8000"
    cookie_secure: bool = True
    display_tz: str = "Asia/Kolkata"
    upload_dir: str = "data/uploads"
    backup_dir: str = "data/backups"

    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    ses_configuration_set: str = ""
    sns_topic_arn: str = ""

    backup_s3_bucket: str = ""
    backup_s3_prefix: str = "pg-backups/"
    backup_aws_access_key_id: str = ""
    backup_aws_secret_access_key: str = ""

    reply_email: str = ""
    reply_imap_host: str = ""
    reply_imap_port: int = 993
    reply_smtp_host: str = ""
    reply_smtp_port: int = 465
    reply_password: str = ""

    alert_email: str = ""

    seed_admin_name: str = "Admin"
    seed_admin_email: str = ""
    seed_admin_password: str = ""


settings = Settings()
