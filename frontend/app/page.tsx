type HealthResult = {
  ok: boolean;
  status: number;
  body: unknown;
};

async function getHealth(): Promise<HealthResult> {
  // Runs server-side inside the Next.js container, so it reaches the API over
  // the internal docker network, not through the browser-facing localhost port.
  const baseUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
  try {
    const res = await fetch(`${baseUrl}/health`, { cache: "no-store" });
    const body = await res.json();
    return { ok: res.ok, status: res.status, body };
  } catch (err) {
    return { ok: false, status: 0, body: { error: String(err) } };
  }
}

export default async function Home() {
  const health = await getHealth();

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 p-8">
      <h1 className="text-2xl font-semibold">Pulse</h1>
      <p>
        API health check: {health.ok ? "OK" : "FAILED"} ({health.status})
      </p>
      <pre className="rounded bg-gray-100 p-4 text-sm text-gray-900">
        {JSON.stringify(health.body, null, 2)}
      </pre>
    </main>
  );
}
