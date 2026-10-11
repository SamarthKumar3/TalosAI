export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  const apiBase = process.env.TALOS_WEB_API_BASE_URL?.replace(/\/$/, "");
  if (!apiBase) return Response.json({ status: "not_ready" }, { status: 503 });
  try {
    const response = await fetch(`${apiBase}/health/ready`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });
    return Response.json(
      { status: response.ok ? "ready" : "not_ready" },
      { status: response.ok ? 200 : 503 },
    );
  } catch {
    return Response.json({ status: "not_ready" }, { status: 503 });
  }
}
