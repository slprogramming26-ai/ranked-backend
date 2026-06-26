from pydantic_settings import BaseSettings
from pydantic import ConfigDict

class Settings(BaseSettings):
    database_hostname: str
    database_port: str
    database_passwort: str
    database_name: str
    database_username: str
    secret_key: str
    algorithm: str
    access_token_expire_minutes: int
    refresh_token_expire_days: int = 60
    s3_access_key: str
    s3_secret_key: str
    s3_region: str
    s3_endpoint: str
    supabase_url: str

    model_config = ConfigDict(env_file=".env")

settings = Settings()