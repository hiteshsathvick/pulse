import type { QueryResult } from "@/lib/insights-api";
import { FunnelResult } from "./FunnelResult";
import { RetentionResult } from "./RetentionResult";
import { TrendResult } from "./TrendResult";

export function InsightResult({ result }: { result: QueryResult }) {
  switch (result.kind) {
    case "trend":
      return <TrendResult rows={result.results} approximate={result.approximate} />;
    case "funnel":
      return <FunnelResult rows={result.results} />;
    case "retention":
      return <RetentionResult rows={result.results} period={result.period} />;
  }
}
