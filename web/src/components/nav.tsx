"use client";

import { Suspense } from "react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { cn } from "@/lib/utils";

function NavLinks() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const fy = searchParams.get("fy");

  // Build links that preserve the FY param when navigating
  const links = [
    { href: `/dashboard${fy ? `?fy=${fy}` : ""}`, label: "Dashboard", match: "/dashboard" },
    { href: `/invoices${fy ? `?fy=${fy}` : ""}`, label: "Invoices", match: "/invoices" },
  ];

  return (
    <div className="flex gap-4">
      {links.map((link) => (
        <Link
          key={link.match}
          href={link.href}
          className={cn(
            "text-sm font-medium transition-colors hover:text-foreground/80",
            pathname.startsWith(link.match)
              ? "text-foreground"
              : "text-foreground/60"
          )}
        >
          {link.label}
        </Link>
      ))}
    </div>
  );
}

export function Nav() {
  return (
    <nav className="border-b bg-background">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="flex h-14 items-center justify-between">
          <div className="flex items-center gap-8">
            <Link href="/dashboard" className="text-lg font-semibold">
              Granite
            </Link>
            <Suspense fallback={<div className="flex gap-4"><span className="text-sm text-muted-foreground">Loading...</span></div>}>
              <NavLinks />
            </Suspense>
          </div>
        </div>
      </div>
    </nav>
  );
}
