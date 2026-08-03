import { ArrowLeft, Receipt, RefreshCw } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { pythonStrategyApi } from '@/api/python-strategy'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { Execution, PythonStrategy, Trade, TradeDate } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

function formatTradeTime(value: string | undefined): string {
  if (!value) return '--'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function formatExecutionLabel(execution: Execution): string {
  if (execution.execution_id === 'legacy') {
    return `Legacy (before run tracking) • ${execution.trade_count} trade(s)`
  }
  const when = execution.start_time
    ? new Date(execution.start_time).toLocaleString('en-IN', {
        day: '2-digit',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      })
    : 'unknown time'
  return `Run #${execution.execution_id} • ${when} • ${execution.trade_count} trade(s)`
}

function formatTradeDateLabel(tradeDate: TradeDate): string {
  const when = new Date(`${tradeDate.date}T00:00:00`).toLocaleDateString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  })
  return `${when} • ${tradeDate.trade_count} trade(s)`
}

// Sentinel for "no execution filter" -- Radix Select can't use an empty
// string as an item value, and this is also the default view: an open
// position is tagged with whichever run OPENED it, which may not be the
// latest run (e.g. it was opened before a restart), so defaulting to
// "latest execution only" could hide a currently open leg entirely.
const ALL_EXECUTIONS = '__all__'

// Matches TODAY_EXECUTION_FILTER in blueprints/python_strategy.py's
// api_get_trades -- spans EVERY execution run whose entry_time falls on
// today's IST date, not just the latest run. Useful after a same-day
// restart produced more than one execution_id.
const TODAY_FILTER = '__today__'

// Trade-date dropdown items share the same Select as execution runs, so
// each is prefixed to distinguish it from an execution_id (which is either
// numeric or "legacy"/the two sentinels above -- never this prefix).
const DATE_VALUE_PREFIX = 'date:'

export default function PythonStrategyTrades() {
  const { strategyId } = useParams<{ strategyId: string }>()
  const [strategy, setStrategy] = useState<PythonStrategy | null>(null)
  const [executions, setExecutions] = useState<Execution[]>([])
  const [tradeDates, setTradeDates] = useState<TradeDate[]>([])
  const [selectedExecutionId, setSelectedExecutionId] = useState<string>(ALL_EXECUTIONS)
  const [selectedDate, setSelectedDate] = useState<string>('')
  const [trades, setTrades] = useState<Trade[]>([])
  const [totalPnl, setTotalPnl] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadingTrades, setLoadingTrades] = useState(false)

  // date takes priority over executionId on the backend when both are
  // passed -- pass whichever filter is currently active.
  const fetchTrades = async (executionId: string, date?: string) => {
    if (!strategyId) return
    try {
      setLoadingTrades(true)
      const tradesData = await pythonStrategyApi.getTrades(
        strategyId,
        executionId === ALL_EXECUTIONS ? undefined : executionId,
        date || undefined
      )
      setTrades(tradesData.trades)
      setTotalPnl(tradesData.total_pnl)
    } catch (_error) {
      showToast.error('Failed to load trades', 'pythonStrategy')
    } finally {
      setLoadingTrades(false)
    }
  }

  const fetchData = async () => {
    if (!strategyId) return
    try {
      setLoading(true)
      const [strategyData, executionsData, tradeDatesData] = await Promise.all([
        pythonStrategyApi.getStrategy(strategyId),
        pythonStrategyApi.getExecutions(strategyId),
        pythonStrategyApi.getTradeDates(strategyId),
      ])
      setStrategy(strategyData)
      setExecutions(executionsData.executions)
      setTradeDates(tradeDatesData.dates)
      // Default to the latest run's open + closed trades -- executions are
      // already sorted newest-first, and /executions folds in currently-open
      // legs too (see api_get_executions), so a run with an open position
      // but no closed trade yet still surfaces here as "latest".
      const latest = executionsData.executions[0]?.execution_id ?? ALL_EXECUTIONS
      setSelectedExecutionId(latest)
      await fetchTrades(latest)
    } catch (_error) {
      showToast.error('Failed to load trades', 'pythonStrategy')
    } finally {
      setLoading(false)
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: one-time initial load on mount; fetchData is recreated every render and must not retrigger this effect
  useEffect(() => {
    fetchData()
  }, [])

  // Live updates via the same multiplexed SSE stream the Errors page uses --
  // a trade closing while this page is open re-fetches the currently
  // selected view (run/Today/all) instead of requiring a manual Refresh.
  // pnl_update (pushed ~every 0.8s per running strategy) additionally patches
  // any OPEN row's LTP/PnL in place -- without this, an open position's
  // Exit/LTP column would only refresh when SOME trade closes (trade_update),
  // which could be minutes away, rather than ticking live like the PnL tile
  // on the dashboard already does from the same push.
  // biome-ignore lint/correctness/useExhaustiveDependencies: fetchTrades/selectedExecutionId/selectedDate change every render; re-subscribing on every change would drop/reopen the connection needlessly
  useEffect(() => {
    if (!strategyId) return
    const eventSource = new EventSource('/python/api/events')
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'trade_update' && data.strategy_id === strategyId) {
          fetchTrades(selectedExecutionId, selectedDate)
          return
        }
        if (data.type === 'pnl_update' && data.strategy_id === strategyId) {
          const openPositions: { leg_key: string; current_price: number; pnl: number }[] =
            data.open_positions || []
          if (openPositions.length === 0) return
          const byLeg = new Map(openPositions.map((p) => [p.leg_key, p]))
          setTrades((prev) =>
            prev.map((trade) => {
              if (trade.status !== 'OPEN') return trade
              const live = byLeg.get(trade.leg)
              if (!live) return trade
              return {
                ...trade,
                exit_px: String(live.current_price),
                pnl_rupees: live.pnl.toFixed(2),
              }
            })
          )
        }
      } catch {
        // ignore malformed events
      }
    }
    return () => eventSource.close()
  }, [strategyId])

  // Single dropdown covers both filter kinds -- a date-prefixed value
  // switches to the date filter (execution_id cleared, since the backend
  // treats date as taking priority anyway); anything else is an
  // execution_id (date filter cleared).
  const handleFilterChange = (value: string) => {
    if (value.startsWith(DATE_VALUE_PREFIX)) {
      const date = value.slice(DATE_VALUE_PREFIX.length)
      setSelectedDate(date)
      fetchTrades(selectedExecutionId, date)
    } else {
      setSelectedExecutionId(value)
      setSelectedDate('')
      fetchTrades(value)
    }
  }

  if (loading) {
    return (
      <div className="container mx-auto py-6 space-y-6">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-[400px]" />
      </div>
    )
  }

  if (!strategy) {
    return null
  }

  return (
    <div className="container mx-auto py-6 space-y-6">
      <Button variant="ghost" asChild>
        <Link to="/python">
          <ArrowLeft className="h-4 w-4 mr-2" />
          Back to Python Strategies
        </Link>
      </Button>

      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Trades</h1>
          <p className="text-muted-foreground">{strategy.name}</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {(executions.length > 0 || tradeDates.length > 0) && (
            <Select
              value={selectedDate ? `${DATE_VALUE_PREFIX}${selectedDate}` : selectedExecutionId}
              onValueChange={handleFilterChange}
            >
              <SelectTrigger className="w-[320px]">
                <SelectValue placeholder="Select a run or trade date" />
              </SelectTrigger>
              <SelectContent>
                {executions.length > 0 && (
                  <SelectGroup>
                    <SelectLabel>Runs</SelectLabel>
                    <SelectItem value={ALL_EXECUTIONS}>All runs (open + closed)</SelectItem>
                    <SelectItem value={TODAY_FILTER}>Today (all runs)</SelectItem>
                    {executions.map((execution) => (
                      <SelectItem key={execution.execution_id} value={execution.execution_id}>
                        {formatExecutionLabel(execution)}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                )}
                {tradeDates.length > 0 && (
                  <>
                    {executions.length > 0 && <SelectSeparator />}
                    <SelectGroup>
                      <SelectLabel>Trade Dates</SelectLabel>
                      {tradeDates.map((tradeDate) => (
                        <SelectItem
                          key={tradeDate.date}
                          value={`${DATE_VALUE_PREFIX}${tradeDate.date}`}
                        >
                          {formatTradeDateLabel(tradeDate)}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </>
                )}
              </SelectContent>
            </Select>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchTrades(selectedExecutionId, selectedDate)}
          >
            <RefreshCw className="h-4 w-4 mr-2" />
            Refresh
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm flex items-center justify-between">
            <span className="flex items-center gap-2">
              <Receipt className="h-4 w-4" />
              Trades (open + closed)
            </span>
            <span
              className={totalPnl >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}
            >
              Total (incl. unrealized): ₹{totalPnl.toFixed(2)}
            </span>
          </CardTitle>
          <CardDescription>
            {selectedDate
              ? `${trades.length} trade(s) on ${selectedDate}`
              : executions.length === 0
                ? 'No trades yet'
                : selectedExecutionId === ALL_EXECUTIONS
                  ? `${trades.length} trade(s) across all runs`
                  : selectedExecutionId === TODAY_FILTER
                    ? `${trades.length} trade(s) today (all runs)`
                    : `${trades.length} trade(s) in this run`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loadingTrades ? (
            <div className="flex items-center justify-center h-[200px]">
              <RefreshCw className="h-6 w-6 animate-spin" />
            </div>
          ) : trades.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              No trades in this run yet
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Status</TableHead>
                  <TableHead>Leg</TableHead>
                  <TableHead>Symbol</TableHead>
                  {trades[0]?.direction !== undefined && <TableHead>Direction</TableHead>}
                  <TableHead>Qty</TableHead>
                  <TableHead>Entry Time</TableHead>
                  <TableHead>Entry</TableHead>
                  <TableHead>Exit Time</TableHead>
                  <TableHead>LTP/Exit</TableHead>
                  <TableHead>PnL (pts)</TableHead>
                  <TableHead>PnL (₹)</TableHead>
                  <TableHead>Reason</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {trades.map((trade, i) => {
                  const pnlRupees = Number.parseFloat(trade.pnl_rupees)
                  const isOpen = trade.status === 'OPEN'
                  return (
                    // biome-ignore lint/suspicious/noArrayIndexKey: trade rows have no stable unique id in the CSV
                    <TableRow key={i}>
                      <TableCell>
                        <span
                          className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                            isOpen
                              ? 'bg-blue-500/15 text-blue-600 dark:text-blue-400'
                              : 'bg-muted text-muted-foreground'
                          }`}
                        >
                          {isOpen ? 'OPEN' : 'CLOSED'}
                        </span>
                      </TableCell>
                      <TableCell className="font-medium">{trade.leg}</TableCell>
                      <TableCell className="font-mono text-xs">{trade.symbol}</TableCell>
                      {trade.direction !== undefined && <TableCell>{trade.direction}</TableCell>}
                      <TableCell>{trade.quantity}</TableCell>
                      <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                        {formatTradeTime(trade.entry_time)}
                      </TableCell>
                      <TableCell>{trade.entry_px}</TableCell>
                      <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                        {isOpen ? '--' : formatTradeTime(trade.exit_time)}
                      </TableCell>
                      <TableCell>{trade.exit_px}</TableCell>
                      <TableCell>{isOpen ? '--' : trade.pnl_points}</TableCell>
                      <TableCell
                        className={
                          pnlRupees >= 0
                            ? 'text-green-600 dark:text-green-400'
                            : 'text-red-600 dark:text-red-400'
                        }
                      >
                        ₹{trade.pnl_rupees}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {isOpen ? 'still open' : trade.exit_reason}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
