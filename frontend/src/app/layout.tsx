import type { Metadata } from "next";
import type { ReactNode } from "react";
import { AuthProvider } from "@/lib/auth-context";
import { QueryClientProvider } from "@/lib/query-client";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pulse",
  description: "Real-time product analytics",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <QueryClientProvider>
          <AuthProvider>{children}</AuthProvider>
        </QueryClientProvider>
      </body>
    </html>
  );
}
