"use client";

import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, Suspense, useState } from "react";
import { Alert, Button, Input } from "@/components/ui";
import { GuestRoute } from "@/components/GuestRoute";
import { useAuth } from "@/lib/auth-context";

function LoginForm() {
  const { login } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const loginMutation = useMutation({
    mutationFn: () => login(email, password),
    onSuccess: () => router.push("/orgs"),
  });

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    loginMutation.mutate();
  }

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 p-24">
      <h1 className="text-2xl font-semibold">Log in</h1>
      {searchParams.get("registered") === "1" && (
        <p data-testid="registered-notice">Account created — log in below.</p>
      )}
      <form onSubmit={handleSubmit} className="flex w-full max-w-sm flex-col gap-3">
        <Input
          type="email"
          placeholder="Email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
        />
        <Input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />
        {loginMutation.isError && (
          <Alert data-testid="login-error">
            {loginMutation.error instanceof Error ? loginMutation.error.message : "login failed"}
          </Alert>
        )}
        <Button type="submit" disabled={loginMutation.isPending}>
          {loginMutation.isPending ? "Logging in…" : "Log in"}
        </Button>
      </form>
      <p>
        No account?{" "}
        <Link href="/register" className="underline">
          Register
        </Link>
      </p>
    </main>
  );
}

export default function LoginPage() {
  return (
    <GuestRoute>
      <Suspense>
        <LoginForm />
      </Suspense>
    </GuestRoute>
  );
}
