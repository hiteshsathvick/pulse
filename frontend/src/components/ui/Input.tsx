import type { InputHTMLAttributes } from "react";

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`rounded border px-3 py-2 ${className}`.trim()} {...props} />;
}
