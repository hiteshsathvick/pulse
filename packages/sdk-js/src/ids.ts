/** crypto.randomUUID() is native in every browser this SDK targets and in
 * Node 19+ -- no reason to pull in a uuid package for this. */
export function generateId(): string {
  return crypto.randomUUID();
}

function randomHex(bytes: number): string {
  const buffer = new Uint8Array(bytes);
  crypto.getRandomValues(buffer);
  // The W3C spec forbids an all-zero id; a single fixed bit makes that
  // impossible without a retry loop (2^-128 odds otherwise, but "cannot"
  // beats "won't").
  buffer[bytes - 1] = (buffer[bytes - 1] ?? 0) | 1;
  return Array.from(buffer, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** A W3C `traceparent` header value (version 00, sampled). The SDK has no
 * OpenTelemetry dependency on purpose (it ships into other people's pages),
 * so it only *originates* the trace id: /ingest continues it, and the ingest
 * worker's spans join the same trace through the Redis stream. The SDK's own
 * span is never exported, so a trace viewer shows the API's server span as
 * the first hop with this id's parent missing. */
export function generateTraceparent(): string {
  return `00-${randomHex(16)}-${randomHex(8)}-01`;
}
