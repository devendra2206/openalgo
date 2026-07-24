import { ArrowLeft, RefreshCw, TrendingDown, TrendingUp, Wallet } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { pythonStrategyApi } from '@/api/python-strategy'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { PnlSnapshot, PythonStrategy } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

const emptySnapshot: PnlSnapshot = {
  realized_pnl: 0,
  unrealized_pnl: 0,
  total_pnl: 0,
  open_positions: [],
  updated_at: null,
}

export default function PythonStrategyPnl() {
  const { strategyId } = useParams<{ strategyId: string }>()
  const [strategy, setStrategy] = useState<PythonStrategy | null>(null)
  const [snapshot, setSnapshot] = useState<PnlSnapshot>(emptySnapshot)
  const [loading, setLoading] = useState(true)

  const fetchData = async () => {
    if (!strategyId) return
    try {
      setLoading(true)
      const [strategyData, pnlData] = await Promise.all([
        pythonStrategyApi.getStrategy(strategyId),
        pythonStrategyApi.getPnl(strategyId),
      ])
      setStrategy(strategyData)
      setSnapshot(pnlData)
    } catch (_error) {
      showToast.error('Failed to load PnL', 'pythonStrategy')
    } finally {
      setLoading(false)
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: one-time initial load on mount; fetchData is recreated every render and must not retrigger this effect
  useEffect(() => {
    fetchData()
  }, [])

  // Live PnL updates via the same SSE stream the strategy list page uses --
  // filtered to just this strategy_id, so the page reflects each pushed
  // snapshot without polling.
  // biome-ignore lint/correctness/useExhaustiveDependencies: subscribe once per strategyId; re-subscribing on every render would thrash the EventSource
  useEffect(() => {
    if (!strategyId) return
    const eventSource = new EventSource('/python/api/events')

    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'pnl_update' && data.strategy_id === strategyId) {
          setSnapshot({
            realized_pnl: data.realized_pnl,
            unrealized_pnl: data.unrealized_pnl,
            total_pnl: data.total_pnl,
            open_positions: data.open_positions,
            updated_at: data.updated_at,
          })
        }
      } catch (_e) {
        // Ignore parse errors (heartbeat messages)
      }
    }

    eventSource.onerror = () => {}

    return () => {
      eventSource.close()
    }
  }, [strategyId])

  if (loading) {
    return (
      <div className="container mx-auto py-6 space-y-6">
        <Skeleton className="h-8 w-32" />
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </div>
        <Skeleton className="h-[300px]" />
      </div>
    )
  }

  if (!strategy) {
    return null
  }

  const pnlColor = (value: number) =>
    value >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'

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
          <h1 className="text-2xl font-bold tracking-tight">PnL</h1>
          <p className="text-muted-foreground">{strategy.name}</p>
        </div>
        <div className="flex items-center gap-2">
          {strategy.status === 'running' && (
            <Badge className="bg-green-500 animate-pulse">Live</Badge>
          )}
          <Button variant="outline" size="sm" onClick={fetchData}>
            <RefreshCw className="h-4 w-4 mr-2" />
            Refresh
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Realized PnL</CardDescription>
          </CardHeader>
          <CardContent>
            <div className={`text-2xl font-bold ${pnlColor(snapshot.realized_pnl)}`}>
              ₹{snapshot.realized_pnl.toFixed(2)}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Unrealized PnL</CardDescription>
          </CardHeader>
          <CardContent>
            <div className={`text-2xl font-bold ${pnlColor(snapshot.unrealized_pnl)}`}>
              ₹{snapshot.unrealized_pnl.toFixed(2)}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="flex items-center gap-2">
              <Wallet className="h-4 w-4" />
              Total PnL
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div
              className={`text-2xl font-bold flex items-center gap-2 ${pnlColor(snapshot.total_pnl)}`}
            >
              {snapshot.total_pnl >= 0 ? (
                <TrendingUp className="h-5 w-5" />
              ) : (
                <TrendingDown className="h-5 w-5" />
              )}
              ₹{snapshot.total_pnl.toFixed(2)}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Open Positions</CardTitle>
          <CardDescription>
            {snapshot.open_positions.length} open leg(s)
            {snapshot.updated_at &&
              ` • Last updated: ${new Date(snapshot.updated_at).toLocaleString()}`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {snapshot.open_positions.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              No open positions
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Leg</TableHead>
                  <TableHead>Symbol</TableHead>
                  <TableHead>Direction</TableHead>
                  <TableHead>Qty</TableHead>
                  <TableHead>Entry Price</TableHead>
                  <TableHead>Current Price</TableHead>
                  <TableHead>PnL</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {snapshot.open_positions.map((pos) => (
                  <TableRow key={pos.leg_key}>
                    <TableCell className="font-medium">{pos.leg_key}</TableCell>
                    <TableCell className="font-mono text-xs">{pos.symbol}</TableCell>
                    <TableCell>
                      <Badge variant={pos.direction === 'LONG' ? 'default' : 'secondary'}>
                        {pos.direction}
                      </Badge>
                    </TableCell>
                    <TableCell>{pos.quantity}</TableCell>
                    <TableCell>{pos.entry_price.toFixed(2)}</TableCell>
                    <TableCell>{pos.current_price.toFixed(2)}</TableCell>
                    <TableCell className={pnlColor(pos.pnl)}>₹{pos.pnl.toFixed(2)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
