const DEFAULT_TIMEOUT_MS = 30_000;

type ErrorPayload = {
  detail?: string;
  message?: string;
};

export async function apiJson<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort("Talos request timed out"), timeoutMs);
  const abort = () => controller.abort(init?.signal?.reason);
  init?.signal?.addEventListener("abort", abort, { once: true });

  try {
    const response = await fetch(`/api/talos/${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
      signal: controller.signal,
    });
    const correlationId = response.headers.get("X-Correlation-ID");
    if (!response.ok) {
      const payload = (await response.json().catch(() => null)) as ErrorPayload | null;
      const detail = payload?.detail ?? payload?.message;
      const suffix = correlationId ? ` (correlation: ${correlationId})` : "";
      throw new Error((detail ?? `Talos request failed with HTTP ${response.status}`) + suffix);
    }
    return (await response.json()) as T;
  } catch (error) {
    if (controller.signal.aborted && !init?.signal?.aborted) {
      throw new Error("Talos request timed out.");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    init?.signal?.removeEventListener("abort", abort);
  }
}
