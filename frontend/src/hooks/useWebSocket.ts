import { useEffect, useRef, useCallback } from 'react'

interface Options {
  onMessage: (data: unknown) => void
  enabled?: boolean
}

export function useWebSocket(channel: string, { onMessage, enabled = true }: Options) {
  const wsRef = useRef<WebSocket | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Tracks whether the current effect instance has been cleaned up.
  // Prevents the stale onclose callback from reopening a WebSocket to the
  // OLD channel after the symbol changes (stale-closure reconnect bug).
  const cancelledRef = useRef(false)
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  const connect = useCallback(() => {
    if (!enabled || cancelledRef.current) return
    const base = import.meta.env.VITE_WS_URL ?? `ws://${window.location.host}`
    const ws = new WebSocket(`${base}/ws/${channel}`)
    wsRef.current = ws

    ws.onmessage = (e) => {
      try {
        onMessageRef.current(JSON.parse(e.data))
      } catch {
        /* ignore malformed frames */
      }
    }

    ws.onclose = () => {
      // Only schedule a reconnect if this connection is still the active one.
      // Without this guard, cleanup calls ws.close() → onclose fires →
      // setTimeout enqueues connect() with the OLD channel still in scope.
      if (!cancelledRef.current) {
        timerRef.current = setTimeout(connect, 2000)
      }
    }

    ws.onerror = () => ws.close()
  }, [channel, enabled])

  useEffect(() => {
    cancelledRef.current = false  // mark this effect instance as active
    connect()
    return () => {
      cancelledRef.current = true  // block any pending onclose from reconnecting
      timerRef.current && clearTimeout(timerRef.current)
      wsRef.current?.close()
    }
  }, [connect])

  const send = useCallback((msg: string) => {
    wsRef.current?.readyState === WebSocket.OPEN && wsRef.current.send(msg)
  }, [])

  return { send }
}
