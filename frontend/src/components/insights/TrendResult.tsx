"use client";

import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Button, EmptyState } from "@/components/ui";
import { formatValue, pivotTrend, SINGLE_SERIES } from "@/lib/insight-results";
import type { TrendRow } from "@/lib/insights-api";

type View = "line" | "bar" | "table";

const VIEWS: { view: View; label: string }[] = [
  { view: "line", label: "Line" },
  { view: "bar", label: "Bar" },
  { view: "table", label: "Table" },
];

const SERIES_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777"];

const tooltipValue = (value: unknown) => (typeof value === "number" ? formatValue(value) : String(value));

export function TrendResult({ rows, approximate = false }: { rows: TrendRow[]; approximate?: boolean }) {
  const [view, setView] = useState<View>("line");

  if (rows.length === 0) return <EmptyState>No events matched this query.</EmptyState>;

  const { data, series } = pivotTrend(rows);
  const singleSeries = series.length === 1 && series[0] === SINGLE_SERIES;

  return (
    <div className="flex flex-col gap-3">
      {approximate && (
        <p className="text-xs text-gray-500" role="note">
          Unique-user counts for a range this large are estimated (typically within about 1%).
        </p>
      )}
      <div className="flex gap-2" role="group" aria-label="Trend view">
        {VIEWS.map((v) => (
          <Button
            key={v.view}
            type="button"
            variant={view === v.view ? "primary" : "secondary"}
            aria-pressed={view === v.view}
            onClick={() => setView(v.view)}
          >
            {v.label}
          </Button>
        ))}
      </div>

      {view === "table" ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b">
                <th className="py-2 pr-4 font-medium">Bucket</th>
                {series.map((s) => (
                  <th key={s} className="py-2 pr-4 font-medium">
                    {singleSeries ? "Value" : s}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.map((point) => (
                <tr key={String(point.bucket)} className="border-b">
                  <td className="py-2 pr-4">{point.bucket}</td>
                  {series.map((s) => (
                    <td key={s} className="py-2 pr-4">
                      {typeof point[s] === "number" ? formatValue(point[s]) : "—"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div
          role="img"
          aria-label={`${view === "line" ? "Line" : "Bar"} chart of the measure over time`}
          className="h-80 w-full"
        >
          <ResponsiveContainer width="100%" height="100%">
            {view === "line" ? (
              <LineChart data={data}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="bucket" />
                <YAxis />
                <Tooltip formatter={tooltipValue} />
                {!singleSeries && <Legend />}
                {series.map((s, i) => (
                  <Line
                    key={s}
                    type="monotone"
                    dataKey={s}
                    stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
                    dot={data.length < 40}
                  />
                ))}
              </LineChart>
            ) : (
              <BarChart data={data}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="bucket" />
                <YAxis />
                <Tooltip formatter={tooltipValue} />
                {!singleSeries && <Legend />}
                {series.map((s, i) => (
                  <Bar key={s} dataKey={s} fill={SERIES_COLORS[i % SERIES_COLORS.length]} />
                ))}
              </BarChart>
            )}
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
