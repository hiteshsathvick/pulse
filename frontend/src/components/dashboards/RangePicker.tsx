import { Input } from "@/components/ui";
import {
  RANGE_PRESET_DAYS,
  type DashboardRange,
  type ResolvedRange,
} from "@/lib/dashboard-range";

const SELECT_CLASS = "rounded border px-3 py-2";

export function RangePicker({
  label,
  value,
  onChange,
  customSeed,
}: {
  label: string;
  value: DashboardRange;
  onChange: (range: DashboardRange) => void;
  // The dates to start a custom range from when the user switches to it, so
  // it opens on what they were just looking at rather than blank.
  customSeed: ResolvedRange;
}) {
  const presets: number[] = [...RANGE_PRESET_DAYS];
  const isOddRelative = value.type === "relative" && !presets.includes(value.days);
  const selected = value.type === "absolute" ? "custom" : String(value.days);

  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">{label}</span>
        <select
          className={SELECT_CLASS}
          value={selected}
          onChange={(e) => {
            if (e.target.value === "custom") {
              onChange({ type: "absolute", from: customSeed.from, to: customSeed.to });
            } else {
              onChange({ type: "relative", days: Number(e.target.value) });
            }
          }}
        >
          {presets.map((days) => (
            <option key={days} value={days}>
              Last {days} days
            </option>
          ))}
          {isOddRelative && value.type === "relative" && (
            <option value={value.days}>Last {value.days} days</option>
          )}
          <option value="custom">Custom range</option>
        </select>
      </label>
      {value.type === "absolute" && (
        <>
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">From</span>
            <Input
              type="date"
              aria-label={`${label} from`}
              value={value.from}
              onChange={(e) => onChange({ ...value, from: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">To</span>
            <Input
              type="date"
              aria-label={`${label} to`}
              value={value.to}
              onChange={(e) => onChange({ ...value, to: e.target.value })}
            />
          </label>
        </>
      )}
    </div>
  );
}
