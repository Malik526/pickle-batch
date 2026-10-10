import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import HomePage from "@/app/(marketing)/page";
import DataDeletionPage from "@/app/(marketing)/data-deletion/page";
import PrivacyPage from "@/app/(marketing)/privacy/page";
import { siteConfig } from "@/lib/site-config";
import TermsPage from "@/app/(marketing)/terms/page";

/**
 * Public routes are the most guarded surface in this milestone — they
 * must keep working exactly as before after the `(marketing)` route
 * group move (Milestone 3.5, Phase 3-ish). These import the real page
 * components directly rather than hitting a server, so a broken import
 * path from the move would fail here immediately.
 */

describe("public routes", () => {
  it("renders the marketing home page", () => {
    render(<HomePage />);
    expect(screen.getByRole("heading", { level: 1, name: /turn finished videos/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /how it works/i })).toBeInTheDocument();
  });

  it("renders the privacy policy page", () => {
    render(<PrivacyPage />);
    expect(screen.getByRole("heading", { level: 1, name: /privacy policy/i })).toBeInTheDocument();
  });

  it("renders the terms of service page", () => {
    render(<TermsPage />);
    expect(screen.getByRole("heading", { level: 1, name: /terms of service/i })).toBeInTheDocument();
  });
});

describe("marketing site chrome (SiteHeader)", () => {
  it("exposes an accessible, labeled control for the mobile menu toggle", async () => {
    const { SiteHeader } = await import("@/components/layout/SiteHeader");
    render(<SiteHeader />);
    const toggle = screen.getByRole("button", { name: /toggle menu/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("still links to /privacy and /terms from the footer after the route-group move", async () => {
    const { SiteFooter } = await import("@/components/layout/SiteFooter");
    render(<SiteFooter />);
    const nav = screen.getByRole("navigation");
    expect(within(nav).getByRole("link", { name: /privacy policy/i })).toHaveAttribute("href", "/privacy");
    expect(within(nav).getByRole("link", { name: /terms of service/i })).toHaveAttribute("href", "/terms");
    expect(within(nav).getByRole("link", { name: /data deletion/i })).toHaveAttribute("href", "/data-deletion");
  });

  // Meta's "Data deletion instructions URL" page.
  describe("/data-deletion", () => {
    it("renders the required sections", () => {
      render(<DataDeletionPage />);
      expect(screen.getByRole("heading", { level: 1, name: "User Data Deletion" })).toBeInTheDocument();
      for (const name of [
        "How to request deletion",
        "What we may delete",
        "Connected Instagram and Meta accounts",
        "Revoking Pickle Batch’s access",
        "Data we may be required to retain",
        "Privacy Policy",
        "Contact",
      ]) {
        expect(screen.getByRole("heading", { level: 2, name })).toBeInTheDocument();
      }
    });

    it("gives a visible contact address with a prefilled subject, and links the Privacy Policy", () => {
      render(<DataDeletionPage />);
      const request = screen.getAllByRole("link", { name: siteConfig.contactEmail })[0];
      expect(request).toHaveAttribute("href", `mailto:${siteConfig.contactEmail}?subject=Data%20Deletion%20Request`);
      expect(screen.getByRole("link", { name: "Privacy Policy" })).toHaveAttribute("href", "/privacy");
    });

    it("doesn't overclaim: no deletion timing, automation, backup or Meta-side deletion promises", () => {
      const { container } = render(<DataDeletionPage />);
      const text = container.textContent ?? "";
      expect(text).toMatch(/does not delete your Instagram, Facebook, Threads, or other Meta account/);
      expect(text).toMatch(/does not by itself delete data/);
      expect(text).not.toMatch(/immediately|permanently deleted|within \d+ (days|hours)|automatic(ally)? delet|backups/i);
    });
  });
});
