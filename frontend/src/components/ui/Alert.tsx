import type { HTMLAttributes } from "react";

type Variant = "error" | "success" | "warning";

const VARIANT_CLASSES: Record<Variant, string> = {
  error: "text-red-600",
  success: "text-green-700",
  warning: "text-amber-600",
};

type AlertProps = HTMLAttributes<HTMLParagraphElement> & {
  variant?: Variant;
};

export function Alert({ variant = "error", className = "", ...props }: AlertProps) {
  return <p className={`${VARIANT_CLASSES[variant]} ${className}`.trim()} {...props} />;
}
