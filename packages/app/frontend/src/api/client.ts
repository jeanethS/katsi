const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "";

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) throw new Error(`Request failed with status ${response.status}`);
  return response.status === 204 ? (undefined as T) : (await response.json()) as T;
}

export function sse(path: string, onEvent: (event: MessageEvent<string>) => void, onError: () => void): () => void {
  const stream = new EventSource(`${baseUrl}${path}`);
  stream.onmessage = onEvent;
  stream.onerror = () => { stream.close(); onError(); };
  return () => stream.close();
}
