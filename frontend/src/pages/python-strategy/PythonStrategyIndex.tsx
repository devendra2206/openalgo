import {
  AlertTriangle,
  Calendar,
  ChevronDown,
  ChevronUp,
  Clock,
  Download,
  FileCode,
  FileText,
  HelpCircle,
  MoreVertical,
  OctagonX,
  Pencil,
  Play,
  Plus,
  Receipt,
  RefreshCw,
  Square,
  Trash2,
  Wallet,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { pythonStrategyApi } from '@/api/python-strategy'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type {
  MasterContractStatus,
  OpenPosition,
  PnlSnapshot,
  PythonStrategy,
  Trade,
} from '@/types/python-strategy'
import { SCHEDULE_DAYS, STATUS_COLORS, STATUS_LABELS } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

export default function PythonStrategyIndex() {
  const navigate = useNavigate()
  const [strategies, setStrategies] = useState<PythonStrategy[]>([])
  const [pnlByStrategy, setPnlByStrategy] = useState<Record<string, PnlSnapshot>>({})
  const [errorCountByStrategy, setErrorCountByStrategy] = useState<Record<string, number>>({})
  const [todayTradesByStrategy, setTodayTradesByStrategy] = useState<Record<string, Trade[]>>({})
  const [expandedTrades, setExpandedTrades] = useState<Set<string>>(new Set())
  const [masterStatus, setMasterStatus] = useState<MasterContractStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [strategyToDelete, setStrategyToDelete] = useState<PythonStrategy | null>(null)
  const [forceExitDialogOpen, setForceExitDialogOpen] = useState(false)
  const [strategyToForceExit, setStrategyToForceExit] = useState<PythonStrategy | null>(null)
  const [currentTime, setCurrentTime] = useState(new Date())

  const fetchData = async (silent = false) => {
    try {
      if (!silent) setLoading(true)
      const [strategiesData, statusData] = await Promise.all([
        pythonStrategyApi.getStrategies(),
        pythonStrategyApi.getMasterContractStatus(),
      ])
      setStrategies(strategiesData)
      setMasterStatus(statusData)

      // Fetch PnL snapshots for running strategies only -- a stopped
      // strategy isn't pushing updates, so its last-known snapshot (if any)
      // stays as-is rather than being re-fetched every refresh.
      const running = strategiesData.filter((s) => s.status === 'running')
      const pnlResults = await Promise.allSettled(
        running.map((s) => pythonStrategyApi.getPnl(s.id))
      )
      setPnlByStrategy((prev) => {
        const next = { ...prev }
        running.forEach((s, i) => {
          const result = pnlResults[i]
          if (result.status === 'fulfilled') {
            next[s.id] = result.value
          }
        })
        return next
      })

      // Same pattern for error-mode legs -- only running strategies can have
      // one, and this doubles as the initial load for a badge that's
      // otherwise kept live via the error_update SSE event below.
      const errorResults = await Promise.allSettled(
        running.map((s) => pythonStrategyApi.getErrors(s.id))
      )
      setErrorCountByStrategy((prev) => {
        const next = { ...prev }
        running.forEach((s, i) => {
          const result = errorResults[i]
          if (result.status === 'fulfilled') {
            next[s.id] = result.value.errors.length
          }
        })
        return next
      })

      // Same pattern again for today's trades -- initial load for the
      // Today's Trades panel's count/list, otherwise kept live via the
      // trade_update SSE event below.
      const tradesResults = await Promise.allSettled(
        running.map((s) => pythonStrategyApi.getTrades(s.id, '__today__'))
      )
      setTodayTradesByStrategy((prev) => {
        const next = { ...prev }
        running.forEach((s, i) => {
          const result = tradesResults[i]
          if (result.status === 'fulfilled') {
            next[s.id] = result.value.trades
          }
        })
        return next
      })
    } catch (_error) {
      if (!silent) showToast.error('Failed to load strategies', 'pythonStrategy')
    } finally {
      if (!silent) setLoading(false)
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: mount-only init of the 1s timer and SSE subscription; adding fetchData would tear down and recreate the EventSource on every render
  useEffect(() => {
    fetchData()
    // Update current time every second
    const timer = setInterval(() => setCurrentTime(new Date()), 1000)

    // Subscribe to SSE for real-time status updates
    const eventSource = new EventSource('/python/api/events')

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'connected') {
          return
        }

        // Live PnL push -- update just this strategy's snapshot in place,
        // no full refetch (a status change still triggers the branch below).
        if (data.type === 'pnl_update' && data.strategy_id) {
          setPnlByStrategy((prev) => ({
            ...prev,
            [data.strategy_id]: {
              realized_pnl: data.realized_pnl,
              unrealized_pnl: data.unrealized_pnl,
              total_pnl: data.total_pnl,
              open_positions: data.open_positions,
              updated_at: data.updated_at,
            },
          }))

          // This same push already carries each open leg's live current_price
          // and pnl (~every 0.8s) -- patch a matching OPEN row in place so its
          // LTP/PnL actually ticks live, AND append any leg not already in
          // the list as a fresh OPEN row. Without the append half, a leg that
          // enters while this page is already open (no prior fetch, no
          // trade_update -- that only fires on EXIT) would never appear in
          // the Today's Trades panel at all, including its collapsible
          // toggle staying hidden even though a real open position exists.
          const openPositions: OpenPosition[] = data.open_positions || []
          if (openPositions.length > 0) {
            setTodayTradesByStrategy((prev) => {
              const existing = prev[data.strategy_id] ?? []
              const byLeg = new Map(existing.map((t) => [t.leg, t]))
              let anyPatched = false
              const patched = existing.map((trade) => {
                if (trade.status !== 'OPEN') return trade
                const live = openPositions.find((p) => p.leg_key === trade.leg)
                if (!live) return trade
                anyPatched = true
                return {
                  ...trade,
                  exit_px: String(live.current_price),
                  pnl_rupees: live.pnl.toFixed(2),
                }
              })
              const newRows: Trade[] = openPositions
                .filter((p) => !byLeg.has(p.leg_key))
                .map((p) => ({
                  leg: p.leg_key,
                  symbol: p.symbol,
                  quantity: String(p.quantity),
                  direction: p.direction,
                  entry_time: p.entry_time ?? '',
                  entry_px: String(p.entry_price),
                  exit_time: '',
                  exit_px: String(p.current_price),
                  pnl_points: '',
                  pnl_rupees: p.pnl.toFixed(2),
                  exit_reason: '',
                  execution_id: p.execution_id != null ? String(p.execution_id) : 'legacy',
                  status: 'OPEN' as const,
                }))
              if (!anyPatched && newRows.length === 0) return prev
              return { ...prev, [data.strategy_id]: [...patched, ...newRows] }
            })
          }
          return
        }

        // A leg entered/left error mode -- re-fetch just this strategy's
        // error count rather than trying to reconstruct it from the partial
        // per-leg event payload.
        if (data.type === 'error_update' && data.strategy_id) {
          pythonStrategyApi
            .getErrors(data.strategy_id)
            .then((res) => {
              setErrorCountByStrategy((prev) => ({
                ...prev,
                [data.strategy_id]: res.errors.length,
              }))
            })
            .catch(() => {})
          return
        }

        // A leg just closed -- re-fetch this strategy's today's-trades list
        // (deliberately no trade data carried on the event itself, same
        // re-fetch-on-notify style as error_update above) so the Today's
        // Trades panel updates live regardless of whether it's currently
        // expanded or collapsed (the collapsed count still needs to stay
        // current).
        if (data.type === 'trade_update' && data.strategy_id) {
          pythonStrategyApi
            .getTrades(data.strategy_id, '__today__')
            .then((res) => {
              setTodayTradesByStrategy((prev) => ({
                ...prev,
                [data.strategy_id]: res.trades,
              }))
            })
            .catch(() => {})
          return
        }

        // Refresh data silently when we receive a status update
        if (data.strategy_id && data.status) {
          fetchData(true) // Silent refresh
        }
      } catch (_e) {
        // Ignore parse errors (heartbeat messages)
      }
    }

    eventSource.onerror = () => {}

    return () => {
      clearInterval(timer)
      eventSource.close()
    }
  }, [])

  const handleStart = async (strategy: PythonStrategy) => {
    try {
      setActionLoading(strategy.id)
      const response = await pythonStrategyApi.startStrategy(strategy.id)
      if (response.status === 'success') {
        // Use response message which differs for immediate start vs armed for schedule
        showToast.success(response.message || `Strategy ${strategy.name} started`, 'pythonStrategy')
        fetchData()
      } else {
        showToast.error(response.message || 'Failed to start strategy', 'pythonStrategy')
      }
    } catch (error: unknown) {
      // Extract error message from Axios response
      const axiosError = error as { response?: { data?: { message?: string } } }
      const errorMessage = axiosError.response?.data?.message || 'Failed to start strategy'
      showToast.error(errorMessage, 'pythonStrategy')
    } finally {
      setActionLoading(null)
    }
  }

  const handleStop = async (strategy: PythonStrategy) => {
    try {
      setActionLoading(strategy.id)
      const response = await pythonStrategyApi.stopStrategy(strategy.id)
      if (response.status === 'success') {
        // Use response message which differs for running vs scheduled strategies
        showToast.success(response.message || `Strategy ${strategy.name} stopped`, 'pythonStrategy')
        fetchData()
      } else {
        showToast.error(response.message || 'Failed to stop strategy', 'pythonStrategy')
      }
    } catch (_error) {
      showToast.error('Failed to stop strategy', 'pythonStrategy')
    } finally {
      setActionLoading(null)
    }
  }

  const handleClearError = async (strategy: PythonStrategy) => {
    try {
      setActionLoading(strategy.id)
      const response = await pythonStrategyApi.clearError(strategy.id)
      if (response.status === 'success') {
        showToast.success('Error cleared', 'pythonStrategy')
        fetchData()
      } else {
        showToast.error(response.message || 'Failed to clear error', 'pythonStrategy')
      }
    } catch (_error) {
      showToast.error('Failed to clear error', 'pythonStrategy')
    } finally {
      setActionLoading(null)
    }
  }

  const handleDelete = async () => {
    if (!strategyToDelete) return
    try {
      setActionLoading(strategyToDelete.id)
      const response = await pythonStrategyApi.deleteStrategy(strategyToDelete.id)
      if (response.status === 'success') {
        showToast.success('Strategy deleted', 'pythonStrategy')
        setStrategies(strategies.filter((s) => s.id !== strategyToDelete.id))
      } else {
        showToast.error(response.message || 'Failed to delete strategy', 'pythonStrategy')
      }
    } catch (_error) {
      showToast.error('Failed to delete strategy', 'pythonStrategy')
    } finally {
      setActionLoading(null)
      setDeleteDialogOpen(false)
      setStrategyToDelete(null)
    }
  }

  const handleForceExit = async () => {
    if (!strategyToForceExit) return
    try {
      setActionLoading(strategyToForceExit.id)
      const response = await pythonStrategyApi.forceExitStrategy(strategyToForceExit.id)
      if (response.status === 'success') {
        showToast.success(
          'Force exit requested -- closing all open positions, then stopping',
          'pythonStrategy'
        )
      } else {
        showToast.error(response.message || 'Failed to request force exit', 'pythonStrategy')
      }
    } catch (error: unknown) {
      const axiosError = error as { response?: { data?: { message?: string } } }
      const errorMessage = axiosError.response?.data?.message || 'Failed to request force exit'
      showToast.error(errorMessage, 'pythonStrategy')
    } finally {
      setActionLoading(null)
      setForceExitDialogOpen(false)
      setStrategyToForceExit(null)
    }
  }

  const toggleTodaysTrades = (strategyId: string) => {
    setExpandedTrades((prev) => {
      const next = new Set(prev)
      if (next.has(strategyId)) {
        next.delete(strategyId)
      } else {
        next.add(strategyId)
      }
      return next
    })
  }

  const handleExport = async (strategy: PythonStrategy) => {
    try {
      const blob = await pythonStrategyApi.exportStrategy(strategy.id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = strategy.file_name
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      showToast.success('Strategy exported', 'pythonStrategy')
    } catch (_error) {
      showToast.error('Failed to export strategy', 'pythonStrategy')
    }
  }

  const handleCheckContracts = async () => {
    try {
      setActionLoading('master')
      const response = await pythonStrategyApi.checkAndStartPending()
      if (response.status === 'success') {
        const started = response.data?.started || 0
        showToast.success(`Started ${started} pending strategies`, 'pythonStrategy')
        fetchData()
      } else {
        showToast.error(response.message || 'Failed to check contracts', 'pythonStrategy')
      }
    } catch (_error) {
      showToast.error('Failed to check contracts', 'pythonStrategy')
    } finally {
      setActionLoading(null)
    }
  }

  const formatScheduleDays = (days: string[]) => {
    if (!days || days.length === 0) return ''
    if (days.length === 7) return 'Every day'
    if (days.length === 5 && !days.includes('sat') && !days.includes('sun')) return 'Weekdays'
    return days
      .map((d) => SCHEDULE_DAYS.find((sd) => sd.value === d)?.label.slice(0, 3) || d)
      .join(', ')
  }

  const formatTime = (timeStr: string | null) => {
    if (!timeStr) return '-'
    return new Date(timeStr).toLocaleString('en-IN', {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  // Stats
  const stats = {
    total: strategies.length,
    running: strategies.filter((s) => s.status === 'running').length,
    scheduled: strategies.filter((s) => s.is_scheduled).length,
    // Sum of live PnL across currently-running strategies only -- a stopped
    // strategy's last-known snapshot isn't included, since it's no longer
    // contributing to today's live total. Same values already shown on each
    // card's PNL button, just aggregated here.
    totalPnl: strategies
      .filter((s) => s.status === 'running')
      .reduce((sum, s) => sum + (pnlByStrategy[s.id]?.total_pnl ?? 0), 0),
  }

  if (loading) {
    return (
      <div className="container mx-auto py-6 space-y-6">
        <div className="flex justify-between items-center">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-10 w-32" />
        </div>
        <div className="grid gap-4 md:grid-cols-4">
          {[1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
        <div className="grid gap-4 grid-cols-1">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-64" />
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="container mx-auto py-6 space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Python Strategies</h1>
          <p className="text-muted-foreground">Manage and run your Python trading scripts</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => navigate('/python/guide')}>
            <HelpCircle className="h-4 w-4 mr-2" />
            Guide
          </Button>
          <Button variant="outline" size="sm" onClick={() => fetchData()}>
            <RefreshCw className="h-4 w-4 mr-2" />
            Refresh
          </Button>
          <Button onClick={() => navigate('/python/new')}>
            <Plus className="h-4 w-4 mr-2" />
            Add Strategy
          </Button>
        </div>
      </div>

      {/* Stats Bar */}
      <div className="grid gap-4 grid-cols-2 md:grid-cols-5">
        <Card>
          <CardContent className="pt-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Total</p>
                <p className="text-2xl font-bold">{stats.total}</p>
              </div>
              <FileCode className="h-8 w-8 text-muted-foreground" />
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Running</p>
                <p className="text-2xl font-bold text-green-500">{stats.running}</p>
              </div>
              <Play className="h-8 w-8 text-green-500" />
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Scheduled</p>
                <p className="text-2xl font-bold text-blue-500">{stats.scheduled}</p>
              </div>
              <Calendar className="h-8 w-8 text-blue-500" />
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Master Contract</p>
                <Badge variant={masterStatus?.ready ? 'default' : 'secondary'}>
                  {masterStatus?.ready ? 'Ready' : 'Not Ready'}
                </Badge>
              </div>
              {!masterStatus?.ready && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={handleCheckContracts}
                  disabled={actionLoading === 'master'}
                >
                  Check
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-muted-foreground">Total PNL</p>
                <p
                  className={`text-2xl font-bold ${stats.totalPnl >= 0 ? 'text-green-500' : 'text-red-500'}`}
                >
                  {stats.totalPnl >= 0 ? '+' : ''}
                  {'₹'}
                  {stats.totalPnl.toFixed(0)}
                </p>
              </div>
              <Wallet
                className={`h-8 w-8 ${stats.totalPnl >= 0 ? 'text-green-500' : 'text-red-500'}`}
              />
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Current Time */}
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Clock className="h-4 w-4" />
        Current IST: {currentTime.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })}
      </div>

      {/* Strategies Grid */}
      {strategies.length === 0 ? (
        <Card className="py-12">
          <CardContent className="flex flex-col items-center justify-center text-center">
            <FileCode className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold mb-2">No Python Strategies</h3>
            <p className="text-muted-foreground mb-4">
              Upload your first Python trading script to get started.
            </p>
            <Button onClick={() => navigate('/python/new')}>
              <Plus className="h-4 w-4 mr-2" />
              Add Strategy
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 grid-cols-1 items-start">
          {strategies.map((strategy) => (
            <Card key={strategy.id} className="relative overflow-hidden flex flex-col">
              {/* Status bar */}
              <div
                className={`absolute top-0 left-0 right-0 h-1 ${STATUS_COLORS[strategy.status]}`}
              />

              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2 overflow-hidden">
                  <div className="min-w-0 flex-1 space-y-1">
                    <CardTitle className="text-lg truncate">{strategy.name}</CardTitle>
                    <CardDescription className="font-mono text-xs truncate">
                      {strategy.file_name}
                    </CardDescription>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <Tooltip>
                      <TooltipTrigger>
                        <Badge
                          variant={strategy.status === 'running' ? 'default' : 'secondary'}
                          className={`${STATUS_COLORS[strategy.status] || ''} whitespace-nowrap`}
                        >
                          {STATUS_LABELS[strategy.status] || strategy.status}
                        </Badge>
                      </TooltipTrigger>
                      <TooltipContent>
                        {strategy.status_message || STATUS_LABELS[strategy.status]}
                      </TooltipContent>
                    </Tooltip>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon" aria-label="Strategy actions menu">
                          <MoreVertical className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onClick={() => handleExport(strategy)}>
                          <Download className="h-4 w-4 mr-2" />
                          Export
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          className="text-red-500"
                          disabled={strategy.status === 'running'}
                          onClick={() => {
                            setStrategyToDelete(strategy)
                            setDeleteDialogOpen(true)
                          }}
                        >
                          <Trash2 className="h-4 w-4 mr-2" />
                          Delete
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>
              </CardHeader>

              <CardContent className="space-y-4 flex-1 flex flex-col">
                {/* Schedule Info - always show */}
                <div className="text-sm p-2 rounded min-h-[52px] bg-blue-500/10 border border-blue-500/20">
                  <div className="flex items-center gap-2">
                    <Calendar className="h-4 w-4 text-blue-500" />
                    <span>
                      {strategy.schedule_start_time || '09:00'}
                      {' - '}
                      {strategy.schedule_stop_time || '16:00'}
                    </span>
                    {strategy.exchange && (
                      <span className="ml-auto px-1.5 py-0.5 text-[10px] font-medium rounded bg-blue-500/20 text-blue-700 dark:text-blue-300">
                        {strategy.exchange}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1">
                    {formatScheduleDays(
                      strategy.schedule_days?.length
                        ? strategy.schedule_days
                        : ['mon', 'tue', 'wed', 'thu', 'fri']
                    )}
                  </p>
                </div>

                {/* Error Message */}
                {strategy.status === 'error' && strategy.error_message && (
                  <Alert variant="destructive">
                    <AlertTriangle className="h-4 w-4" />
                    <AlertDescription className="text-xs">
                      {strategy.error_message}
                      <Button
                        variant="link"
                        size="sm"
                        className="p-0 h-auto ml-2"
                        onClick={() => handleClearError(strategy)}
                      >
                        Clear
                      </Button>
                    </AlertDescription>
                  </Alert>
                )}

                {/* Timestamps */}
                <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground mt-auto">
                  <div>
                    <span className="block">Last Started</span>
                    <span>{formatTime(strategy.last_started)}</span>
                  </div>
                  <div>
                    <span className="block">Last Stopped</span>
                    <span>{formatTime(strategy.last_stopped)}</span>
                  </div>
                </div>

                {/* Action Buttons */}
                <div className="flex flex-wrap gap-2 pt-2 mt-auto">
                  {strategy.status === 'running' || strategy.status === 'scheduled' ? (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant={strategy.status === 'running' ? 'destructive' : 'outline'}
                          size="sm"
                          className={`flex-1 ${strategy.status === 'scheduled' ? 'border-orange-500 text-orange-600 hover:bg-orange-50 dark:text-orange-400 dark:hover:bg-orange-950' : ''}`}
                          onClick={() => handleStop(strategy)}
                          disabled={actionLoading === strategy.id}
                        >
                          <Square className="h-4 w-4 mr-2" />
                          {strategy.status === 'running' ? 'Stop' : 'Cancel'}
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        {strategy.status === 'running'
                          ? 'Stop running strategy'
                          : 'Cancel scheduled auto-start'}
                      </TooltipContent>
                    </Tooltip>
                  ) : (
                    <></>
                  )}

                  {strategy.status === 'running' && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="destructive"
                          size="sm"
                          className="flex-1 border border-red-800"
                          onClick={() => {
                            setStrategyToForceExit(strategy)
                            setForceExitDialogOpen(true)
                          }}
                          disabled={actionLoading === strategy.id}
                        >
                          <OctagonX className="h-4 w-4 mr-1" />
                          <span className="text-xs">Force Exit</span>
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        Close every open position (short legs first, then long) and stop
                      </TooltipContent>
                    </Tooltip>
                  )}

                  {strategy.status !== 'running' && strategy.status !== 'scheduled' && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="default"
                          size="sm"
                          className="flex-1 bg-green-600 hover:bg-green-700"
                          onClick={() => handleStart(strategy)}
                          disabled={actionLoading === strategy.id}
                        >
                          <Play className="h-4 w-4 mr-2" />
                          Start
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Start strategy</TooltipContent>
                    </Tooltip>
                  )}

                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="outline"
                        size="sm"
                        className="flex-1 border-blue-500 text-blue-600 hover:bg-blue-50 dark:text-blue-400 dark:hover:bg-blue-950"
                        asChild
                        disabled={strategy.status === 'running'}
                      >
                        <Link to={`/python/${strategy.id}/schedule`}>
                          <Pencil className="h-4 w-4 mr-1" />
                          <span className="text-xs">Schedule</span>
                        </Link>
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                      {strategy.schedule_start_time && strategy.schedule_stop_time
                        ? `${strategy.schedule_start_time} - ${strategy.schedule_stop_time}`
                        : 'Edit schedule'}
                    </TooltipContent>
                  </Tooltip>

                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        variant="outline"
                        size="sm"
                        asChild
                        className={`flex-1 ${
                          pnlByStrategy[strategy.id]
                            ? pnlByStrategy[strategy.id].total_pnl >= 0
                              ? 'border-green-500 text-green-600 hover:bg-green-50 dark:text-green-400 dark:hover:bg-green-950'
                              : 'border-red-500 text-red-600 hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950'
                            : ''
                        }`}
                      >
                        <Link to={`/python/${strategy.id}/pnl`}>
                          <Wallet className="h-4 w-4 mr-1" />
                          <span className="text-xs">
                            {pnlByStrategy[strategy.id]
                              ? `₹${pnlByStrategy[strategy.id].total_pnl.toFixed(0)}`
                              : 'PNL'}
                          </span>
                        </Link>
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                      {pnlByStrategy[strategy.id]
                        ? `Realized ₹${pnlByStrategy[strategy.id].realized_pnl.toFixed(0)} + Unrealized ₹${pnlByStrategy[strategy.id].unrealized_pnl.toFixed(0)}`
                        : 'View PnL'}
                    </TooltipContent>
                  </Tooltip>

                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button variant="outline" size="sm" className="flex-1" asChild>
                        <Link to={`/python/${strategy.id}/trades`}>
                          <Receipt className="h-4 w-4 mr-1" />
                          <span className="text-xs">Trades</span>
                        </Link>
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Today's trades</TooltipContent>
                  </Tooltip>

                  {!!errorCountByStrategy[strategy.id] && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          variant="destructive"
                          size="sm"
                          asChild
                          className="flex-1 animate-pulse"
                        >
                          <Link to={`/python/${strategy.id}/errors`}>
                            <AlertTriangle className="h-4 w-4 mr-1" />
                            <span className="text-xs">
                              {errorCountByStrategy[strategy.id]} needs attention
                            </span>
                          </Link>
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>
                        An order failed and needs Retry/Cancel/Manually Completed
                      </TooltipContent>
                    </Tooltip>
                  )}

                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button variant="outline" size="sm" className="flex-1" asChild>
                        <Link to={`/python/${strategy.id}/logs`}>
                          <FileText className="h-4 w-4 mr-1" />
                          <span className="text-xs">Logs</span>
                        </Link>
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>View logs</TooltipContent>
                  </Tooltip>

                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button variant="outline" size="sm" className="flex-1" asChild>
                        <Link to={`/python/${strategy.id}/edit`}>
                          <FileCode className="h-4 w-4 mr-1" />
                          <span className="text-xs">Edit</span>
                        </Link>
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Edit code</TooltipContent>
                  </Tooltip>
                </div>

                {(todayTradesByStrategy[strategy.id]?.length ?? 0) > 0 && (
                  <div className="mt-3 pt-3 border-t">
                    <button
                      type="button"
                      onClick={() => toggleTodaysTrades(strategy.id)}
                      className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                    >
                      {expandedTrades.has(strategy.id) ? (
                        <ChevronUp className="h-3 w-3" />
                      ) : (
                        <ChevronDown className="h-3 w-3" />
                      )}
                      Today's Trades ({todayTradesByStrategy[strategy.id]?.length ?? 0})
                    </button>

                    {expandedTrades.has(strategy.id) && (
                      <div className="mt-2 overflow-x-auto">
                        <table className="w-full text-xs">
                          <thead>
                            <tr className="text-muted-foreground border-b">
                              <th className="text-left py-1 pr-2 font-medium">Leg</th>
                              <th className="text-left py-1 px-2 font-medium">Symbol</th>
                              <th className="text-right py-1 px-2 font-medium">Qty</th>
                              <th className="text-right py-1 px-2 font-medium">Entry</th>
                              <th className="text-right py-1 px-2 font-medium">LTP/Exit</th>
                              <th className="text-right py-1 px-2 font-medium">PnL</th>
                              <th className="text-left py-1 pl-2 font-medium">Reason</th>
                            </tr>
                          </thead>
                          <tbody>
                            {[...todayTradesByStrategy[strategy.id]]
                              .reverse()
                              .slice(0, 5)
                              .map((trade, i) => {
                                const pnlRupees = Number.parseFloat(trade.pnl_rupees)
                                const isOpen = trade.status === 'OPEN'
                                return (
                                  // biome-ignore lint/suspicious/noArrayIndexKey: trade rows have no stable unique id in the CSV
                                  <tr key={i} className="border-b last:border-0">
                                    <td className="py-1 pr-2 font-medium">{trade.leg}</td>
                                    <td className="py-1 px-2 font-mono text-[10px] text-muted-foreground">
                                      {trade.symbol}
                                    </td>
                                    <td className="text-right py-1 px-2">{trade.quantity}</td>
                                    <td className="text-right py-1 px-2">{trade.entry_px}</td>
                                    <td className="text-right py-1 px-2">
                                      {trade.exit_px}
                                      {isOpen && (
                                        <span className="ml-1 inline-block h-1.5 w-1.5 rounded-full bg-blue-500 animate-pulse" />
                                      )}
                                    </td>
                                    <td
                                      className={`text-right py-1 px-2 ${
                                        pnlRupees >= 0
                                          ? 'text-green-600 dark:text-green-400'
                                          : 'text-red-600 dark:text-red-400'
                                      }`}
                                    >
                                      ₹{trade.pnl_rupees}
                                    </td>
                                    <td className="py-1 pl-2 text-muted-foreground">
                                      {isOpen ? 'OPEN' : trade.exit_reason}
                                    </td>
                                  </tr>
                                )
                              })}
                          </tbody>
                        </table>
                        {todayTradesByStrategy[strategy.id].length > 5 && (
                          <Link
                            to={`/python/${strategy.id}/trades`}
                            className="block text-right text-xs text-primary hover:underline mt-1"
                          >
                            View all trades →
                          </Link>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Delete Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Strategy</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete "{strategyToDelete?.name}"? This will remove the
              strategy file and all associated logs.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleDelete}>
              Delete Strategy
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Force Exit Dialog */}
      <Dialog open={forceExitDialogOpen} onOpenChange={setForceExitDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Force Exit "{strategyToForceExit?.name}"?</DialogTitle>
            <DialogDescription>
              This will immediately close every open position for this strategy at the current
              market price -- short legs first, then long legs -- and stop it. This cannot be
              undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setForceExitDialogOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleForceExit}>
              Force Exit All Positions
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
