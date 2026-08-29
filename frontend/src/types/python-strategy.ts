// Python Strategy Types

export interface PythonStrategy {
  id: string
  name: string
  file_name: string
  exchange: string
  status: 'stopped' | 'running' | 'error' | 'scheduled' | 'paused' | 'manually_stopped'
  status_message?: string
  process_id: number | null
  last_started: string | null
  last_stopped: string | null
  error_message: string | null
  is_scheduled: boolean
  manually_stopped?: boolean
  schedule_start_time: string | null
  schedule_stop_time: string | null
  schedule_days: string[]
  created_at: string
  updated_at: string
}

export interface PythonStrategyContent {
  id: string
  name: string
  file_name: string
  content: string
  line_count: number
  size_kb: number
  last_modified: string
}

export interface LogFile {
  name: string
  path: string
  size_kb: number
  last_modified: string
}

export interface LogContent {
  content: string
  lines: number
  size_kb: number
  last_updated: string
}

export interface OpenPosition {
  leg_key: string
  symbol: string
  direction: 'LONG' | 'SHORT'
  quantity: number
  entry_price: number
  current_price: number
  pnl: number
  entry_time?: string
  execution_id?: number | string
}

export interface PnlSnapshot {
  realized_pnl: number
  unrealized_pnl: number
  total_pnl: number
  open_positions: OpenPosition[]
  updated_at: string | null
}

// Row shape from trades_{strategy_id}.csv, read generically -- column set
// can drift slightly between the 5 strategy scripts, so only the fields
// every script's trade-log writer guarantees are typed strictly; anything
// else passes through as an optional string.
export interface Trade {
  leg: string
  symbol: string
  quantity: string
  direction?: string
  entry_time: string
  entry_px: string
  exit_time: string
  exit_px: string
  pnl_points: string
  pnl_rupees: string
  exit_reason: string
  execution_id: string
  status?: 'OPEN' | 'CLOSED'
  // Dual-timeframe strategies only (e.g. TFTT) -- present when the CSV
  // trade log carries these columns, absent (undefined) for every other
  // strategy's rows. See PythonStrategyTrades.tsx's conditional TableHead.
  entry_timeframe?: string
  controlling_timeframe_at_exit?: string
  handoff_occurred?: string
  handoff_ts?: string
  sl_pct_amount?: string
  sl_pct_level?: string
  sl_candle_reference_high?: string
  sl_candle_reference_low?: string
  [key: string]: string | undefined
}

export interface TradesResponse {
  trades: Trade[]
  total_pnl: number
}

// One process run of the strategy -- every trade is tagged with whichever
// run OPENED it (see execution_id on Trade). "legacy" groups trades logged
// before this tracking existed.
export interface Execution {
  execution_id: string
  start_time: string | null
  trade_count: number
  total_pnl: number
}

export interface ExecutionsResponse {
  executions: Execution[]
}

// A distinct trade (entry) date found in the strategy's trade log, for the
// Trades page's date-filter dropdown -- spans every execution_id.
export interface TradeDate {
  date: string
  trade_count: number
}

export interface TradeDatesResponse {
  dates: TradeDate[]
}

// A leg the strategy has pushed into error mode -- poll_fill() exhausted its
// automatic retries. See docs/prd/python-strategies-order-error-recovery.md.
export interface LegError {
  leg_key: string
  error_state: 'entry_failed' | 'exit_failed' | ''
  error_kind: 'terminal' | 'resting' | ''
  error_message: string
  error_since: string
  symbol: string
  quantity: number
  action: string // BUY/SELL the leg was attempting when it failed
}

export interface LegErrorsResponse {
  errors: LegError[]
}

export type LegAction = 'retry' | 'cancel' | 'manual'

export interface EnvironmentVariables {
  regular: Record<string, string>
  secure: Record<string, string>
}

export interface ScheduleConfig {
  start_time: string
  stop_time: string
  days: string[]
  exchange?: string
}

// Exchanges that drive the strategy's calendar/holiday awareness in /python.
// The session window shown against each one is served by /python/api/exchanges
// from the market calendar DB, so an exchange timing change (SEBI moving the
// F&O close to 15:40, or an admin edit under /admin/timings) reaches this
// dropdown with no code change. Per-date overrides (partial holidays, Muhurat
// sessions) are applied by the backend scheduler from the same DB.
export interface StrategyExchange {
  value: string
  label: string
  /** Descriptive segment name, e.g. 'NSE F&O'. Null for CRYPTO. */
  description: string | null
  /** Session open as HH:MM IST. Null for 24/7 exchanges. */
  start_time: string | null
  /** Session close as HH:MM IST. Null for 24/7 exchanges. */
  end_time: string | null
  /** Human-readable window, e.g. '09:15-15:40' or '24/7'. */
  window: string | null
  is_24x7: boolean
}

// Shown only until /python/api/exchanges responds. Deliberately carries no
// timings: a stale hardcoded window is worse than none, and the real values
// arrive a moment later.
export const FALLBACK_STRATEGY_EXCHANGES: StrategyExchange[] = [
  { value: 'NSE', label: 'NSE — Equity', description: 'Equity' },
  { value: 'BSE', label: 'BSE — Equity', description: 'Equity' },
  { value: 'NFO', label: 'NFO — NSE F&O', description: 'NSE F&O' },
  { value: 'BFO', label: 'BFO — BSE F&O', description: 'BSE F&O' },
  { value: 'CDS', label: 'CDS — NSE Currency', description: 'NSE Currency' },
  { value: 'BCD', label: 'BCD — BSE Currency', description: 'BSE Currency' },
  { value: 'MCX', label: 'MCX — Commodity', description: 'Commodity' },
  { value: 'NCO', label: 'NCO — NSE Commodity', description: 'NSE Commodity' },
  { value: 'CRYPTO', label: 'CRYPTO — 24/7', description: null },
].map((e) => ({
  ...e,
  start_time: null,
  end_time: null,
  window: e.value === 'CRYPTO' ? '24/7' : null,
  is_24x7: e.value === 'CRYPTO',
}))

export const CRYPTO_EXCHANGE_VALUE = 'CRYPTO'

export interface MasterContractStatus {
  ready: boolean
  message: string
  last_updated: string | null
}

export const SCHEDULE_DAYS = [
  { value: 'mon', label: 'Monday' },
  { value: 'tue', label: 'Tuesday' },
  { value: 'wed', label: 'Wednesday' },
  { value: 'thu', label: 'Thursday' },
  { value: 'fri', label: 'Friday' },
  { value: 'sat', label: 'Saturday' },
  { value: 'sun', label: 'Sunday' },
] as const

export const STATUS_COLORS: Record<string, string> = {
  running: 'bg-green-500',
  stopped: 'bg-gray-500',
  error: 'bg-red-500',
  scheduled: 'bg-blue-500',
  paused: 'bg-yellow-500',
  manually_stopped: 'bg-orange-500',
}

export const STATUS_LABELS: Record<string, string> = {
  running: 'Running',
  stopped: 'Stopped',
  error: 'Error',
  scheduled: 'Scheduled',
  paused: 'Paused',
  manually_stopped: 'Manual Stop',
}
