import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "link" | "danger";

const VARIANT_CLASSES: Record<Variant, string> = {
  primary: "rounded px-3 py-2 bg-black text-white",
  secondary: "rounded px-3 py-2 border bg-white text-black",
  link: "text-sm underline",
  danger: "text-sm text-red-600 underline",
};

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
};

export function Button({ variant = "primary", className = "", ...props }: ButtonProps) {
  return (
    <button
      className={`disabled:opacity-50 ${VARIANT_CLASSES[variant]} ${className}`.trim()}
      {...props}
    />
  );
}
