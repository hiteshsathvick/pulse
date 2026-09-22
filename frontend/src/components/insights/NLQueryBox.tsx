"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, Button, Input, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import type { InsightSpec } from "@/lib/insight-spec";
import { runInsightQuery, translateNLQuery, type QueryResult } from "@/lib/insights-api";
import { InsightResult } from "./InsightResult";

type Translation =
  | { status: "ok"; spec: InsightSpec; warnings: string[] }
  | { status: "clarify"; message: string };

// A short, human-readable one-liner shown alongside the raw JSON -- the
// point of "interpreted spec shown before running" (SPEC.md #6.15) is that
// the user can sanity-check it without having to read JSON.
function describeSpec(spec: InsightSpec): string {
  if (spec.kind === "trend") {
    const events = spec.events.map((e) => `"${e}"`).join(", ");
    return `Trend: ${spec.measure} of ${events}, ${spec.range.from} to ${spec.range.to} (by ${spec.granularity})`;
  }
  if (spec.kind === "funnel") {
    const steps = spec.steps.map((s) => s.event).join(" → ");
    return `Funnel: ${steps} (within ${spec.window.value} ${spec.window.unit}${spec.window.value === 1 ? "" : "s"}), ${spec.range.from} to ${spec.range.to}`;
  }
  return `Retention: born "${spec.born_event}", return "${spec.return_event}", ${spec.periods} ${spec.period}s, ${spec.range.from} to ${spec.range.to}`;
}

export function NLQueryBox({ orgId, projectId }: { orgId: string; projectId: string }) {
  const { accessToken } = useAuth();
  const [question, setQuestion] = useState("");
  const [translation, setTranslation] = useState<Translation | null>(null);
  const [result, setResult] = useState<QueryResult | null>(null);

  const translateMutation = useMutation({
    mutationFn: (q: string) => translateNLQuery(accessToken as string, orgId, projectId, q),
    onSuccess: (response) => {
      setResult(null);
      if (response.status === "ok" && response.spec) {
        setTranslation({ status: "ok", spec: response.spec, warnings: response.warnings });
      } else {
        setTranslation({
          status: "clarify",
          message: response.message ?? "Could you rephrase that?",
        });
      }
    },
  });

  const runMutation = useMutation({
    mutationFn: (spec: InsightSpec) => runInsightQuery(accessToken as string, orgId, projectId, spec),
    onSuccess: setResult,
  });

  const ask = () => {
    const q = question.trim();
    if (!q) return;
    setTranslation(null);
    setResult(null);
    translateMutation.mutate(q);
  };

  return (
    <div className="flex flex-col gap-3 rounded border p-4">
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Ask in English</span>
        <div className="flex gap-2">
          <Input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") ask();
            }}
            placeholder='e.g. "how many users completed checkout completed last week"'
            className="flex-1"
          />
          <Button
            type="button"
            disabled={question.trim().length === 0 || translateMutation.isPending}
            onClick={ask}
          >
            Ask
          </Button>
        </div>
      </label>

      {translateMutation.isPending && <Spinner label="Interpreting…" />}
      {translateMutation.isError && <Alert>{translateMutation.error.message}</Alert>}

      {translation?.status === "clarify" && <Alert>{translation.message}</Alert>}

      {translation?.status === "ok" && (
        <div className="flex flex-col gap-2 rounded border bg-gray-50 p-3 text-sm">
          <p className="font-medium">{describeSpec(translation.spec)}</p>
          {translation.warnings.map((warning) => (
            <Alert key={warning} variant="warning">
              {warning}
            </Alert>
          ))}
          <details>
            <summary className="cursor-pointer text-xs text-gray-500">View raw spec</summary>
            <pre className="overflow-auto text-xs">{JSON.stringify(translation.spec, null, 2)}</pre>
          </details>
          <div>
            <Button
              type="button"
              variant="secondary"
              disabled={runMutation.isPending}
              onClick={() => runMutation.mutate(translation.spec)}
            >
              Run
            </Button>
          </div>
        </div>
      )}

      {runMutation.isPending && <Spinner label="Running…" />}
      {runMutation.isError && <Alert>{runMutation.error.message}</Alert>}
      {result && <InsightResult result={result} />}
    </div>
  );
}
