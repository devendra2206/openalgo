import type {
  EnvironmentVariables,
  ExecutionsResponse,
  LegAction,
  LegErrorsResponse,
  LogContent,
  LogFile,
  MasterContractStatus,
  PnlSnapshot,
  PythonStrategy,
  PythonStrategyContent,
  ScheduleConfig,
  TradesResponse,
} from '@/types/python-strategy'
import type { ApiResponse } from '@/types/trading'
import { webClient } from './client'

export const pythonStrategyApi = {
  /**
   * Get all Python strategies
   */
  getStrategies: async (): Promise<PythonStrategy[]> => {
    const response = await webClient.get<{ strategies: PythonStrategy[] }>('/python/api/strategies')
    return response.data.strategies || []
  },

  /**
   * Get a single strategy
   */
  getStrategy: async (strategyId: string): Promise<PythonStrategy> => {
    const response = await webClient.get<{ strategy: PythonStrategy }>(
      `/python/api/strategy/${strategyId}`
    )
    return response.data.strategy
  },

  /**
   * Get strategy content for editing
   */
  getStrategyContent: async (strategyId: string): Promise<PythonStrategyContent> => {
    const response = await webClient.get<PythonStrategyContent>(
      `/python/api/strategy/${strategyId}/content`
    )
    return response.data
  },

  /**
   * Upload a new strategy with mandatory schedule
   */
  uploadStrategy: async (
    name: string,
    file: File,
    schedule: {
      start_time: string
      stop_time: string
      days: string[]
      exchange?: string
    }
  ): Promise<ApiResponse<{ strategy_id: string }>> => {
    const formData = new FormData()
    formData.append('strategy_name', name)
    formData.append('strategy_file', file)
    formData.append('exchange', schedule.exchange || 'NSE')
    // Add schedule fields
    formData.append('schedule_start', schedule.start_time)
    formData.append('schedule_stop', schedule.stop_time)
    formData.append('schedule_days', JSON.stringify(schedule.days))

    const response = await webClient.post<ApiResponse<{ strategy_id: string }>>(
      '/python/new',
      formData,
      {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
      }
    )
    return response.data
  },

  /**
   * Save strategy content
   */
  saveStrategy: async (strategyId: string, content: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/save/${strategyId}`, {
      content,
    })
    return response.data
  },

  /**
   * Export strategy file
   */
  exportStrategy: async (
    strategyId: string,
    version: 'saved' | 'current' = 'saved'
  ): Promise<Blob> => {
    const response = await webClient.get(`/python/export/${strategyId}?version=${version}`, {
      responseType: 'blob',
    })
    return response.data
  },

  /**
   * Delete a strategy
   */
  deleteStrategy: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/delete/${strategyId}`)
    return response.data
  },

  /**
   * Start a strategy
   */
  startStrategy: async (strategyId: string): Promise<ApiResponse<{ process_id: number }>> => {
    const response = await webClient.post<ApiResponse<{ process_id: number }>>(
      `/python/start/${strategyId}`
    )
    return response.data
  },

  /**
   * Stop a strategy
   */
  stopStrategy: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/stop/${strategyId}`)
    return response.data
  },

  /**
   * Clear error state
   */
  clearError: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/clear-error/${strategyId}`)
    return response.data
  },

  /**
   * Schedule a strategy
   */
  scheduleStrategy: async (
    strategyId: string,
    config: ScheduleConfig
  ): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(
      `/python/schedule/${strategyId}`,
      config
    )
    return response.data
  },

  /**
   * Unschedule a strategy
   */
  unscheduleStrategy: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/unschedule/${strategyId}`)
    return response.data
  },

  /**
   * Get log files for a strategy
   */
  getLogFiles: async (strategyId: string): Promise<LogFile[]> => {
    const response = await webClient.get<{ logs: LogFile[] }>(`/python/api/logs/${strategyId}`)
    return response.data.logs || []
  },

  /**
   * Get log file content
   */
  getLogContent: async (strategyId: string, logName: string): Promise<LogContent> => {
    const response = await webClient.get<LogContent>(`/python/api/logs/${strategyId}/${logName}`)
    return response.data
  },

  /**
   * Clear all logs for a strategy
   */
  clearLogs: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/logs/${strategyId}/clear`)
    return response.data
  },

  /**
   * Get environment variables
   */
  getEnvVariables: async (strategyId: string): Promise<EnvironmentVariables> => {
    const response = await webClient.get<EnvironmentVariables>(`/python/env/${strategyId}`)
    return response.data
  },

  /**
   * Save environment variables
   */
  saveEnvVariables: async (
    strategyId: string,
    variables: EnvironmentVariables
  ): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(`/python/env/${strategyId}`, variables)
    return response.data
  },

  /**
   * Get the last-pushed PnL snapshot for a strategy
   */
  getPnl: async (strategyId: string): Promise<PnlSnapshot> => {
    const response = await webClient.get<PnlSnapshot>(`/python/api/strategy/${strategyId}/pnl`)
    return response.data
  },

  /**
   * Get today's closed trades for a strategy. Pass `date` (YYYY-MM-DD) to
   * filter by trade date instead -- takes priority over `executionId` on
   * the backend when both are passed.
   */
  getTrades: async (
    strategyId: string,
    executionId?: string,
    date?: string
  ): Promise<TradesResponse> => {
    const params: Record<string, string> = {}
    if (date) params.date = date
    else if (executionId) params.execution_id = executionId
    const response = await webClient.get<TradesResponse>(
      `/python/api/strategy/${strategyId}/trades`,
      { params: Object.keys(params).length ? params : undefined }
    )
    return response.data
  },

  /**
   * List execution runs for a strategy (for the Trades page's dropdown)
   */
  getExecutions: async (strategyId: string): Promise<ExecutionsResponse> => {
    const response = await webClient.get<ExecutionsResponse>(
      `/python/api/strategy/${strategyId}/executions`
    )
    return response.data
  },

  /**
   * Get legs currently in error mode for a strategy (Retry/Cancel/Manually
   * Completed page). See docs/prd/python-strategies-order-error-recovery.md.
   */
  getErrors: async (strategyId: string): Promise<LegErrorsResponse> => {
    const response = await webClient.get<LegErrorsResponse>(
      `/python/api/strategy/${strategyId}/errors`
    )
    return response.data
  },

  /**
   * Resolve a leg's error mode: Retry, Cancel, or Mark as Manually Completed
   * (fill_price required for "manual"). Returns immediately -- the strategy
   * picks this up on its own next cycle; watch for the error_update SSE
   * event (or re-poll getErrors) to see it actually resolved.
   */
  postLegAction: async (
    strategyId: string,
    legKey: string,
    action: LegAction,
    fillPrice?: number
  ): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(
      `/python/api/strategy/${strategyId}/action`,
      { leg_key: legKey, action, fill_price: fillPrice }
    )
    return response.data
  },

  /**
   * Force-close every open position for a running strategy (short legs
   * before long legs where both exist), then stop it. Returns immediately --
   * the strategy picks this up on its own next cycle; watch for the
   * status_update SSE event (or re-poll getStrategies) to see it stopped.
   */
  forceExitStrategy: async (strategyId: string): Promise<ApiResponse<void>> => {
    const response = await webClient.post<ApiResponse<void>>(
      `/python/api/strategy/${strategyId}/force_exit`
    )
    return response.data
  },

  /**
   * Get master contract status
   */
  getMasterContractStatus: async (): Promise<MasterContractStatus> => {
    const response = await webClient.get<MasterContractStatus>('/python/status')
    return response.data
  },

  /**
   * Check and start pending strategies (after master contract download)
   */
  checkAndStartPending: async (): Promise<ApiResponse<{ started: number }>> => {
    const response =
      await webClient.post<ApiResponse<{ started: number }>>('/python/check-contracts')
    return response.data
  },
}
