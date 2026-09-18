"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui";
import { OrgProjectSwitcher } from "@/components/OrgProjectSwitcher";

export function AppShell({ children }: { children: React.ReactNode }) {
  const { logout } = useAuth();
  const router = useRouter();

  async function handleLogout() {
    await logout();
    router.push("/login");
  }

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b px-6 py-4">
        <Link href="/orgs" className="font-semibold">
          Pulse
        </Link>
        <div className="flex items-center gap-4">
          <OrgProjectSwitcher />
          <Button variant="secondary" onClick={handleLogout}>
            Log out
          </Button>
        </div>
      </header>
      <main className="flex flex-1 flex-col gap-6 p-8">{children}</main>
    </div>
  );
}
