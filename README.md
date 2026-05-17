# SR WorkMind - Snowflake MCP Server Edition

End-to-end pipeline using **Snowflake's official Managed MCP Server** (Model Context Protocol):

```
User Prompt -> Intent Classifier -> LLM SQL Generator -> MCP Client (JSON-RPC) -> Snowflake MCP Server -> Result -> UI Response
```

## Architecture

### MCP Client (Backend — `backend/`)

- `backend/main.py` -> FastAPI API layer (`POST /execute`)
- `backend/intent/classifier.py` -> intent/platform detection (rule-first)
- `backend/model/sql_generator.py` -> SQL generation (rule-first + LLM fallback)
- `backend/model/llm_clients.py` -> Ollama / Groq / HuggingFace / Gemini clients
- `backend/mcp/mcp_client.py` -> **Standardized MCP Client** (JSON-RPC 2.0 over HTTPS)
- `backend/mcp/executor.py` -> governance logging + MCP routing
- `backend/response/formatter.py` -> human-friendly response message
- `backend/schemas.py` -> request/response contracts
- `backend/config.py` -> env-driven settings

### MCP Server (Snowflake-Managed)

The MCP Server runs inside Snowflake as a managed object (`CREATE MCP SERVER`).
It exposes a `SYSTEM_EXECUTE_SQL` tool that your backend invokes via standard
`tools/call` JSON-RPC requests.

### Frontend

- `src/app/page.tsx` calls backend `/execute` on `Execute`
- AI Thinking Panel shows real MCP Server execution details
- Generated SQL is displayed
- Result table is rendered from Snowflake query output
- Audit rows include expandable execution details

## Security and Governance

- **MCP Server RBAC**: Access control is enforced by Snowflake's role-based access
- **PAT/OAuth Authentication**: Bearer token auth on every MCP request
- **Server-Side Validation**: The MCP Server's tool config controls read/write access
- **Local Audit Log**: Every prompt + SQL is logged to `backend/logs/mcp_audit.log`
- **LLM generates SQL only**: Snowflake execution is isolated in the MCP Server layer

## Setup

### 1) Snowflake — Create MCP Server

Run these SQL commands in your Snowflake worksheet:

```sql
USE DATABASE netflix_db;
USE SCHEMA PUBLIC;

CREATE OR REPLACE MCP SERVER srworkmind_mcp_server
  FROM SPECIFICATION $$
  tools:
    - title: "SQL Execution Tool"
      name: "sql_exec_tool"
      type: "SYSTEM_EXECUTE_SQL"
      description: "Execute SQL queries against the Snowflake database for SR WorkMind."
      config:
        read_only: false
        query_timeout: 120
        warehouse: "COMPUTE_WH"
  $$;

-- Verify
DESCRIBE MCP SERVER srworkmind_mcp_server;
```

### 2) Snowflake — Generate a PAT

1. Go to Snowflake → Admin → Users → Your User
2. Generate a Programmatic Access Token (PAT)
3. Copy the token value

### 3) Frontend

```bash
npm install
```

Create `.env.local` in project root:

```env
BACKEND_API_BASE_URL=http://127.0.0.1:8000
```

### 4) Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Update `backend/.env` with your Snowflake account, PAT, and LLM provider:

```env
SNOWFLAKE_ACCOUNT=YOUR_ORG-YOUR_ACCOUNT
SNOWFLAKE_DATABASE=netflix_db
SNOWFLAKE_SCHEMA=PUBLIC
SNOWFLAKE_PAT=your-programmatic-access-token-here
MCP_SERVER_NAME=srworkmind_mcp_server
MCP_TOOL_NAME=sql_exec_tool
```

If you want Salesforce or DocuSign enabled locally, also set:

```env
PUBLIC_BACKEND_URL=http://127.0.0.1:8000
FRONTEND_ORIGIN=http://localhost:3000
TOKEN_STORAGE_DIR=backend/logs
LLM_PROVIDER=groq
GROQ_API_KEY=your-groq-api-key
SALESFORCE_INSTANCE_URL=your-salesforce-instance-url
SALESFORCE_CONSUMER_KEY=your-salesforce-connected-app-client-id
DOCUSIGN_CLIENT_ID=your-docusign-client-id
DOCUSIGN_CLIENT_SECRET=your-docusign-client-secret
```

### 5) Run backend API

From project root:

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

### 6) Run frontend UI

From project root in another terminal:

```bash
npm run dev
```

Open:

- Frontend: `http://localhost:3000`
- Backend health: `http://127.0.0.1:8000/health`

## Render Deployment

This repo ships a **Render Blueprint** (`render.yaml`) that creates two web services:

| Service | Name | Stack |
|---------|------|-------|
| Backend | `sr-workmind-backend` | Python / FastAPI (`backend/`) |
| Frontend | `sr-workmind-frontend` | Node / Next.js (repo root) |

The UI talks to the backend only through `/api/*` (runtime proxy in `src/app/api/[...path]/route.ts`). OAuth callbacks hit the **backend** public URL directly.

### Step-by-step (one-time)

1. **Push this folder to GitHub** (do not commit `.env`, `backend/.env`, or `backend/.venv`).
2. Open [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint**.
3. Connect the GitHub repo and apply `render.yaml`.
4. When prompted, enter **secret** values (at minimum):
   - `GROQ_API_KEY`
   - `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_PAT`
   - Optional: `SALESFORCE_INSTANCE_URL`, `SALESFORCE_CONSUMER_KEY`
   - Optional: `DOCUSIGN_CLIENT_ID`, `DOCUSIGN_CLIENT_SECRET`, `DOCUSIGN_MCP_SERVER_URL`
5. Wait for both services to deploy. Open the **frontend** URL (`sr-workmind-frontend.onrender.com`).
6. Register OAuth redirect URIs (use your real backend URL from the Render dashboard):
   - Salesforce: `https://<backend-host>/oauth/salesforce/callback`
   - DocuSign: `https://<backend-host>/oauth/docusign/callback`

`FRONTEND_ORIGIN`, `PUBLIC_BACKEND_URL`, and `BACKEND_API_BASE_URL` are wired automatically by the Blueprint.

### Environment variables (reference)

**Set by Blueprint (no action needed):**

- `APP_ENV`, `TOKEN_STORAGE_DIR`, `LLM_PROVIDER`, `MCP_*`, DocuSign defaults
- `FRONTEND_ORIGIN` ← frontend `RENDER_EXTERNAL_URL`
- `PUBLIC_BACKEND_URL` ← backend `RENDER_EXTERNAL_URL`
- `BACKEND_API_BASE_URL` ← backend internal `host:port`

**You must set in the Render dashboard:**

| Variable | Required for |
|----------|----------------|
| `GROQ_API_KEY` | LLM SQL / intent |
| `SNOWFLAKE_*` | Snowflake MCP queries |
| `SALESFORCE_*` | Salesforce connector |
| `DOCUSIGN_*` | DocuSign connector |

### Verify after deploy

- Backend health: `https://<backend-host>/health` → `{"status":"ok"}`
- Frontend loads; run a Snowflake prompt from the UI
- Connect Salesforce / DocuSign via the sidebar links (`/api/auth/...`)

### Notes

- **Free tier:** OAuth tokens are stored under `/tmp/sr-workmind-tokens` and are cleared on redeploy. For persistent tokens, upgrade the backend to a paid instance and attach a Render disk at `/var/data`, then set `TOKEN_STORAGE_DIR=/var/data`.
- **Cold starts:** Free services spin down after inactivity; the first request may take ~30s.
- **Production DocuSign:** switch `DOCUSIGN_OAUTH_BASE_URL` and `DOCUSIGN_MCP_SERVER_URL` to production URLs in the Render dashboard.

### Render commands (from `render.yaml`)

- Backend build: `pip install -r backend/requirements.txt`
- Backend start: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
- Frontend build: `npm ci && npm run build`
- Frontend start: `npm start`

## API Contract

### Request

`POST /execute`

```json
{
  "prompt": "Show top 5 customers by revenue"
}
```

### Response

```json
{
  "intent": {
    "platform": "snowflake",
    "action": "query",
    "intent": "top_customers",
    "parameters": { "limit": 5 }
  },
  "sql": "SELECT customer_name, revenue FROM customers ORDER BY revenue DESC LIMIT 5;",
  "result": {
    "columns": ["CUSTOMER_NAME", "REVENUE"],
    "rows": [["ABC Corp", 12345]]
  },
  "message": "Top customers by revenue are: ...",
  "model": "LLM:groq",
  "connector": "Snowflake MCP Server (Standardized)",
  "mcp_validation": "Passed — MCP Server verified",
  "execution_time_sec": 1.2,
  "security_checks": ["MCP Server RBAC", "OAuth/PAT Auth", "Audit Logged"]
}
```

## Prompts to Run in SR WorkMind UI

Use these in the UI input box:

1. `Show top 5 customers by revenue`
2. `Get total sales by region`
3. `List last 10 orders`

## Docusign Managed MCP Server Setup

The Docusign connector is additive to the existing Snowflake and Salesforce
paths. Configure it in `backend/.env`:

```env
DOCUSIGN_CLIENT_ID=your-integration-key
DOCUSIGN_CLIENT_SECRET=your-client-secret
DOCUSIGN_OAUTH_BASE_URL=https://account-d.docusign.com
DOCUSIGN_MCP_SERVER_URL=https://mcp-d.docusign.com/mcp
DOCUSIGN_MCP_RESOURCE=https://mcp-d.docusign.com/mcp
DOCUSIGN_SCOPES=signature extended agreement_object_model_read aow_manage
DOCUSIGN_REQUIRE_HUMAN_APPROVAL=true
```

Use production OAuth base URL `https://account.docusign.com` and production MCP
URL `https://mcp.docusign.com/mcp` when your app is approved for production.
The MCP server URL is beta/config-driven; set `DOCUSIGN_MCP_SERVER_URL` to the
exact endpoint assigned by Docusign if it differs from the default.

After the backend is running, connect Docusign at:

```text
http://127.0.0.1:8000/auth/docusign
```

Example prompts:

1. `Show active supplier agreements in Docusign`
2. `Get Docusign agreement details for agreement 12345`
3. `Show Docusign workflow trigger requirements for workflow 12345`
4. `Trigger Docusign workflow 12345 for supplier contract review`

## Notes

- The MCP Server is a Snowflake-managed object — no separate infrastructure needed.
- Authentication uses Programmatic Access Tokens (PAT). Upgrade to OAuth for production.
- The `read_only: false` config in the MCP server tool allows full CRUD operations.
- For LLM, set `LLM_PROVIDER=groq` (or ollama/gemini/huggingface) in `.env`.
