import { useEffect, useRef, useCallback } from 'react'

interface Options {
  onMessage: (data: unknown) => void
  enabled?: boolean
}

const INITIAL_BACKOFF_MS = 1000
const MAX_BACKOFF_MS     = 30_000

export function useWebSocket(channel: string, { onMessage, enabled = true }: Options) {
  const wsRef       = useRef<WebSocket | null>(null)
  const timerRef    = useRef<ReturnType<typeof setTimeout> | null>(null)
  const backoffRef  = useRef(INITIAL_BACKOFF_MS)
  // Tracks whether the current effect instance has been cleaned up.
  // Prevents the stale onclose callback from reopening a WebSocket to the
  // OLD channel after the symbol changes (stale-closure reconnect bug).
  const cancelledRef = useRef(false)
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  const connect = useCallback(() => {
    if (!enabled || cancelledRef.current) return
    // Match the page scheme: an https:// page must use wss:// (browsers block
    // ws:// as mixed content). VITE_WS_URL overrides for cross-origin setups.
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const base = import.meta.env.VITE_WS_URL ?? `${scheme}//${window.location.host}`
    const ws = new WebSocket(`${base}/ws/${channel}`)
    wsRef.current = ws

    ws.onopen = () => {
      // Successful connect — reset the backoff so a future drop starts fast again.
      backoffRef.current = INITIAL_BACKOFF_MS
    }

    ws.onmessage = (e) => {
      try {
        onMessageRef.current(JSON.parse(e.data))
      } catch {
        /* ignore malformed frames */
      }
    }

    ws.onclose = () => {
      // Only schedule a reconnect if this connection is still the active one.
      if (!cancelledRef.current) {
        const delay = backoffRef.current
        backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF_MS)
        timerRef.current = setTimeout(connect, delay)
      }
    }

    ws.onerror = () => ws.close()
  }, [channel, enabled])

  useEffect(() => {
    cancelledRef.current = false
    backoffRef.current = INITIAL_BACKOFF_MS
    connect()
    return () => {
      cancelledRef.current = true
      timerRef.current && clearTimeout(timerRef.current)
      wsRef.current?.close()
    }
  }, [connect])

  const send = useCallback((msg: string) => {
    wsRef.current?.readyState === WebSocket.OPEN && wsRef.current.send(msg)
  }, [])

  return { send }
}
