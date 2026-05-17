"use client";

import {
  FormEvent,
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type PlatformId =
  | "snowflake"
  | "salesforce"
  | "docusign"
  | "anaplan"
  | "onestream"
  | "certinia"
  | "adp";

type TableData = {
  columns: string[];
  rows: string[][];
};

type ExecuteApiResponse = {
  intent: {
    platform: string;
    action: string;
    intent: string;
    parameters: Record<string, unknown>;
  };
  sql: string;
  result: {
    columns: string[];
    rows: Array<Array<string | number | boolean | null>>;
  };
  message: string;
  model: string;
  connector: string;
  mcp_validation: string;
  execution_time_sec: number;
  security_checks: string[];
};

type Message = {
  role: "user" | "assistant";
  content: string;
  outputType?: "text" | "table";
  tableData?: TableData;
};

type AuditRow = {
  id: number;
  time: string;
  prompt: string;
  platform: string;
  status: "Success" | "Review";
  intent: string;
  model: string;
  mcpValidation: string;
  connector: string;
  executionTime: string;
  securityChecks: string;
};

type ThinkingStep = {
  name: string;
  detail: string;
  status: "pending" | "running" | "done";
};

const platforms: { id: PlatformId; label: string; badge: string }[] = [
  { id: "snowflake", label: "Snowflake", badge: "SF" },
  { id: "salesforce", label: "Salesforce", badge: "SA" },
  { id: "docusign", label: "DocuSign", badge: "DS" },
  { id: "anaplan", label: "Anaplan", badge: "AN" },
  { id: "onestream", label: "OneStream", badge: "OS" },
  { id: "certinia", label: "Certinia", badge: "CE" },
  { id: "adp", label: "ADP", badge: "AD" },
];

const snowflakeSuggestions = [
  "Show top 5 customers by revenue",
  "Get total sales by region",
  "List last 10 orders",
  "Show top customers",
];

const salesforceSuggestions = [
  "Show all accounts in Technology industry",
  "Find contacts at Acme Corp",
  "List open opportunities over $50K",
  "Show recent leads by status",
];

const docusignSuggestions = [
  "Show active supplier agreements in Docusign",
  "Get Docusign agreement details for agreement 12345",
  "Show Docusign workflow trigger requirements for workflow 12345",
  "Trigger Docusign workflow 12345 for supplier contract review",
];

const snowflakeSteps: ThinkingStep[] = [
  { name: "Intent Detection", detail: "Analyzing prompt", status: "pending" },
  { name: "Model Selection", detail: "Choosing model", status: "pending" },
  {
    name: "MCP Server",
    detail: "Snowflake MCP Server (tools/call)",
    status: "pending",
  },
  {
    name: "SQL Execution",
    detail: "MCP Server JSON-RPC -> Snowflake",
    status: "pending",
  },
  {
    name: "Response Generation",
    detail: "Composing business-safe response",
    status: "pending",
  },
];

const salesforceSteps: ThinkingStep[] = [
  { name: "Intent Detection", detail: "Analyzing prompt", status: "pending" },
  { name: "Model Selection", detail: "Choosing model", status: "pending" },
  {
    name: "MCP Server",
    detail: "Salesforce Hosted MCP Server",
    status: "pending",
  },
  {
    name: "SOQL Execution",
    detail: "MCP Server JSON-RPC -> Salesforce",
    status: "pending",
  },
  {
    name: "Response Generation",
    detail: "Composing business-safe response",
    status: "pending",
  },
];

const docusignSteps: ThinkingStep[] = [
  { name: "Intent Detection", detail: "Analyzing prompt", status: "pending" },
  {
    name: "OAuth Token",
    detail: "Checking Docusign bearer token",
    status: "pending",
  },
  {
    name: "MCP Server",
    detail: "Docusign Managed MCP Server",
    status: "pending",
  },
  {
    name: "Tool Discovery",
    detail: "MCP tools/list -> Docusign schemas",
    status: "pending",
  },
  {
    name: "Tool Execution",
    detail: "MCP tools/call -> Docusign",
    status: "pending",
  },
];

function suggestionsFor(platform: PlatformId) {
  if (platform === "salesforce") {
    return salesforceSuggestions;
  }
  if (platform === "docusign") {
    return docusignSuggestions;
  }
  return snowflakeSuggestions;
}

function stepsFor(platform: PlatformId) {
  if (platform === "salesforce") {
    return salesforceSteps;
  }
  if (platform === "docusign") {
    return docusignSteps;
  }
  return snowflakeSteps;
}

function inferPlatformFromPrompt(prompt: string): PlatformId {
  const text = prompt.toLowerCase();
  const words = new Set(text.split(/\s+/));

  if (
    text.includes("docusign") ||
    text.includes("docu sign") ||
    [
      "agreement",
      "agreements",
      "contract",
      "contracts",
      "envelope",
      "envelopes",
      "signature",
      "signatures",
      "signing",
      "navigator",
      "maestro",
      "workflow",
      "workflows",
      "counterparty",
      "counterparties",
    ].some((word) => words.has(word))
  ) {
    return "docusign";
  }

  if (
    text.includes("salesforce") ||
    text.includes("sfdc") ||
    [
      "account",
      "accounts",
      "contact",
      "contacts",
      "opportunity",
      "opportunities",
      "lead",
      "leads",
      "case",
      "cases",
      "task",
      "tasks",
      "crm",
      "pipeline",
      "deal",
      "deals",
      "prospect",
      "prospects",
      "sobject",
    ].some((word) => words.has(word))
  ) {
    return "salesforce";
  }

  return "snowflake";
}

function ShieldIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
      <path
        d="M12 3L4 6V12C4 16.5 7 20.74 12 22C17 20.74 20 16.5 20 12V6L12 3Z"
        stroke="currentColor"
        strokeWidth="1.7"
      />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none">
      <path
        d="M5 12L10 17L19 8"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function SrLogo() {
  return (
    <div className="inline-flex items-center gap-3 md:gap-4">
      <img
        src="/spaulding-ridge-mark.svg"
        alt="Spaulding Ridge mark"
        className="h-8 w-auto md:h-10"
      />
      <span className="sr-brand-wordmark">SPAULDING RIDGE</span>
    </div>
  );
}

function DotIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none">
      <circle cx="12" cy="12" r="4" fill="currentColor" />
    </svg>
  );
}

export default function Home() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "assistant",
      content:
        "Welcome to SR WorkMind. Ask in natural language and I will orchestrate across enterprise systems.",
    },
  ]);
  const [input, setInput] = useState("Show top 5 customers by revenue");
  const [activePlatform, setActivePlatform] = useState<PlatformId>("snowflake");
  const [thinkingSteps, setThinkingSteps] =
    useState<ThinkingStep[]>(snowflakeSteps);
  const [isThinking, setIsThinking] = useState(false);
  const [auditRows, setAuditRows] = useState<AuditRow[]>([]);
  const [expandedAuditId, setExpandedAuditId] = useState<number | null>(null);
  const [lastAction, setLastAction] = useState("Awaiting user instruction");
  const [generatedSql, setGeneratedSql] = useState("");
  const [sfAuthenticated, setSfAuthenticated] = useState(false);
  const [dsAuthenticated, setDsAuthenticated] = useState(false);

  const apiBase = "/api";
  const promptSuggestions = suggestionsFor(activePlatform);

  // Check external connector auth status on mount
  useEffect(() => {
    const checkAuth = async () => {
      try {
        const [sfRes, dsRes] = await Promise.all([
          fetch(`${apiBase}/auth/salesforce/status`),
          fetch(`${apiBase}/auth/docusign/status`),
        ]);
        if (sfRes.ok) {
          const data = await sfRes.json();
          setSfAuthenticated(data.authenticated ?? false);
        }
        if (dsRes.ok) {
          const data = await dsRes.json();
          setDsAuthenticated(data.authenticated ?? false);
        }
      } catch {
        /* ignore */
      }
    };
    checkAuth();
  }, [apiBase]);

  const timersRef = useRef<number[]>([]);

  const activePlatformName = useMemo(
    () => platforms.find((p) => p.id === activePlatform)?.label ?? "None",
    [activePlatform],
  );

  const clearTimers = () => {
    timersRef.current.forEach((timer) => window.clearTimeout(timer));
    timersRef.current = [];
  };

  useEffect(() => clearTimers, []);

  const runFlow = async (prompt: string, platformForPrompt: PlatformId) => {
    setIsThinking(true);
    setGeneratedSql("");
    const currentSteps = stepsFor(platformForPrompt);
    setThinkingSteps(currentSteps);
    try {
      const response = await fetch(`${apiBase}/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || "Execution failed");
      }

      const payload = (await response.json()) as ExecuteApiResponse;
      const platformId = payload.intent.platform.toLowerCase() as PlatformId;
      const knownPlatform = platforms.some((p) => p.id === platformId)
        ? platformId
        : "snowflake";
      const prettyIntent = payload.intent.intent.replaceAll("_", " ");

      setLastAction(prettyIntent);
      setActivePlatform(knownPlatform);

      const details = [
        `Platform: ${payload.intent.platform} | Action: ${payload.intent.action}`,
        `Selected Model: ${payload.model}`,
        `Validation check: ${payload.mcp_validation} | Routing check: PASS`,
        `${payload.connector}`,
        `System response prepared in ${payload.execution_time_sec}s`,
      ];

      details.forEach((detail, index) => {
        const timer = window.setTimeout(
          () => {
            setThinkingSteps((prev) =>
              prev.map((step, stepIndex) => {
                if (stepIndex < index) {
                  return { ...step, status: "done" };
                }
                if (stepIndex === index) {
                  return { ...step, detail, status: "running" };
                }
                return step;
              }),
            );
          },
          240 + index * 260,
        );
        timersRef.current.push(timer);
      });

      const finishTimer = window.setTimeout(() => {
        const normalizedRows = payload.result.rows.map((row) =>
          row.map((cell) => (cell === null ? "" : String(cell))),
        );
        const hasTable = payload.result.columns.length > 0;

        setThinkingSteps((prev) =>
          prev.map((step) => ({ ...step, status: "done" })),
        );
        setGeneratedSql(payload.sql);
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: payload.message,
            outputType: hasTable ? "table" : "text",
            tableData: hasTable
              ? {
                  columns: payload.result.columns,
                  rows: normalizedRows,
                }
              : undefined,
          },
        ]);
        setAuditRows((prev) => [
          {
            id: prev.length + 1,
            time: new Date().toLocaleTimeString(),
            prompt,
            platform: payload.intent.platform,
            status: "Success",
            intent: prettyIntent,
            model: payload.model,
            mcpValidation: payload.mcp_validation,
            connector: payload.connector,
            executionTime: `${payload.execution_time_sec} sec`,
            securityChecks: payload.security_checks.join(" | "),
          },
          ...prev,
        ]);
        setIsThinking(false);
      }, 1700);

      timersRef.current.push(finishTimer);
    } catch (error) {
      const msg = error instanceof Error ? error.message : "Execution failed";
      if (msg.toLowerCase().includes("docusign not authenticated")) {
        setActivePlatform("docusign");
      }
      if (msg.toLowerCase().includes("salesforce not authenticated")) {
        setActivePlatform("salesforce");
      }
      setThinkingSteps((prev) =>
        prev.map((step, index) =>
          index === 2
            ? {
                ...step,
                status: "done",
                detail: "Validation failed or backend unavailable",
              }
            : step,
        ),
      );
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: `Execution failed: ${msg}`,
          outputType: "text",
        },
      ]);
      setIsThinking(false);
    }
  };

  const submitPrompt = (event: FormEvent) => {
    event.preventDefault();
    const prompt = input.trim();
    if (!prompt || isThinking) {
      return;
    }

    const platformForPrompt = inferPlatformFromPrompt(prompt);
    setActivePlatform(platformForPrompt);
    clearTimers();
    setMessages((prev) => [...prev, { role: "user", content: prompt }]);
    void runFlow(prompt, platformForPrompt);
    setInput("");
  };

  return (
    <div className="sr-shell min-h-screen">
      <div className="sr-topline px-4 py-2 text-center text-sm text-[#e4efff] md:px-8">
        SR WorkMind | Unified Enterprise AI Agent
      </div>

      <div className="grid min-h-[calc(100vh-36px)] grid-cols-1 lg:grid-cols-[84px_1fr]">
        <aside className="sr-sidebar hidden lg:flex">
          <div className="flex h-full flex-col items-center gap-5 py-5">
            <div className="rounded-xl border border-white/20 bg-white/10 p-2 text-white">
              <DotIcon />
            </div>
            {[
              "Workspace",
              "Prompts",
              "Workflows",
              "Connectors",
              "Teams",
              "Audit",
              "Settings",
            ].map((item) => (
              <button
                key={item}
                className="w-full px-2 text-center text-[11px] font-medium text-white/82 transition hover:text-white"
              >
                {item}
              </button>
            ))}
          </div>
        </aside>

        <main className="mx-auto w-full max-w-7xl space-y-6 px-4 py-6 md:px-8">
          <section className="sr-command fade-in-up rounded-3xl px-5 py-7 md:px-8 md:py-10">
            <div className="mx-auto max-w-3xl text-center">
              <div className="mb-4 inline-flex items-center justify-center">
                <SrLogo />
              </div>
              <h1 className="font-mono text-3xl font-bold text-white md:text-5xl">
                SR WorkMind AI Workspace
              </h1>
              <p className="mt-3 text-sm text-[#d5e5ff] md:text-base">
                Ask naturally. SR WorkMind handles intent, model selection, MCP
                routing, connector execution, and enterprise-safe responses.
              </p>

              <form
                onSubmit={submitPrompt}
                className="mx-auto mt-6 max-w-2xl space-y-3"
              >
                <div className="rounded-2xl border border-white/30 bg-[#0a2548]/65 p-3 backdrop-blur">
                  <div className="flex flex-col gap-2 md:flex-row">
                    <input
                      value={input}
                      onChange={(event) => setInput(event.target.value)}
                      placeholder="Ask SR WorkMind to run a task..."
                      className="w-full rounded-xl border border-[#8cb0df]/55 bg-[#f2f7ff] px-4 py-3 text-sm text-[#10294b] outline-none ring-[#7eb8ff] transition placeholder:text-[#5e7698] focus:ring-2"
                    />
                    <button
                      type="submit"
                      disabled={isThinking}
                      className="rounded-xl bg-[#b8dd75] px-5 py-3 text-sm font-semibold text-[#0f2d53] transition hover:brightness-105 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      Execute
                    </button>
                  </div>
                </div>
                <div className="flex flex-wrap justify-center gap-2">
                  {promptSuggestions.map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      onClick={() => setInput(suggestion)}
                      className="rounded-full border border-[#9bb9e2]/45 bg-[#113764]/72 px-3 py-1.5 text-xs text-[#e5f0ff] transition hover:bg-[#19497f]"
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </form>

              <div className="mx-auto mt-6 flex max-w-2xl items-center justify-center gap-6 rounded-2xl border border-[#95b6e4]/40 bg-[#0d315b]/70 px-4 py-3 text-sm text-[#e5f0ff]">
                <span>
                  Active Platform: <strong>{activePlatformName}</strong>
                </span>
                <span className="hidden h-4 w-px bg-white/20 md:block" />
                <span className="hidden md:block">
                  Latest Action: <strong>{lastAction}</strong>
                </span>
              </div>
            </div>
          </section>

          <section className="grid gap-6 xl:grid-cols-[1.55fr_1fr]">
            <div className="space-y-6">
              <article className="fade-in-up glass-card rounded-3xl p-5 md:p-6">
                <div className="mb-4 flex items-center justify-between">
                  <h2 className="font-mono text-xl font-bold text-slate-900">
                    AI Workspace
                  </h2>
                  <span className="rounded-full bg-[var(--accent-soft)] px-3 py-1 text-xs font-semibold text-[var(--accent)]">
                    {activePlatform === "salesforce"
                      ? "Salesforce Hosted MCP"
                      : activePlatform === "docusign"
                        ? "Docusign Managed MCP"
                        : "Snowflake MCP Server"}
                  </span>
                </div>

                <div className="max-h-[360px] space-y-3 overflow-y-auto rounded-2xl border border-[var(--line)] bg-slate-50/90 p-4">
                  {messages.map((message, index) => (
                    <div
                      key={`${message.role}-${index}`}
                      className={`fade-in-up rounded-2xl p-3 text-sm md:text-[15px] ${
                        message.role === "user"
                          ? "ml-auto max-w-[88%] bg-[var(--accent)] text-white"
                          : "mr-auto max-w-[94%] bg-white text-slate-800"
                      }`}
                    >
                      <p className="font-semibold opacity-70">
                        {message.role === "user" ? "You" : "SR WorkMind"}
                      </p>
                      <p className="mt-1">{message.content}</p>
                      {message.outputType === "table" && message.tableData ? (
                        <div className="mt-3 overflow-x-auto rounded-xl border border-[var(--line)]">
                          <table className="min-w-full bg-white text-left text-xs text-slate-700 md:text-sm">
                            <thead className="bg-slate-100 text-slate-600">
                              <tr>
                                {message.tableData.columns.map((column) => (
                                  <th
                                    key={column}
                                    className="px-3 py-2 font-semibold"
                                  >
                                    {column}
                                  </th>
                                ))}
                              </tr>
                            </thead>
                            <tbody>
                              {message.tableData.rows.map((row, rowIndex) => (
                                <tr
                                  key={row[0] + rowIndex}
                                  className="border-t border-[var(--line)]"
                                >
                                  {row.map((cell) => (
                                    <td key={cell} className="px-3 py-2">
                                      {cell}
                                    </td>
                                  ))}
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ) : null}
                    </div>
                  ))}
                  {isThinking ? (
                    <div className="fade-in-up mr-auto max-w-[70%] rounded-2xl bg-white p-3 text-sm text-slate-700">
                      <p className="font-semibold opacity-70">SR WorkMind</p>
                      <p className="mt-1">Running orchestration pipeline...</p>
                    </div>
                  ) : null}
                </div>

                {generatedSql ? (
                  <div className="mt-4 rounded-2xl border border-[var(--line)] bg-white p-3">
                    <p className="text-xs font-semibold uppercase tracking-[0.08em] text-slate-500">
                      {activePlatform === "salesforce"
                        ? "Generated SOQL / Operation"
                        : activePlatform === "docusign"
                          ? "Generated Tool Call"
                          : "Generated SQL"}
                    </p>
                    <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs text-slate-800 md:text-sm">
                      {generatedSql}
                    </pre>
                  </div>
                ) : null}
              </article>

              <article className="fade-in-up glass-card rounded-3xl p-5 md:p-6">
                <h2 className="font-mono text-lg font-bold text-slate-900 md:text-xl">
                  Workflow Visualization
                </h2>
                <div className="mt-4 grid gap-2 md:grid-cols-7">
                  {[
                    "User",
                    "Intent",
                    "Model",
                    "MCP",
                    "Connector",
                    "System",
                    "Response",
                  ].map((stage, index, arr) => (
                    <div key={stage} className="flex items-center gap-2">
                      <div className="w-full rounded-xl border border-[var(--line)] bg-white px-3 py-2 text-center text-xs font-semibold text-slate-700 md:text-sm">
                        {stage}
                      </div>
                      {index < arr.length - 1 ? (
                        <div className="flow-arrow relative hidden h-1 w-5 rounded-full bg-slate-200 md:block" />
                      ) : null}
                    </div>
                  ))}
                </div>
              </article>

              <section className="fade-in-up glass-card rounded-3xl p-5 md:p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <h2 className="font-mono text-lg font-bold text-slate-900 md:text-xl">
                    Admin / Audit View
                  </h2>
                  <p className="text-xs text-[var(--muted)] md:text-sm">
                    Recent actions and execution logs
                  </p>
                </div>
                <div className="mt-4 overflow-x-auto rounded-2xl border border-[var(--line)] bg-white">
                  <table className="min-w-full text-left text-xs text-slate-700 md:text-sm">
                    <thead className="bg-slate-100 text-slate-600">
                      <tr>
                        <th className="px-3 py-2">Time</th>
                        <th className="px-3 py-2">Prompt</th>
                        <th className="px-3 py-2">Platform</th>
                        <th className="px-3 py-2">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {auditRows.length === 0 ? (
                        <tr>
                          <td className="px-3 py-3 text-slate-500" colSpan={4}>
                            No executions yet. Run a prompt to generate audit
                            records.
                          </td>
                        </tr>
                      ) : (
                        auditRows.slice(0, 6).map((row) => {
                          const isExpanded = expandedAuditId === row.id;
                          return (
                            <Fragment key={row.id}>
                              <tr
                                onClick={() =>
                                  setExpandedAuditId((prev) =>
                                    prev === row.id ? null : row.id,
                                  )
                                }
                                className="cursor-pointer border-t border-[var(--line)] transition hover:bg-slate-50"
                              >
                                <td className="px-3 py-2">{row.time}</td>
                                <td className="px-3 py-2">
                                  <span className="mr-2 text-slate-500">
                                    {isExpanded ? "▾" : "▸"}
                                  </span>
                                  {row.prompt}
                                </td>
                                <td className="px-3 py-2">{row.platform}</td>
                                <td className="px-3 py-2 font-semibold text-emerald-700">
                                  {row.status}
                                </td>
                              </tr>
                              {isExpanded ? (
                                <tr className="border-t border-[var(--line)] bg-slate-50/70">
                                  <td colSpan={4} className="px-4 py-3">
                                    <div className="rounded-xl border border-[var(--line)] bg-white p-3 text-xs text-slate-700 md:text-sm">
                                      <p className="mb-2 font-semibold text-slate-900">
                                        Execution Details
                                      </p>
                                      <div className="grid gap-1.5">
                                        <p>
                                          Intent Detected:{" "}
                                          <strong>{row.intent}</strong>
                                        </p>
                                        <p>
                                          Platform:{" "}
                                          <strong>{row.platform}</strong>
                                        </p>
                                        <p>
                                          Model Used:{" "}
                                          <strong>{row.model}</strong>
                                        </p>
                                        <p>
                                          MCP Validation:{" "}
                                          <strong>{row.mcpValidation}</strong>
                                        </p>
                                        <p>
                                          Connector:{" "}
                                          <strong>{row.connector}</strong>
                                        </p>
                                        <p>
                                          Execution Time:{" "}
                                          <strong>{row.executionTime}</strong>
                                        </p>
                                        <p>
                                          Security Checks:{" "}
                                          <strong>{row.securityChecks}</strong>
                                        </p>
                                      </div>
                                    </div>
                                  </td>
                                </tr>
                              ) : null}
                            </Fragment>
                          );
                        })
                      )}
                    </tbody>
                  </table>
                </div>
              </section>
            </div>

            <aside className="space-y-6">
              <article className="fade-in-up glass-card rounded-3xl p-5 md:p-6">
                <h2 className="font-mono text-lg font-bold text-slate-900">
                  AI Thinking Panel
                </h2>
                <p className="mt-1 text-xs text-[var(--muted)] md:text-sm">
                  Transparent execution trace across intent, model, MCP, and
                  connector.
                </p>
                <div className="mt-4 space-y-3">
                  {thinkingSteps.map((step) => (
                    <div
                      key={step.name}
                      className={`rounded-2xl border p-3 transition ${
                        step.status === "done"
                          ? "border-emerald-200 bg-emerald-50"
                          : step.status === "running"
                            ? "border-sky-200 bg-sky-50"
                            : "border-[var(--line)] bg-white"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-sm font-semibold text-slate-800">
                          {step.name}
                        </p>
                        <span
                          className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase ${
                            step.status === "done"
                              ? "bg-emerald-100 text-emerald-700"
                              : step.status === "running"
                                ? "stage-pulse bg-sky-100 text-sky-700"
                                : "bg-slate-100 text-slate-500"
                          }`}
                        >
                          {step.status === "done"
                            ? "Done"
                            : step.status === "running"
                              ? "In Progress"
                              : "Pending"}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-slate-600">
                        {step.detail}
                      </p>
                    </div>
                  ))}
                </div>
              </article>

              <article className="fade-in-up glass-card rounded-3xl p-5 md:p-6">
                <h2 className="font-mono text-lg font-bold text-slate-900">
                  Platform Orchestration
                </h2>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {platforms.map((platform) => {
                    const isActive = platform.id === activePlatform;
                    const isClickable =
                      platform.id === "snowflake" ||
                      platform.id === "salesforce" ||
                      platform.id === "docusign";
                    return (
                      <button
                        key={platform.id}
                        type="button"
                        onClick={() =>
                          isClickable && setActivePlatform(platform.id)
                        }
                        disabled={!isClickable}
                        className={`rounded-xl border px-3 py-2 text-left transition ${
                          isActive
                            ? "border-[var(--accent)] bg-[var(--accent-soft)]"
                            : isClickable
                              ? "border-[var(--line)] bg-white hover:bg-slate-50 cursor-pointer"
                              : "border-[var(--line)] bg-white/50 opacity-60 cursor-not-allowed"
                        }`}
                      >
                        <p
                          className={`inline-flex h-7 w-7 items-center justify-center rounded-full text-xs font-bold ${
                            isActive
                              ? "bg-[var(--accent)] text-white"
                              : "bg-slate-100 text-slate-600"
                          }`}
                        >
                          {platform.badge}
                        </p>
                        <p className="mt-2 text-xs font-semibold text-slate-800 md:text-sm">
                          {platform.label}
                        </p>
                      </button>
                    );
                  })}
                </div>

                {activePlatform === "salesforce" && !sfAuthenticated && (
                  <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4">
                    <p className="text-sm font-semibold text-amber-800">
                      Salesforce Authentication Required
                    </p>
                    <p className="mt-1 text-xs text-amber-700">
                      You must connect your Salesforce org via OAuth 2.0 before
                      executing commands.
                    </p>
                    <a
                      href={`${apiBase}/auth/salesforce`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-3 inline-block rounded-lg bg-[#00a1e0] px-4 py-2 text-xs font-bold text-white transition hover:bg-[#008cbf]"
                    >
                      Connect Salesforce
                    </a>
                  </div>
                )}

                {activePlatform === "docusign" && !dsAuthenticated && (
                  <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4">
                    <p className="text-sm font-semibold text-amber-800">
                      Docusign Authentication Required
                    </p>
                    <p className="mt-1 text-xs text-amber-700">
                      Connect Docusign via OAuth 2.0 before running MCP tools.
                    </p>
                    <a
                      href={`${apiBase}/auth/docusign`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-3 inline-block rounded-lg bg-[#4c00ff] px-4 py-2 text-xs font-bold text-white transition hover:brightness-110"
                    >
                      Connect Docusign
                    </a>
                  </div>
                )}
              </article>

              <article className="fade-in-up glass-card rounded-3xl p-5 md:p-6">
                <h2 className="font-mono text-lg font-bold text-slate-900">
                  Security & Governance
                </h2>
                <div className="mt-3 space-y-2 text-sm">
                  {[
                    "MCP Server RBAC enforced",
                    "PAT/OAuth authentication",
                    "Audit logging enabled",
                  ].map((item) => (
                    <div
                      key={item}
                      className="flex items-center justify-between rounded-xl border border-[var(--line)] bg-white px-3 py-2"
                    >
                      <span className="inline-flex items-center gap-2 text-slate-700">
                        <span className="text-[var(--success)]">
                          <ShieldIcon />
                        </span>
                        {item}
                      </span>
                      <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-bold uppercase text-emerald-700">
                        <CheckIcon />
                        OK
                      </span>
                    </div>
                  ))}
                </div>
              </article>
            </aside>
          </section>
        </main>
      </div>
    </div>
  );
}
