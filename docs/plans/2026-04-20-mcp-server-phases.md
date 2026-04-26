# Implementation Plan: MCP Server Infrastructure (Phases 1-3)
Date: 2026-04-20
Status: Draft

## Summary
This plan covers the deployment and configuration of Model Context Protocol (MCP) servers across three capability domains: cross-project research (Phase 1), multi-repo codebase operations (Phase 2), and a custom trading agent server (Phase 3). Phases 1 and 2 wire up off-the-shelf MCP servers with lightweight custom configuration layers. Phase 3 is a ground-up custom MCP server with hard guardrails for live order execution. The trading server's primary goal is to generate enough revenue to self-fund API costs and infrastructure for all three phases.

## Requirements addressed
This plan does not map to ne-body F-NNN or NF-NNN requirements. It is infrastructure that supports the development workflow across multiple projects, including ne-body. The trading server (Phase 3) is a separate revenue-generating system.

> **Note:** This plan creates infrastructure outside the ne-body POC scope. The MCP servers are developer tooling and a separate trading system, not modifications to the SSA platform. The ne-body architecture document and requirements document are unaffected. The plan document lives in the ne-body repo because that is where the planner agent operates, but Phases 2 and 3 will produce artifacts in separate repositories.

---

## Files affected

### Phase 1 — Research across projects
- `~/.claude/settings.json` — add MCP server declarations for memory, search, and fetch servers
- **New repo:** `~/git/mcp-memory-proxy/` — custom project-scoping proxy layer around `@modelcontextprotocol/server-memory`
  - `server.py` — MCP server that wraps the memory server with project namespace isolation
  - `config.json` — project registry mapping project slugs to memory namespace prefixes
  - `requirements.txt` or `package.json` — dependencies

### Phase 2 — Codebase work
- `~/.claude/settings.json` — add filesystem and git MCP server declarations
- **New repo:** `~/git/mcp-pipeline-tools/` — custom MCP server exposing build/test/deploy hooks
  - `server.py` — MCP server exposing project-specific pipeline tools
  - `pipelines.json` — per-project pipeline configuration (commands, paths, env vars)
  - `requirements.txt` or `package.json`

### Phase 3 — Trading agents
- **New repo:** `~/git/mcp-trading-server/` — custom MCP server for trading workflows
  - `server.py` — main MCP server entry point
  - `tools/market_data.py` — market data tool implementations
  - `tools/orders.py` — order management tool implementations
  - `tools/portfolio.py` — portfolio state tool implementations
  - `tools/backtest.py` — backtesting tool implementations
  - `guardrails/limits.py` — position size, drawdown, instrument whitelist enforcement
  - `guardrails/kill_switch.py` — emergency halt mechanism
  - `broker/base.py` — abstract broker interface
  - `broker/etrade.py` — E-Trade integration
  - `broker/alpaca.py` — Alpaca integration (or Webull/IBKR — see open questions)
  - `data/polygon.py` — Polygon.io market data adapter
  - `config.py` — configuration loading (guardrail params, broker credentials, instrument whitelist)
  - `auth.py` — server authentication layer
  - `tests/` — full test suite

---

## Data flow changes

### Phase 1 data flow
```
Claude Code session
  |-- identifies current project (from cwd or explicit context)
  |-- calls mcp-memory-proxy with project-scoped namespace
  |      |-- proxies to @modelcontextprotocol/server-memory
  |      |-- all entities/relations stored with project prefix
  |-- calls brave-search or exa for web research
  |-- calls fetch for URL content retrieval
```

### Phase 2 data flow
```
Claude Code session
  |-- calls filesystem server (scoped to allowed repo roots)
  |-- calls git server for history/blame/diff
  |-- calls pipeline-tools server
  |      |-- looks up project in pipelines.json by cwd
  |      |-- executes build/test/deploy commands in subprocess
  |      |-- returns stdout/stderr and exit code
```

### Phase 3 data flow
```
Claude Code session (research context)
  |-- calls market_data tools (read-only, always safe)
  |-- calls backtest tools (read-only, always safe)
  |-- does NOT have access to order tools

Claude Code session (execution context)
  |-- calls market_data tools
  |-- calls portfolio tools
  |-- calls order tools
  |      |-- guardrails/limits.py validates BEFORE broker call
  |      |-- instrument whitelist check
  |      |-- position size check (% of account balance)
  |      |-- max drawdown check (cumulative P&L floor)
  |      |-- kill switch check (global halt flag)
  |      |-- IF all pass: broker adapter executes order
  |      |-- IF any fail: returns rejection with reason, no order sent
```

> **Critical design constraint from source plan:** Research sessions must never have live order-entry tools active. This is enforced by running two separate MCP server configurations — the trading server exposes different tool sets depending on the session profile.

---

## Implementation steps

### Phase 1: Research across projects

#### 1.1 Install off-the-shelf MCP servers
- **Action:** Install `@modelcontextprotocol/server-memory`, `@modelcontextprotocol/server-brave-search` (or `exa`), and `@modelcontextprotocol/server-fetch` via npm/npx
- **Why:** These provide the base capabilities — knowledge graph persistence, web search, and URL fetching
- **Dependencies:** Node.js 20+ installed, Brave Search API key (or Exa API key)
- **Risk:** Low — these are maintained reference implementations

#### 1.2 Register servers in Claude Code settings
- **Action:** Add MCP server entries to `~/.claude/settings.json` under a new `"mcpServers"` key. Each entry specifies the server command, arguments, and any required environment variables
- **Why:** Claude Code discovers MCP servers from this configuration
- **Dependencies:** Step 1.1
- **Risk:** Low

#### 1.3 Build the project-scoping memory proxy
- **Action:** Create `~/git/mcp-memory-proxy/` with a lightweight MCP server (Python or TypeScript) that:
  1. Intercepts all `create_entities`, `create_relations`, `search_nodes`, `open_nodes`, and `delete_entities` calls
  2. Determines the current project from an explicit `project` parameter on each tool call (do not rely on cwd — MCP servers do not inherit the caller's working directory reliably)
  3. Prefixes all entity names with the project slug (e.g., `ne-body::KalmanFilter` vs `trading::PositionSizer`) before forwarding to the underlying memory server
  4. On read operations, filters results to only return entities matching the current project prefix
  5. Exposes a `list_projects` tool that returns all known project slugs from the stored graph
  6. Exposes a `cross_project_search` tool that explicitly searches across all projects (opt-in bleed-through for cross-pollination)
- **Why:** Without namespacing, memory from the trading project would pollute ne-body research and vice versa. The proxy enforces isolation by default while allowing explicit cross-project queries
- **Dependencies:** Step 1.1 (the underlying memory server must be running)
- **Risk:** Medium — the proxy must faithfully forward all MCP protocol messages and handle the memory server's knowledge graph schema correctly. Error in prefix stripping on reads would return empty results silently.

#### 1.4 Choose search provider
- **Action:** Evaluate Brave Search vs Exa. Decision criteria: (a) API cost per query, (b) quality of results for technical/financial domains, (c) rate limits. Configure the chosen provider's API key as an environment variable
- **Why:** Both are viable; need a concrete choice before wiring up
- **Dependencies:** None
- **Risk:** Low — either can be swapped later without architectural change

#### 1.5 Verify end-to-end
- **Action:** In a Claude Code session, test: (a) store a memory entity scoped to `ne-body`, (b) switch project context to `trading`, (c) confirm the ne-body entity is not visible, (d) use `cross_project_search` to confirm it is retrievable when explicitly requested, (e) perform a web search and fetch a URL
- **Dependencies:** Steps 1.2, 1.3, 1.4
- **Risk:** Low

---

### Phase 2: Codebase work

#### 2.1 Install filesystem and git MCP servers
- **Action:** Install `@modelcontextprotocol/server-filesystem` and `@modelcontextprotocol/server-git` via npm/npx
- **Why:** Filesystem server provides scoped file read/write across multiple repos. Git server provides history, blame, and diff without shelling out
- **Dependencies:** Node.js 20+
- **Risk:** Low

#### 2.2 Configure filesystem server with repo allowlist
- **Action:** Register the filesystem server in `~/.claude/settings.json` with the `allowedDirectories` parameter set to the known repo roots:
  - `/Users/jebbaugh/git/n-body`
  - `/Users/jebbaugh/git/mcp-memory-proxy` (from Phase 1)
  - `/Users/jebbaugh/git/mcp-pipeline-tools` (this phase)
  - `/Users/jebbaugh/git/mcp-trading-server` (Phase 3, when created)
  - Additional repos as needed
- **Why:** Scoping prevents accidental reads/writes outside project directories. This is the filesystem server's built-in sandboxing mechanism
- **Dependencies:** Step 2.1
- **Risk:** Low — but the allowlist must be maintained as new repos are added

#### 2.3 Configure git MCP server
- **Action:** Register the git server in `~/.claude/settings.json`. The git server operates on any git repo the filesystem server can access, so no additional scoping is needed
- **Why:** Provides `git_log`, `git_diff`, `git_blame`, `git_show`, `git_status`, `git_branch_list` tools without Bash permissions
- **Dependencies:** Step 2.1
- **Risk:** Low

#### 2.4 Build the custom pipeline-tools MCP server
- **Action:** Create `~/git/mcp-pipeline-tools/` with a Python MCP server exposing these tools:

  **Tool: `run_tests`**
  - Parameters: `project` (string, required), `target` (string, optional — specific test file or pattern)
  - Behavior: Looks up the project in `pipelines.json`, runs the configured test command (e.g., `pytest tests/ -v` for ne-body, `cargo test` for a Rust project), returns stdout/stderr and exit code
  - Guardrail: Commands are defined in the config file, not passed by the caller. The caller can only select a project and optionally a target pattern that is appended to the pre-defined command

  **Tool: `run_build`**
  - Parameters: `project` (string, required)
  - Behavior: Runs the configured build command for the project
  - Guardrail: Same as above — command template from config, not caller

  **Tool: `run_lint`**
  - Parameters: `project` (string, required), `files` (array of strings, optional)
  - Behavior: Runs the configured lint command (e.g., `ruff check` for Python, `eslint` for JS)

  **Tool: `run_typecheck`**
  - Parameters: `project` (string, required), `files` (array of strings, optional)
  - Behavior: Runs the configured type checker (e.g., `mypy` for Python)

  **Tool: `deploy`**
  - Parameters: `project` (string, required), `environment` (string, required — "staging" or "production")
  - Behavior: Runs the configured deploy script. **Production deploys require a confirmation token** — the tool returns a one-time token on first call, and the caller must pass it back on a second call to confirm
  - Guardrail: Two-step confirmation for production. Deploy commands from config only

- **Why:** Centralizes project-specific build/test/deploy knowledge in a config file rather than scattering Bash permission entries across Claude Code settings. New projects are onboarded by adding an entry to `pipelines.json`, not by editing settings.json
- **Dependencies:** Phase 1 complete (so the memory proxy is available for the pipeline server to log results if desired)
- **Risk:** Medium — subprocess execution requires careful input sanitization. The `target` parameter on `run_tests` must be validated to prevent command injection (e.g., reject targets containing `;`, `|`, `&&`, backticks)

#### 2.5 Create pipelines.json for ne-body
- **Action:** Create the initial pipeline configuration entry for ne-body:
  ```json
  {
    "ne-body": {
      "root": "/Users/jebbaugh/git/n-body",
      "test": {
        "command": "uv run pytest tests/ -v",
        "target_flag": "",
        "target_append": true
      },
      "build": {
        "command": "echo 'No build step for ne-body POC'"
      },
      "lint": {
        "command": "uv run ruff check",
        "files_append": true
      },
      "typecheck": {
        "command": "uv run mypy --ignore-missing-imports",
        "files_append": true,
        "default_files": ["backend/"]
      },
      "deploy": null
    }
  }
  ```
- **Why:** ne-body is the first project to onboard; validates the pipeline server works end-to-end
- **Dependencies:** Step 2.4
- **Risk:** Low

#### 2.6 Verify end-to-end
- **Action:** In a Claude Code session: (a) use filesystem server to read a file from ne-body, (b) use git server to view recent commits, (c) use pipeline-tools to run ne-body tests, (d) confirm test output is returned correctly
- **Dependencies:** Steps 2.2-2.5
- **Risk:** Low

---

### Phase 3: Trading agents

#### 3.1 Repository and project setup
- **Action:** Create `~/git/mcp-trading-server/` as a Python project with:
  - `pyproject.toml` with dependencies: `mcp` (Python SDK), `httpx`, `pydantic`, `polygon-api-client`, broker SDK(s)
  - Directory structure: `server.py`, `tools/`, `guardrails/`, `broker/`, `data/`, `tests/`
  - `.env.example` documenting all required credentials
- **Why:** Clean separation from all other projects. Trading code must never live in the ne-body repo
- **Dependencies:** None
- **Risk:** Low

#### 3.2 Define tool schemas — Market Data

**Tool: `get_quote`**
```
Parameters:
  symbol: string (required) — ticker symbol
  
Returns:
  symbol: string
  bid: float
  ask: float
  last: float
  volume: int
  timestamp_utc: string (ISO 8601)
```

**Tool: `get_ohlcv`**
```
Parameters:
  symbol: string (required)
  timeframe: string (required) — "1m", "5m", "15m", "1h", "1d"
  start_date: string (required) — ISO 8601 date
  end_date: string (required) — ISO 8601 date
  
Returns:
  bars: array of {
    timestamp_utc: string
    open: float
    high: float
    low: float
    close: float
    volume: int
  }
```

**Tool: `get_ticker_details`**
```
Parameters:
  symbol: string (required)
  
Returns:
  symbol: string
  name: string
  market: string — "stocks", "otc"
  market_cap: float | null
  share_class_shares_outstanding: int | null
  primary_exchange: string
  type: string — "CS", "ADRC", etc.
```

**Tool: `screen_tickers`**
```
Parameters:
  market: string (optional) — "stocks", "otc"
  min_volume: int (optional)
  max_price: float (optional)
  min_price: float (optional)
  sort_by: string (optional) — "volume", "change_pct", "market_cap"
  limit: int (optional, default 20, max 100)
  
Returns:
  tickers: array of {symbol, name, last, change_pct, volume}
```

- **Why:** All read-only. These are safe in both research and execution contexts
- **Dependencies:** Polygon.io API key
- **Risk:** Low — read-only operations, no financial risk

#### 3.3 Define tool schemas — Portfolio State

**Tool: `get_positions`**
```
Parameters: (none required)

Returns:
  positions: array of {
    symbol: string
    quantity: int
    avg_cost: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
  }
  cash_balance: float
  total_equity: float
  buying_power: float
```

**Tool: `get_account_summary`**
```
Parameters: (none required)

Returns:
  total_equity: float
  cash_balance: float
  buying_power: float
  day_pnl: float
  total_pnl: float
  open_position_count: int
  max_drawdown_current: float — running drawdown from equity high-water mark
  max_drawdown_limit: float — configured limit
  drawdown_remaining: float — how much more drawdown before kill switch triggers
```

**Tool: `get_order_history`**
```
Parameters:
  status: string (optional) — "open", "filled", "cancelled", "all"
  since: string (optional) — ISO 8601 datetime
  limit: int (optional, default 50)

Returns:
  orders: array of {
    order_id: string
    symbol: string
    side: string — "buy", "sell"
    quantity: int
    order_type: string — "market", "limit", "stop", "stop_limit"
    limit_price: float | null
    stop_price: float | null
    status: string
    filled_quantity: int
    filled_avg_price: float | null
    submitted_at_utc: string
    filled_at_utc: string | null
  }
```

- **Why:** Read-only view of account state. Safe in both contexts. Critical for agent to understand current exposure before making decisions
- **Dependencies:** Broker API credentials
- **Risk:** Low — read-only

#### 3.4 Define tool schemas — Order Management

**Tool: `submit_order`**
```
Parameters:
  symbol: string (required)
  side: string (required) — "buy" or "sell"
  quantity: int (required)
  order_type: string (required) — "market", "limit", "stop", "stop_limit"
  limit_price: float (optional, required if order_type is "limit" or "stop_limit")
  stop_price: float (optional, required if order_type is "stop" or "stop_limit")
  time_in_force: string (optional, default "day") — "day", "gtc", "ioc"
  reason: string (required) — free-text justification logged for audit

Returns:
  order_id: string
  status: string — "pending", "accepted", "rejected"
  rejection_reason: string | null
  guardrail_checks: array of {
    check: string — "instrument_whitelist", "position_size", "max_drawdown", "kill_switch", "buying_power"
    passed: bool
    detail: string
  }
```

**Tool: `cancel_order`**
```
Parameters:
  order_id: string (required)

Returns:
  order_id: string
  status: string — "cancel_requested", "already_filled", "not_found"
```

**Tool: `cancel_all_orders`**
```
Parameters: (none)

Returns:
  cancelled_count: int
  failed_count: int
  details: array of {order_id, status}
```

- **Why:** These are the dangerous tools. They MUST only be available in execution-context sessions, never in research sessions
- **Dependencies:** Broker API credentials, guardrails implemented (step 3.5)
- **Risk:** HIGH — these move real money. Every call passes through the guardrail layer before reaching the broker

#### 3.5 Implement guardrail layer

This is the most critical component. Guardrails are enforced in the MCP server itself, not in prompts or agent instructions. The agent cannot bypass them.

**Guardrail: Instrument whitelist**
- Data structure: `Set[str]` loaded from config file `allowed_instruments.json`
- Enforcement point: `submit_order` checks `symbol in whitelist` before any broker call
- Rejection: returns immediately with `rejection_reason: "instrument not in whitelist"`
- Update mechanism: file-based, requires server restart to change. Intentionally not updatable via MCP tool

**Guardrail: Position size limit**
- Data structure: `max_position_pct: float` (e.g., 0.05 = 5% of account equity per position)
- Enforcement: On `submit_order`, compute `(quantity * current_price) / total_equity`. If exceeds `max_position_pct`, reject
- Also enforce: `max_position_value_usd: float` as an absolute cap (e.g., $500) regardless of account size
- Rejection: returns with detail showing the computed percentage and the limit

**Guardrail: Max drawdown**
- Data structure: `max_drawdown_pct: float` (e.g., 0.10 = 10% from high-water mark)
- State: The server maintains an equity high-water mark in a persistent file (`state/hwm.json`)
- Enforcement: On `submit_order`, compute current drawdown. If already at or beyond limit, reject all new buy orders. Sell orders are always permitted (to allow position unwinding)
- Rejection: returns with current drawdown percentage and limit

**Guardrail: Kill switch**
- Data structure: `kill_switch_active: bool` in a persistent file (`state/kill_switch.json`)
- Enforcement: If active, ALL orders rejected immediately. Portfolio and market data tools remain functional
- Activation: (a) Automatic — triggered when max drawdown is breached, (b) Manual — a `kill_switch.json` file can be edited or a separate admin endpoint/tool can flip it
- Deactivation: Manual only. Requires editing the file and restarting the server. Intentionally high-friction
- The server checks the kill switch file on every `submit_order` call (not cached in memory) so it can be flipped externally without restart

**Guardrail: Buying power check**
- Enforcement: On `submit_order` for buys, verify `quantity * price <= available_buying_power` via broker API. This is a redundant check (the broker will also reject), but catching it at the MCP layer provides a better error message

**Guardrail: PDT (Pattern Day Trader) awareness**
- For E-Trade accounts under $25k: track round-trip day trades in a rolling 5-business-day window
- If 3 round trips already used, reject new buy orders for symbols with existing same-day positions
- Data structure: `state/day_trades.json` — array of `{symbol, buy_time_utc, sell_time_utc}`

**Audit logging:**
- Every `submit_order` call is logged to `logs/orders.jsonl` with: timestamp, parameters, all guardrail check results, broker response, and the agent's stated `reason`
- Every `cancel_order` call is also logged
- Log file is append-only, never truncated by the server

- **Why:** The entire design philosophy is that guardrails live in server code, not in prompts. An adversarial prompt or confused agent cannot bypass a position size check that runs in Python before the broker API is called
- **Dependencies:** Steps 3.2, 3.3 (market data and portfolio tools needed to compute guardrail checks)
- **Risk:** HIGH — guardrail bugs could allow oversized positions or trading during a kill switch. This is the most critical code to test thoroughly

#### 3.6 Define tool schemas — Backtesting

**Tool: `run_backtest`**
```
Parameters:
  strategy: string (required) — name of a registered strategy
  symbols: array of string (required)
  start_date: string (required) — ISO 8601
  end_date: string (required) — ISO 8601
  initial_capital: float (optional, default 10000)
  parameters: object (optional) — strategy-specific params

Returns:
  total_return_pct: float
  max_drawdown_pct: float
  sharpe_ratio: float
  win_rate: float
  total_trades: int
  avg_trade_pnl: float
  equity_curve: array of {date, equity}
  trades: array of {symbol, side, quantity, price, date_utc, pnl}
```

**Tool: `list_strategies`**
```
Parameters: (none)

Returns:
  strategies: array of {
    name: string
    description: string
    parameters: object — JSON schema of configurable params
  }
```

- **Why:** Backtesting is read-only and safe in any context. Strategies are registered in server code, not defined by the agent at runtime (prevents arbitrary code execution)
- **Dependencies:** Polygon.io historical data access
- **Risk:** Medium — backtesting is computationally expensive. Need timeout limits on `run_backtest` to prevent runaway queries. Strategy code is server-side only

#### 3.7 Implement broker abstraction layer

**Abstract interface (`broker/base.py`):**
```
class BrokerAdapter(ABC):
    async def get_account() -> AccountSummary
    async def get_positions() -> list[Position]
    async def get_orders(status, since, limit) -> list[Order]
    async def submit_order(symbol, side, qty, order_type, limit_price, stop_price, tif) -> OrderResult
    async def cancel_order(order_id) -> CancelResult
    async def get_quote(symbol) -> Quote  # for real-time price in guardrail checks
```

**E-Trade adapter (`broker/etrade.py`):**
- Uses E-Trade API (OAuth 1.0a) for equities
- Handles token refresh automatically
- PDT tracking integrated
- Suitable for: main equities account

**Alpaca adapter (`broker/alpaca.py`):**
- Uses Alpaca API (API key auth) — simpler auth model
- Supports paper trading mode (same API, different base URL)
- Suitable for: penny stock / OTC strategy, and paper trading during development
- Note: Alpaca OTC support is limited. If OTC/pink sheets are critical, IBKR may be needed instead (see open questions)

- **Why:** Broker abstraction allows running the same trading logic against paper or live, and against different brokers for different strategies
- **Dependencies:** Broker account(s) set up, API credentials obtained
- **Risk:** Medium — each broker has different order status models, error codes, and settlement timing. Edge cases in order lifecycle (partial fills, corrections) require careful handling

#### 3.8 Implement session context separation

- **Action:** The trading MCP server accepts a `--profile` argument at startup: `research` or `execution`
  - `research` profile: registers market_data tools + portfolio tools + backtest tools. Order management tools are NOT registered. The MCP server literally does not expose them — there is no tool to call, not even one that would be rejected
  - `execution` profile: registers all tools including order management
- **Why:** This is the architectural separation called out in the source plan. A research session cannot submit orders because the tools do not exist in that session's MCP server instance
- **Dependencies:** All tool implementations complete
- **Risk:** Medium — must verify that Claude Code correctly reflects the available tools from each server instance. If both profiles are registered simultaneously, the separation fails. Configuration must ensure only one profile is active per session

#### 3.9 Implement server authentication

- **Action:** The trading MCP server requires authentication before accepting any tool calls:
  - On startup, the server loads an API key from `TRADING_MCP_API_KEY` environment variable
  - All MCP transport messages must include this key in headers (for SSE/HTTP transport) or the server validates the parent process identity (for stdio transport)
  - For the stdio transport used by Claude Code: authentication is implicit — only the Claude Code process can communicate with the server. The real auth boundary is the broker API credentials, which are loaded from environment variables on the server side and never exposed to the client
- **Why:** Prevents unauthorized tool invocation. For local development, the stdio transport provides process-level isolation. For any future networked deployment, explicit API key auth is required
- **Dependencies:** None
- **Risk:** Low for local stdio deployment. HIGH if ever exposed over network — would need TLS + proper auth

#### 3.10 Agent orchestration approach

The trading system uses a two-agent model, NOT a single agent with all tools:

**Research agent:**
- Has: memory (Phase 1), web search (Phase 1), fetch (Phase 1), market data, portfolio (read-only), backtest
- Does NOT have: order tools
- Job: analyze market conditions, screen for opportunities, run backtests, produce trade recommendations as structured output
- Output: writes recommendations to a shared file or memory store with: symbol, direction, rationale, suggested size, risk/reward

**Execution agent:**
- Has: market data, portfolio, order management (with guardrails)
- Does NOT have: web search, research memory (prevents rabbit-holing during execution)
- Job: reviews recommendations from research agent, validates current market conditions, executes orders, manages open positions
- Input: reads recommendations from the shared store
- Constraint: execution agent should be prompted to follow a strict checklist (check guardrails, verify price hasn't moved significantly since recommendation, confirm position sizing)

**Orchestration:**
- Phase 3 MVP: manual. User runs research agent, reviews recommendations, then runs execution agent
- Phase 3 v2: automated handoff. Research agent outputs to a queue, execution agent picks up and executes. Human approval gate configurable (always-approve for paper trading, require approval for live)
- The two agents run as separate Claude Code sessions with different MCP server profiles

- **Why:** Separation of concerns prevents the research agent from impulsively trading on a half-formed thesis, and prevents the execution agent from going down research rabbit holes when it should be managing positions
- **Dependencies:** All prior Phase 3 steps
- **Risk:** Medium — the handoff mechanism between research and execution agents needs careful design to avoid stale recommendations being executed

---

## Test strategy

### Phase 1 tests
- **Unit:** Memory proxy correctly prefixes entity names per project. Search with project scope returns only scoped results. `cross_project_search` returns results from all projects
- **Integration:** Full round-trip: store entity in project A, switch to project B, verify isolation, switch back, verify retrieval
- **Edge cases:** Empty project name, special characters in entity names, very long entity names

### Phase 2 tests
- **Unit:** Pipeline config loading, command construction with target/file parameters, input sanitization (reject shell metacharacters in target parameter)
- **Integration:** Run ne-body tests via pipeline-tools, verify output matches direct `pytest` invocation
- **Security:** Attempt command injection via the `target` parameter — must be rejected

### Phase 3 tests
- **Unit — Guardrails (most critical):**
  - Position size limit: verify rejection when order exceeds max_position_pct
  - Position size limit: verify acceptance when order is within limits
  - Max drawdown: verify rejection when drawdown exceeds limit
  - Max drawdown: verify sell orders are permitted even during drawdown breach
  - Kill switch: verify all orders rejected when active
  - Kill switch: verify market data and portfolio tools still work when active
  - Instrument whitelist: verify rejection for unlisted symbols
  - PDT tracking: verify correct counting of round trips in rolling window
  - PDT tracking: verify rejection on 4th round trip for accounts under $25k
  - Buying power: verify rejection when insufficient funds
  - Compound check: verify all guardrails run (not short-circuit) so the response shows ALL failures

- **Unit — Broker adapters:**
  - Mock broker API responses for: order accepted, order rejected, partial fill, order cancelled, account query
  - Verify correct mapping between MCP tool schema and broker-specific API formats

- **Integration — Paper trading:**
  - Full round-trip: get quote, submit order via Alpaca paper, verify order appears in get_orders, verify position appears in get_positions
  - Cancel order flow: submit limit order, cancel it, verify status

- **Integration — Backtest:**
  - Run a simple strategy backtest, verify results are reasonable (not NaN, not infinite, dates are correct)

- **System — Session separation:**
  - Start server in research profile, verify order tools are not in tool list
  - Start server in execution profile, verify all tools are in tool list

- **Audit:**
  - Submit orders (accepted and rejected), verify all are logged in `logs/orders.jsonl` with correct fields

---

## Risks and mitigations

### Phase 1
- **Risk:** Memory server knowledge graph grows unbounded over time. **Mitigation:** Add a periodic pruning tool or document manual cleanup process. Monitor file size of the memory store.
- **Risk:** Brave Search API costs accumulate. **Mitigation:** Set a monthly budget alert. Consider caching frequent queries.

### Phase 2
- **Risk:** Command injection via pipeline tool parameters. **Mitigation:** Strict input validation — reject any target/file parameter containing shell metacharacters. Use `subprocess.run()` with `shell=False` and pass arguments as a list, never a concatenated string. **Risk level: Medium.**
- **Risk:** Pipeline commands hang indefinitely. **Mitigation:** Enforce a configurable timeout (default 120 seconds) on all subprocess calls.

### Phase 3
- **Risk:** Guardrail bug allows oversized position or trading during kill switch. **Mitigation:** Guardrail code is the most thoroughly tested component. 100% branch coverage required on `guardrails/limits.py` and `guardrails/kill_switch.py`. All guardrail tests run on every commit. **Risk level: HIGH.**
- **Risk:** Broker API changes break order submission silently. **Mitigation:** Broker adapters include response validation — if the broker response does not match expected schema, the order is treated as failed and logged. Never assume success without explicit confirmation from broker API. **Risk level: Medium.**
- **Risk:** Agent submits many small orders rapidly, each within guardrail limits individually but collectively creating excessive exposure. **Mitigation:** Add a `max_open_positions: int` guardrail (e.g., 5) and a `max_orders_per_hour: int` rate limit (e.g., 20). Both enforced in the guardrail layer. **Risk level: Medium.**
- **Risk:** Stale market data leads to guardrail calculations using wrong prices. **Mitigation:** `submit_order` guardrail checks fetch a fresh quote from the broker (not from Polygon cache) to compute position value. If the quote is older than 60 seconds, reject. **Risk level: Medium.**
- **Risk:** Partial fills leave positions in an ambiguous state. **Mitigation:** The portfolio tool always queries the broker for current positions, never relies on local state. The order history tool shows partial fill quantities. Agent instructions must include "always check positions after order submission." **Risk level: Medium.**
- **Risk:** Network failure during order submission — order may or may not have been received by broker. **Mitigation:** On network error, immediately query order status. If order is found, report status. If not found, report "order status unknown — check broker directly." Never retry an order automatically on network failure. **Risk level: HIGH.**
- **Risk:** PDT violation despite tracking — broker's count may differ from ours due to timing or corrections. **Mitigation:** Our PDT tracking is conservative (a safety net, not authoritative). The broker is the final arbiter. If our count says 2 and broker rejects for PDT, we log the discrepancy and update our state. **Risk level: Low.**

---

## Open questions

These require human decision before implementation begins.

### Phase 1
1. **Search provider:** Brave Search or Exa? Need to compare pricing and result quality for the use cases (technical documentation, financial research, SEC filings).

### Phase 2
2. **Pipeline server language:** Python (consistent with ne-body backend) or TypeScript (consistent with off-the-shelf MCP servers)? Python is recommended for consistency with existing skills and the Phase 3 trading server.

### Phase 3
3. **Paper trading first or live?** Recommendation: Start with Alpaca paper trading for all development and testing. Switch to live only after 2 weeks of paper trading with no guardrail issues. This is a human decision gate — the implementer should not enable live trading without explicit approval.

4. **Penny stock / OTC broker:** Alpaca has limited OTC support. Options:
   - (a) Alpaca for everything, accept limited OTC coverage
   - (b) IBKR for OTC (better coverage, more complex API, higher account minimums)
   - (c) Webull (OTC support unclear, API is unofficial)
   - Recommendation: Start with Alpaca (simplest API, good paper trading), evaluate OTC needs after initial strategy testing. Add IBKR adapter later if OTC coverage is insufficient.

5. **Backtesting framework:** Build custom or use an existing library (e.g., `vectorbt`, `backtrader`, `zipline-reloaded`)? Custom is simpler for MVP but less feature-rich. Recommendation: Use `vectorbt` for backtesting engine, wrap it behind the MCP tool interface. Justification: vectorbt is fast (vectorized), has good Polygon.io integration, and produces the metrics we need.

6. **Revenue target:** What is the monthly API cost budget that trading revenue needs to cover? This determines position sizing and strategy aggressiveness. Need a concrete number to configure guardrails.

7. **Starting capital allocation:** How much capital is allocated to the trading account? This directly affects `max_position_value_usd` and `max_drawdown_pct` guardrail parameters.

8. **E-Trade vs. Alpaca as primary broker:** E-Trade is existing but has a more complex API (OAuth 1.0a). Alpaca is simpler and has native paper trading. Recommendation: Alpaca as primary for Phase 3 MVP, E-Trade adapter as follow-on if equities strategy needs E-Trade-specific features.

9. **Agent autonomy level for execution:** Three options:
   - (a) Fully manual: agent recommends, human executes via broker UI
   - (b) Human-in-the-loop: agent submits orders via MCP, but a confirmation prompt appears before each order
   - (c) Autonomous: agent submits orders with guardrails as the only safety net
   - Recommendation: Start with (b) for live trading, (c) for paper trading. This is configurable via a `require_confirmation: bool` in the trading server config.

---

## Phasing and dependencies summary

```
Phase 1 (Research)          Phase 2 (Codebase)          Phase 3 (Trading)
  |                           |                           |
  1.1 Install servers         2.1 Install servers         3.1 Repo setup
  1.2 Register in settings    2.2 Configure filesystem    3.2 Market data tools
  1.3 Memory proxy            2.3 Configure git           3.3 Portfolio tools
  1.4 Choose search           2.4 Pipeline-tools server   3.4 Order tools
  1.5 Verify                  2.5 pipelines.json          3.5 Guardrails  <-- critical path
  |                           2.6 Verify                  3.6 Backtest tools
  |                           |                           3.7 Broker adapters
  |                           |                           3.8 Session separation
  |                           |                           3.9 Auth
  |                           |                           3.10 Agent orchestration
  
  Phase 1 -----> Phase 2 (Phase 2 uses Phase 1 memory)
  Phase 1 + 2 are independent of Phase 3
  Phase 3 can start in parallel with Phase 2 (no dependency)
  Phase 3 guardrails (3.5) must be complete before any order tool testing (3.4 integration tests)
```

Estimated effort:
- Phase 1: 1 day (mostly configuration, small custom proxy)
- Phase 2: 1-2 days (pipeline server is the bulk of the work)
- Phase 3: 5-7 days (custom server, guardrails, broker integration, testing)
  - Guardrails + tests alone: 2 days
  - Broker adapters: 2 days
  - Market data + backtest integration: 1-2 days
  - Agent orchestration + session separation: 1 day
