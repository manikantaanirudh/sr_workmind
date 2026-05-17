import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# Load .env from backend directory
backend_dir = Path(__file__).parent
env_file = backend_dir / ".env"
load_dotenv(env_file)


def _clean_env(value: str) -> str:
    """Strip whitespace and surrounding quotes (common when pasting into Render UI)."""
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


@dataclass
class Settings:
    app_env: str = os.getenv("APP_ENV", "dev")
    app_port: int = int(os.getenv("APP_PORT", "8000"))
    frontend_origin: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
    public_backend_url: str = os.getenv("PUBLIC_BACKEND_URL", "")
    oauth_redirect_base_url: str = os.getenv("OAUTH_REDIRECT_BASE_URL", "")
    token_storage_dir: str = os.getenv("TOKEN_STORAGE_DIR", str(backend_dir / "logs"))
    # Prefer MCP tools/call; fall back to SQL API (same PAT) when sql_exec_tool fails on account.
    snowflake_use_sql_api: bool = os.getenv("SNOWFLAKE_USE_SQL_API", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    snowflake_mcp_sql_api_fallback: bool = os.getenv(
        "SNOWFLAKE_MCP_SQL_API_FALLBACK", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}

    llm_provider: str = os.getenv("LLM_PROVIDER", "ollama")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    huggingface_api_key: str = os.getenv("HUGGINGFACE_API_KEY", "")
    huggingface_model: str = os.getenv("HUGGINGFACE_MODEL", "mistralai/Mistral-7B-Instruct-v0.3")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # Snowflake account identifiers (still needed for MCP endpoint URL)
    snowflake_account: str = os.getenv("SNOWFLAKE_ACCOUNT", "")
    snowflake_database: str = os.getenv("SNOWFLAKE_DATABASE", "")
    snowflake_schema: str = os.getenv("SNOWFLAKE_SCHEMA", "")

    # Programmatic Access Token (PAT) for MCP Server authentication
    snowflake_pat: str = os.getenv("SNOWFLAKE_PAT", "")
    
    # Credentials for native connector fallback
    snowflake_user: str = os.getenv("SNOWFLAKE_USER", "")
    snowflake_password: str = os.getenv("SNOWFLAKE_PASSWORD", "")

    # MCP Server configuration
    mcp_server_name: str = os.getenv("MCP_SERVER_NAME", "srworkmind_mcp_server")
    mcp_tool_name: str = os.getenv("MCP_TOOL_NAME", "sql_exec_tool")

    # Legacy fields kept for LLM SQL generation context only
    snowflake_warehouse: str = os.getenv("SNOWFLAKE_WAREHOUSE", "")
    snowflake_role: str = os.getenv("SNOWFLAKE_ROLE", "")

    # Optional override; when empty, schema is discovered via Snowflake MCP + INFORMATION_SCHEMA.
    table_hints: str = os.getenv("SNOWFLAKE_TABLE_HINTS", "")

    # ── Salesforce Hosted MCP Server ──
    salesforce_instance_url: str = os.getenv("SALESFORCE_INSTANCE_URL", "")
    salesforce_consumer_key: str = os.getenv("SALESFORCE_CONSUMER_KEY", "")
    salesforce_mcp_server_url: str = os.getenv(
        "SALESFORCE_MCP_SERVER_URL",
        "https://api.salesforce.com/platform/mcp/v1/platform/sobject-all",
    )
    # Optional override; when empty, schema is discovered via Salesforce MCP getObjectSchema.
    salesforce_object_hints: str = os.getenv("SALESFORCE_OBJECT_HINTS", "")

    # Docusign Fully Managed MCP Server (Beta)
    docusign_client_id: str = os.getenv("DOCUSIGN_CLIENT_ID", "")
    docusign_client_secret: str = os.getenv("DOCUSIGN_CLIENT_SECRET", "")
    docusign_oauth_base_url: str = os.getenv(
        "DOCUSIGN_OAUTH_BASE_URL",
        "https://account-d.docusign.com",
    )
    docusign_mcp_server_url: str = os.getenv(
        "DOCUSIGN_MCP_SERVER_URL",
        "https://mcp-d.docusign.com/mcp",
    )
    docusign_mcp_resource: str = os.getenv(
        "DOCUSIGN_MCP_RESOURCE",
        "https://mcp-d.docusign.com/mcp",
    )
    docusign_scopes: str = os.getenv(
        "DOCUSIGN_SCOPES",
        "signature extended agreement_object_model_read aow_manage",
    )
    docusign_require_human_approval: bool = (
        os.getenv("DOCUSIGN_REQUIRE_HUMAN_APPROVAL", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    def __post_init__(self) -> None:
        self.snowflake_pat = _clean_env(self.snowflake_pat)
        self.snowflake_account = _clean_env(self.snowflake_account)
        self.snowflake_database = _clean_env(self.snowflake_database)
        self.snowflake_schema = _clean_env(self.snowflake_schema)
        self.snowflake_warehouse = _clean_env(self.snowflake_warehouse)
        self.snowflake_role = _clean_env(self.snowflake_role)
        self.groq_api_key = _clean_env(self.groq_api_key)
        self.salesforce_consumer_key = _clean_env(self.salesforce_consumer_key)
        self.frontend_origin = _clean_env(self.frontend_origin)

    @property
    def cors_origins(self) -> list[str]:
        """Origins allowed for browser calls to the backend API."""
        origins = {
            self.frontend_origin,
            "http://127.0.0.1:3000",
            "http://localhost:3000",
            "https://sr-workmind-frontend.onrender.com",
        }
        extra = os.getenv("CORS_EXTRA_ORIGINS", "")
        for item in extra.split(","):
            item = item.strip()
            if item:
                origins.add(item)
        return [o for o in origins if o]


settings = Settings()
