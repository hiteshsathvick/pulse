export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" className="text-gray-500">
      {label}
    </p>
  );
}
