import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# Load .env from backend directory
backend_dir = Path(__file__).parent
env_file = backend_dir / ".env"
load_dotenv(env_file)


@dataclass
class Settings:
    app_env: str = os.getenv("APP_ENV", "dev")
    app_port: int = int(os.getenv("APP_PORT", "8000"))
    frontend_origin: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
    public_backend_url: str = os.getenv("PUBLIC_BACKEND_URL", "")
    oauth_redirect_base_url: str = os.getenv("OAUTH_REDIRECT_BASE_URL", "")
    token_storage_dir: str = os.getenv("TOKEN_STORAGE_DIR", str(backend_dir / "logs"))
    # Prefer Snowflake SQL API (PAT) over MCP tools/call when true (default on).
    snowflake_use_sql_api: bool = os.getenv("SNOWFLAKE_USE_SQL_API", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

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

    table_hints: str = os.getenv(
        "SNOWFLAKE_TABLE_HINTS",
        "NETFLIX_TABLE(show_id, type, title, director, cast, country, date_added, "
        "release_year, rating, duration, listed_in, description); "
        "customers(customer_name, revenue, region); "
        "orders(order_id, customer_name, order_date, sales_amount)",
    )

    # ── Salesforce Hosted MCP Server ──
    salesforce_instance_url: str = os.getenv("SALESFORCE_INSTANCE_URL", "")
    salesforce_consumer_key: str = os.getenv("SALESFORCE_CONSUMER_KEY", "")
    salesforce_mcp_server_url: str = os.getenv(
        "SALESFORCE_MCP_SERVER_URL",
        "https://api.salesforce.com/platform/mcp/v1/platform/sobject-all",
    )
    salesforce_object_hints: str = os.getenv(
        "SALESFORCE_OBJECT_HINTS",
        "Account(Id, Name, Industry, Type, Phone, Website, BillingCity, BillingState); "
        "Contact(Id, FirstName, LastName, Email, Phone, AccountId, Title, Department); "
        "Opportunity(Id, Name, StageName, Amount, CloseDate, AccountId, Probability); "
        "Case(Id, Subject, Status, Priority, ContactId, AccountId, Description); "
        "Lead(Id, FirstName, LastName, Company, Status, Email, Phone, Industry); "
        "Task(Id, Subject, Status, Priority, WhoId, WhatId, ActivityDate)",
    )

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


settings = Settings()
