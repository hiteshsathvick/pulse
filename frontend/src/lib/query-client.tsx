"use client";

import {
  QueryClient,
  QueryClientProvider as TanstackQueryClientProvider,
} from "@tanstack/react-query";
import { useState } from "react";

export function QueryClientProvider({ children }: { children: React.ReactNode }) {
  // Created once per browser session (not module-level): a module-level
  // singleton would leak cached data across users/orgs during Next.js's
  // request-time module reuse and, more simply, across React's dev-mode
  // remounts.
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            retry: 1,
          },
        },
      })
  );

  return <TanstackQueryClientProvider client={client}>{children}</TanstackQueryClientProvider>;
}
