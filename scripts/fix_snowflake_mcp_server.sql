-- Recreate Snowflake hosted MCP server with warehouse (required for sql_exec_tool).
-- Run as ACCOUNTADMIN or a role that can create MCP servers in the target schema.
-- Replace placeholders to match backend/.env (SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA, MCP_SERVER_NAME).

USE DATABASE IDENTIFIER($SNOWFLAKE_DATABASE);
USE SCHEMA IDENTIFIER($SNOWFLAKE_SCHEMA);

-- Drop if you need a clean recreate (optional)
-- DROP MCP SERVER IF EXISTS srworkmind_mcp_server;

CREATE OR REPLACE MCP SERVER srworkmind_mcp_server
  FROM SPECIFICATION $$
    tools:
      - name: "sql_exec_tool"
        type: "SYSTEM_EXECUTE_SQL"
        title: "Execute SQL"
        description: "Run read-only or DML SQL in the connected database/schema"
        config:
          warehouse: "COMPUTE_WH"
          query_timeout: 120
  $$;

-- Grant PAT role access (replace SR_WORKMIND_ROLE with your PAT role)
GRANT USAGE ON MCP SERVER srworkmind_mcp_server TO ROLE SR_WORKMIND_ROLE;
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE SR_WORKMIND_ROLE;
