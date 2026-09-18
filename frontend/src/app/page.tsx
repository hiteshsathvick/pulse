"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth-context";

export default function Home() {
  const { user, initializing } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (initializing) return;
    router.replace(user ? "/orgs" : "/login");
  }, [initializing, user, router]);

  return <p className="p-24">Loading…</p>;
}
