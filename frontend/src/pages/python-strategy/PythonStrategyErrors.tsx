import { AlertTriangle, ArrowLeft, RefreshCw } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'
import { pythonStrategyApi } from '@/api/python-strategy'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import type { LegAction, LegError, PythonStrategy } from '@/types/python-strategy'
import { showToast } from '@/utils/toast'

function formatSince(value: string): string {
  if (!value) return 'unknown'
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

// The button's own label tells the user what will actually happen -- it
// differs by side (entry/exit) and by whether the order is still resting at
// the broker or already dead. See docs/prd/python-strategies-order-error-recovery.md.
function retryLabel(leg: LegError): string {
  return leg.error_kind === 'resting' ? 'Re-price and retry' : 'Place new order'
}

function cancelLabel(leg: LegError): string {
  if (leg.error_state === 'exit_failed') return 'Ignore & keep position open'
  return leg.error_kind === 'resting' ? 'Give it one more chance, then cancel' : 'Abandon entry'
}

export default function PythonStrategyErrors() {
  const { strategyId } = useParams<{ strategyId: string }>()
  const [strategy, setStrategy] = useState<PythonStrategy | null>(null)
  const [errors, setErrors] = useState<LegError[]>([])
  const [loading, setLoading] = useState(true)
  const [pendingLegs, setPendingLegs] = useState<Set<string>>(new Set())
  const [manualInputLeg, setManualInputLeg] = useState<string | null>(null)
  const [manualPrice, setManualPrice] = useState('')

  const fetchData = async () => {
    if (!strategyId) return
    try {
      setLoading(true)
      const [strategyData, errorsData] = await Promise.all([
        pythonStrategyApi.getStrategy(strategyId),
        pythonStrategyApi.getErrors(strategyId),
      ])
      setStrategy(strategyData)
      setErrors(errorsData.errors)
    } catch (_error) {
      showToast.error('Failed to load errors', 'pythonStrategy')
    } finally {
      setLoading(false)
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: one-time initial load on mount
  useEffect(() => {
    fetchData()
  }, [])

  // Live updates via the same multiplexed SSE stream the strategy card uses --
  // a resolved leg drops off this page without the user needing to refresh.
  useEffect(() => {
    if (!strategyId) return
    const eventSource = new EventSource('/python/api/events')
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'error_update' && data.strategy_id === strategyId) {
          fetchData()
          // The backend has just reported a fresh state for this strategy's
          // errors -- whether a pending action resolved cleanly or re-failed
          // (e.g. Retry attempted again and failed once more), this is the
          // authoritative signal that whatever was "pending" has finished.
          // Without clearing here, a leg whose Retry/Cancel/Manual action
          // re-entered error mode would show "Waiting for the strategy to
          // confirm..." forever, permanently hiding its buttons.
          setPendingLegs(new Set())
        }
      } catch {
        // ignore malformed events
      }
    }
    return () => eventSource.close()
    // biome-ignore lint/correctness/useExhaustiveDependencies: fetchData is recreated every render
  }, [strategyId])

  const sendAction = async (legKey: string, action: LegAction, fillPrice?: number) => {
    if (!strategyId) return
    try {
      setPendingLegs((prev) => new Set(prev).add(legKey))
      await pythonStrategyApi.postLegAction(strategyId, legKey, action, fillPrice)
      showToast.success('Action sent -- waiting for the strategy to confirm...', 'pythonStrategy')
      setManualInputLeg(null)
      setManualPrice('')
    } catch (_error) {
      showToast.error('Failed to send action', 'pythonStrategy')
      setPendingLegs((prev) => {
        const next = new Set(prev)
        next.delete(legKey)
        return next
      })
    }
  }

  const confirmManual = (legKey: string) => {
    const price = Number.parseFloat(manualPrice)
    if (!price || price <= 0) {
      showToast.error('Enter a valid fill price', 'pythonStrategy')
      return
    }
    sendAction(legKey, 'manual', price)
  }

  if (loading) {
    return (
      <div className="container mx-auto py-6 space-y-6">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-[300px]" />
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
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <AlertTriangle className="h-6 w-6 text-red-500" />
            Action Needed
          </h1>
          <p className="text-muted-foreground">{strategy.name}</p>
        </div>
        <Button variant="outline" size="sm" onClick={fetchData}>
          <RefreshCw className="h-4 w-4 mr-2" />
          Refresh
        </Button>
      </div>

      {errors.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            No legs currently need attention.
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {errors.map((leg) => {
            const isPending = pendingLegs.has(leg.leg_key)
            return (
              <Card key={leg.leg_key} className="border-red-500/40">
                <CardHeader>
                  <CardTitle className="text-sm flex items-center justify-between flex-wrap gap-2">
                    <span className="flex items-center gap-2">
                      {leg.leg_key}
                      <Badge variant="destructive">
                        {leg.error_state === 'entry_failed' ? 'Entry failed' : 'Exit failed'}
                      </Badge>
                      <Badge variant="outline">
                        {leg.error_kind === 'resting' ? 'Still resting at broker' : 'Rejected/cancelled'}
                      </Badge>
                    </span>
                    <span className="text-xs text-muted-foreground font-normal">
                      In error since {formatSince(leg.error_since)}
                    </span>
                  </CardTitle>
                  <CardDescription>
                    Tried to {leg.action} {leg.quantity} x{' '}
                    <span className="font-mono">{leg.symbol}</span>
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <p className="text-sm text-muted-foreground">{leg.error_message}</p>

                  {isPending ? (
                    <p className="text-sm text-blue-500">
                      Waiting for the strategy to confirm...
                    </p>
                  ) : (
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        variant="default"
                        size="sm"
                        onClick={() => sendAction(leg.leg_key, 'retry')}
                      >
                        {retryLabel(leg)}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => sendAction(leg.leg_key, 'cancel')}
                      >
                        {cancelLabel(leg)}
                      </Button>
                      {manualInputLeg === leg.leg_key ? (
                        <div className="flex items-center gap-2">
                          <Input
                            type="number"
                            step="0.05"
                            placeholder="Fill price"
                            value={manualPrice}
                            onChange={(e) => setManualPrice(e.target.value)}
                            className="w-28 h-9"
                          />
                          <Button size="sm" onClick={() => confirmManual(leg.leg_key)}>
                            Confirm
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => {
                              setManualInputLeg(null)
                              setManualPrice('')
                            }}
                          >
                            Cancel
                          </Button>
                        </div>
                      ) : (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => setManualInputLeg(leg.leg_key)}
                        >
                          Mark as Manually Completed
                        </Button>
                      )}
                    </div>
                  )}
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}
    </div>
  )
}
