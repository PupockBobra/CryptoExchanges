/**
 * fetchJson — fetch + JSON parse that throws on a non-2xx response.
 *
 * Plain `fetch(url).then(r => r.json())` silently parses an error body (e.g. a
 * FastAPI `{detail: …}` 500) and hands it downstream, where `.map`/pivot code
 * then crashes on a non-array. Throwing here lets the caller's try/catch keep
 * the previous data and surface the failure instead.
 */
export async function fetchJson<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const res = await fetch(input, init)
  if (!res.ok) {
    throw new Error(`Request failed: ${res.status} ${res.statusText} (${typeof input === 'string' ? input : ''})`)
  }
  return res.json() as Promise<T>
}
