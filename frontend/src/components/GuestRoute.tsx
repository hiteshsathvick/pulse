"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth-context";

/** The inverse of ProtectedRoute: redirects away (to /orgs) if the visitor
 * is already signed in, for pages like /login and /register that only make
 * sense to a logged-out visitor. */
export function GuestRoute({ children }: { children: React.ReactNode }) {
  const { user, initializing } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!initializing && user) {
      router.replace("/orgs");
    }
  }, [initializing, user, router]);

  if (initializing) {
    return <p className="p-24">Checking session…</p>;
  }
  if (user) {
    return null;
  }
  return <>{children}</>;
}
