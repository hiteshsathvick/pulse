import { useId } from "react";
import type { InputHTMLAttributes } from "react";
import { Input } from "@/components/ui";

// A native <input list> + <datalist>: browser-provided autocomplete that still
// accepts free text, so an event/property not yet in the schema registry can
// still be typed (the registry only learns a name once it has been ingested).
export function SuggestInput({
  options,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { options: string[] }) {
  const listId = useId();
  return (
    <>
      <Input list={listId} autoComplete="off" {...props} />
      <datalist id={listId} data-testid={`suggestions-${props["aria-label"] ?? props.name ?? ""}`}>
        {options.map((option) => (
          <option key={option} value={option} />
        ))}
      </datalist>
    </>
  );
}
