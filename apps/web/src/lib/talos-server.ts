const ALLOWED_ROUTES = [
  /^doctor$/,
  /^models(?:\/(?:credentials|profiles|refresh))?$/,
  /^extensions$/,
  /^extensions\/tools(?:\/reload)?$/,
  /^extensions\/skills(?:\/upload)?$/,
  /^agent$/,
  /^config$/,
  /^guardrails$/,
  /^runtime-settings$/,
  /^traces$/,
  /^traces\/[A-Za-z0-9-]+$/,
  /^usage$/,
  /^alerts$/,
  /^evaluation-configurations$/,
  /^evaluation-datasets$/,
  /^evaluation-datasets\/[A-Za-z0-9-]+$/,
  /^evaluation-runs$/,
  /^evaluation-runs\/[A-Za-z0-9-]+$/,
  /^evaluation-runs\/[A-Za-z0-9-]+\/cancel$/,
  /^conversations\/[A-Za-z0-9-]+\/why(?:\/history)?$/,
  /^conversations$/,
  /^conversations\/[A-Za-z0-9-]+$/,
  /^conversations\/[A-Za-z0-9-]+\/repository$/,
  /^repositories\/url$/,
  /^repositories\/jobs\/[A-Za-z0-9-]+(?:\/cancel)?$/,
  /^context\/estimate$/,
  /^chat\/stream$/,
  /^knowledge$/,
  /^knowledge\/jobs\/[A-Za-z0-9-]+$/,
  /^knowledge\/[A-Za-z0-9-]+\/preview$/,
  /^runs\/[A-Za-z0-9-]+\/cancel$/,
];

const MAX_REQUEST_BYTES = 8 * 1024 * 1024;
const UPSTREAM_TIMEOUT_MS = 65_000;

function requiredEnvironment(name: "TALOS_WEB_API_BASE_URL" | "TALOS_LOCAL_API_TOKEN"): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Required server environment variable ${name} is missing`);
  return value;
}

export async function proxyTalos(path: string, request: Request): Promise<Response> {
  const method = request.method.toUpperCase();
  const requestUrl = new URL(request.url);
  const rejected = rejectRequest(path, method, request, requestUrl);
  if (rejected) return rejected;

  const declaredLength = Number(request.headers.get("Content-Length") ?? 0);
  if (declaredLength > MAX_REQUEST_BYTES) return bodyTooLarge();

  const body = method === "GET" ? undefined : await request.arrayBuffer();
  if (body && body.byteLength > MAX_REQUEST_BYTES) return bodyTooLarge();

  return forwardRequest(path, method, request, requestUrl, body);
}

function rejectRequest(
  path: string,
  method: string,
  request: Request,
  requestUrl: URL,
): Response | null {
  if (!ALLOWED_ROUTES.some((pattern) => pattern.test(path))) {
    return Response.json({ message: "Unsupported Talos API route" }, { status: 404 });
  }
  if (!["GET", "POST", "PATCH"].includes(method)) return unsupportedMethod();
  if (path.includes("/why") && method !== "GET") return unsupportedMethod();
  if (method === "PATCH" && !["guardrails", "runtime-settings"].includes(path)) {
    return unsupportedMethod();
  }
  const host = request.headers.get("Host")?.toLowerCase();
  const hostname = host?.startsWith("[") ? host.slice(1, host.indexOf("]")) : host?.split(":")[0];
  if (!host || !hostname || !["127.0.0.1", "localhost", "::1"].includes(hostname)) {
    return Response.json({ message: "Talos Web is available only on loopback" }, { status: 403 });
  }

  const origin = request.headers.get("Origin");
  if (method !== "GET" && !origin) {
    return Response.json({ message: "Origin is required for mutating requests" }, { status: 403 });
  }
  return origin ? rejectCrossOrigin(origin, requestUrl.protocol, host) : null;
}

function rejectCrossOrigin(origin: string, protocol: string, host: string): Response | null {
  try {
    // The internal bind host in request.url can differ from the loopback Host used by the browser.
    const publicOrigin = new URL(`${protocol}//${host}`).origin;
    return new URL(origin).origin === publicOrigin
      ? null
      : Response.json({ message: "Cross-origin requests are not allowed" }, { status: 403 });
  } catch {
    return Response.json({ message: "Cross-origin requests are not allowed" }, { status: 403 });
  }
}

async function forwardRequest(
  path: string,
  method: string,
  request: Request,
  requestUrl: URL,
  body: ArrayBuffer | undefined,
): Promise<Response> {
  const apiBase = requiredEnvironment("TALOS_WEB_API_BASE_URL").replace(/\/$/, "");
  const token = requiredEnvironment("TALOS_LOCAL_API_TOKEN");
  const upstreamUrl = `${apiBase}/api/v1/${path}${requestUrl.search}`;
  const upstream = await fetch(upstreamUrl, {
    method,
    body,
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body ? { "Content-Type": request.headers.get("Content-Type") ?? "application/json" } : {}),
      ...(request.headers.get("Last-Event-ID") ? { "Last-Event-ID": request.headers.get("Last-Event-ID") as string } : {}),
      ...(request.headers.get("Idempotency-Key") ? { "Idempotency-Key": request.headers.get("Idempotency-Key") as string } : {}),
    },
    signal: AbortSignal.any([request.signal, AbortSignal.timeout(path === "repositories/url" ? 100_000 : UPSTREAM_TIMEOUT_MS)]),
  });

  const headers = new Headers();
  headers.set("Cache-Control", "no-store");
  headers.set("Content-Type", upstream.headers.get("Content-Type") ?? "application/json");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "no-referrer");
  const correlationId = upstream.headers.get("X-Correlation-ID");
  if (correlationId) headers.set("X-Correlation-ID", correlationId);
  return new Response(upstream.body, { status: upstream.status, headers });
}

function unsupportedMethod(): Response {
  return Response.json({ message: "Unsupported method" }, { status: 405 });
}

function bodyTooLarge(): Response {
  return Response.json({ message: "Request body is too large" }, { status: 413 });
}
